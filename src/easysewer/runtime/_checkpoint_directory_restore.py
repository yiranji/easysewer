"""Prepare complete writable forests before native commit; publish afterwards.

No caller/source directory is changed. Precommit rejection drops only the
private restore folder. Once native state commits, any apply failure is fatal
to this lifecycle; the owner retains new/retired trees for diagnosis/cleanup.
"""
from dataclasses import dataclass
from pathlib import Path
from ._directory_tree import DirectoryLimits, _node, _scan, relative_path, link_member, verify_tree
from ._directory_state import verify_absent
from ._checkpoint_directory_inputs import _parents
from ._workspace import remove_owned_tree
from .backend import NativeFailure
from ..validation._cooperative import checkpoint_scope, checkpointed


def _observation(root,path,limits):
    parents=_parents(root,path)
    try:_node(path,'directory')
    except FileNotFoundError:
        verify_absent(path)
        return parents,None,{}
    tree,stamps=_scan(path,limits)
    return parents,tree,stamps


def _check(root,path,expected,limits):
    parents,tree,stamps=_observation(root,path,limits)
    if any(dict(parents).get(p)!=identity for p,identity in expected[0]) or (tree,stamps)!=expected[1:]:
        raise ValueError('Writable checkpoint directory changed during restore preparation: '+str(path))


def _build(archive,target,tree,checkpoint):
    if tree is None:return
    target.mkdir();created={}
    for entry in checkpointed(tree.entries,interval=1):
        path=target/relative_path(entry.path)
        if entry.kind=='directory':path.mkdir(parents=True,exist_ok=True)
        else:
            path.parent.mkdir(parents=True,exist_ok=True)
            if entry.hardlink_to is not None:
                link_member(target,entry,expected_identity=created[entry.hardlink_to],checkpoint=checkpoint)
            else:
                found=[];archive.copy_blob(dict(sha256=entry.sha256,size=entry.size),path,checkpoint=checkpoint,on_create=found.append)
                created[entry.path]=found[0]


@dataclass
class Tree:
    target: Path
    prepared: Path
    retired: Path
    manifest: object
    original: tuple
    staged: tuple
    retired_identity: tuple | None=None
    published: bool=False


class DirectoryRestore:
    def __init__(self,root,folder,limits,rows):
        self.root,self.folder,self.limits,self.rows=root,folder,limits,rows
        self.applied=False

    @classmethod
    def prepare(cls,snapshot,archive,folder,*,limits,checkpoint):
        from ._checkpoint_container import _decode_directory_states
        states=_decode_directory_states(archive.data,snapshot,None)
        from ._checkpoint_directory_outputs import decode
        outputs,_=decode(archive.data,snapshot)
        if states.keys() & outputs.keys():raise ValueError('Mutable and output checkpoint trees overlap')
        states.update(outputs)
        if not states:return None
        root=Path(snapshot.execution_directory).resolve(strict=True)
        folder=Path(folder).absolute()
        if not folder.is_relative_to(root) or folder==root:raise ValueError('Restore directory must be private to the execution workspace')
        budget=DirectoryLimits(total_bytes=limits.total_bytes)
        result=cls(root,folder,budget,[])
        with checkpoint_scope(checkpoint):
            for index,(relative,tree) in enumerate(sorted(states.items())):
                target=root/relative_path(relative)
                if target==folder or target.is_relative_to(folder) or folder.is_relative_to(target):raise ValueError('Restore scratch directory overlaps a writable resource')
                original=_observation(root,target,budget)
                prepared=folder/('directory-'+str(index));retired=folder/('retired-'+str(index))
                _build(archive,prepared,tree,checkpoint)
                staged=_observation(root,prepared,budget)
                if staged[1]!=tree:raise ValueError('Prepared restore tree differs from checkpoint')
                result.rows.append(Tree(target,prepared,retired,tree,original,staged))
            result.check(checkpoint=checkpoint)
        return result

    def bind_streams(self,bindings,outputs,trace,*,checkpoint):
        # Run before native/worker opens. No caller/current tree is linked.
        from ._directory_tree import _node
        self.check(checkpoint=checkpoint)
        def bind(relative,destination):
            checkpoint();target=self.root/relative_path(relative)
            row=next((r for r in self.rows if target.is_relative_to(r.target) and target!=r.target),None)
            if row is None:raise ValueError('Bound stream has no prepared output directory')
            source=row.prepared/target.relative_to(row.target)
            _node(source,'file');verify_absent(destination);destination.hardlink_to(source)
            if not destination.samefile(source):raise ValueError('Prepared output stream link changed')
        for item in bindings['outputs']:bind(item['relative_path'],outputs[item['index']])
        if bindings['trace'] is not None:bind(bindings['trace'],trace)
        # Creating a link legitimately changes a member's metadata. Retain the
        # complete content/topology and inode checks before accepting new stamps.
        for row in self.rows:
            actual=_observation(self.root,row.prepared,self.limits)
            if actual[1]!=row.manifest or {n:s[:2] for n,s in actual[2].items()}!={n:s[:2] for n,s in row.staged[2].items()}:
                raise ValueError('Prepared tree changed while binding streams')
            row.staged=actual
        self.check(checkpoint=checkpoint)

    def check(self,*,checkpoint=lambda:None):
        with checkpoint_scope(checkpoint):
            for row in checkpointed(self.rows,interval=1):
                _check(self.root,row.target,row.original,self.limits)
                _check(self.root,row.prepared,row.staged,self.limits)

    def apply(self):
        # Native commit is irreversible. Complete this critical section without
        # introducing a new cooperative cancellation point.
        with checkpoint_scope(lambda:None):
            if self.applied:raise ValueError('Directory restore is already applied')
            self.check()
            for row in self.rows:
                _check(self.root,row.target,row.original,self.limits)
                _check(self.root,row.prepared,row.staged,self.limits)
                verify_absent(row.retired)
                if row.original[1] is not None:
                    row.target.rename(row.retired)
                    row.retired_identity=row.original[2][''][:2]
                    info=_node(row.retired,'directory')[1]
                    if (info.st_dev,info.st_ino)!=row.retired_identity:raise ValueError('Retired directory identity changed')
                if row.manifest is not None:
                    # Existing parents were identity-checked above; new parents
                    # must remain regular directories before publishing.
                    row.target.parent.mkdir(parents=True,exist_ok=True)
                    _parents(self.root,row.target)
                    verify_absent(row.target)
                    row.prepared.rename(row.target)
                    actual=_observation(self.root,row.target,self.limits)
                    if actual[1]!=row.manifest or {n:s[:2] for n,s in actual[2].items()}!={n:s[:2] for n,s in row.staged[2].items()}:
                        raise ValueError('Restored directory identity/content differs after publication')
                else:verify_absent(row.target)
                row.published=True
            self.applied=True

    def cleanup(self):
        failures=[]
        if not self.applied:return ()
        for row in self.rows:
            if row.retired_identity is None:continue
            try:
                actual=_observation(self.root,row.retired,self.limits)
                if actual[1]!=row.original[1] or {n:s[:2] for n,s in actual[2].items()}!={n:s[:2] for n,s in row.original[2].items()}:
                    raise ValueError('Retired directory changed; preserve it')
                remove_owned_tree(row.retired,parent=self.folder,identity=row.retired_identity)
            except BaseException as error:
                failures.append(NativeFailure(stage='checkpoint_directory_cleanup',code=None,message=f'{type(error).__name__}: {error}'))
        return tuple(failures)
