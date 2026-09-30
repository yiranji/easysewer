"""Initial directory evidence and metadata-only checks at native boundaries.

Complete bytes are checked on admission and checkpoint capture. Boundary checks
bind regular entry identities, membership, absence and hardlink topology without
rehashing whole files. Writable current forests remain separate from their
immutable initial evidence. Ordinary file-only execution keeps its old path.
"""
from dataclasses import dataclass
from pathlib import Path
import os
from ._directory_tree import DirectoryLimits, _node, _stamp, _scan, relative_path
from ._directory_state import initial_path, verify_absent
from ..validation._cooperative import checkpoint_scope, checkpointed, checkpoint as check_work


def _parents(root,path):
    current=root;values=[]
    for part in (None,*path.relative_to(root).parts[:-1]):
        if part is not None:current=current/part
        check_work()
        try:_,info=_node(current,'directory')
        except FileNotFoundError:continue
        values.append((current,_stamp(info)[:3]))
    return tuple(values)


@dataclass(frozen=True)
class DirectoryInput:
    root: Path
    path: Path
    parents: tuple
    identities: tuple
    membership: tuple
    absent: bool

    def check(self):
        # Inspect every currently present ancestor, including parents created
        # beneath a previously absent optional directory's existing ancestor.
        parents=dict(_parents(self.root,self.path))
        if any(parents.get(path)!=identity for path,identity in self.parents):
            raise ValueError('Checkpoint directory ancestor changed since admission')
        if self.absent:
            verify_absent(self.path)
            return
        for name,kind,stamp in checkpointed(self.identities,interval=1):
            _,info=_node(self.path/name,kind)
            if _stamp(info)!=stamp:
                raise ValueError('Checkpoint directory member changed since admission: '+str(self.path/name))
        for name,expected in checkpointed(self.membership,interval=1):
            found=[]
            with os.scandir(self.path/name) as members:
                for member in members:
                    check_work()
                    if len(found)>=len(expected):
                        raise ValueError('Checkpoint directory membership changed since admission')
                    found.append(member.name)
            if tuple(sorted(found))!=expected:
                raise ValueError('Checkpoint directory membership changed since admission')
        # Check parents/root again after enumeration; no content reads here.
        after=dict(_parents(self.root,self.path))
        if any(after.get(path)!=identity for path,identity in self.parents):
            raise ValueError('Checkpoint directory ancestor changed during verification')
        for name,kind,stamp in checkpointed(self.identities,interval=1):
            if kind=='directory' and _stamp(_node(self.path/name,kind)[1])!=stamp:
                raise ValueError('Checkpoint directory changed during verification')


@dataclass(frozen=True)
class DirectoryInputs:
    trees: tuple[DirectoryInput,...]
    files: frozenset[str]

    def check(self,*,checkpoint=None):
        with checkpoint_scope(checkpoint):
            for tree in checkpointed(self.trees,interval=1):tree.check()

    @classmethod
    def read(cls,snapshot,*,checkpoint=None):
        from ._checkpoint_container import _snapshot_trees
        if not any(r.active and r.kind=='directory' and r.role!='swmm:temporary_directory' for r in snapshot.resources):return None
        with checkpoint_scope(checkpoint):
            root=Path(snapshot.execution_directory).resolve(strict=True);trees=_snapshot_trees(snapshot)
            # Optional read-only absence has no manifest or independent initial
            # path, but it still participates in immutable boundary checks.
            for record in checkpointed(snapshot.resources):
                if record.active and record.kind=='directory' and record.access!='write' and record.role!='swmm:temporary_directory' and record.directory_group is None:
                    name=initial_path(record)
                    if trees.setdefault(name,record.tree)!=record.tree:raise ValueError('Conflicting immutable directory evidence')
            result=[];files=set()
            for name,expected in checkpointed(trees.items()):
                path=root/relative_path(name);parents=_parents(root,path)
                if expected is None:
                    verify_absent(path);value=DirectoryInput(root,path,parents,(),(),True)
                else:
                    limits=DirectoryLimits(total_bytes=max(1,expected.total_bytes),entries=max(1,len(expected.entries)),depth=max((len(e.path.split('/')) for e in expected.entries),default=1))
                    actual,stamps=_scan(path,limits)
                    if actual!=expected:raise ValueError('Checkpoint initial directory differs from its snapshot')
                    kinds={'':'directory',**{entry.path:entry.kind for entry in expected.entries}};members={key:[] for key,kind in kinds.items() if kind=='directory'}
                    for entry in checkpointed(expected.entries):
                        parent,_,child=entry.path.rpartition('/');members[parent].append(child)
                        if entry.kind=='file':files.add(name+'/'+entry.path)
                    value=DirectoryInput(root,path,parents,tuple((key,kinds[key],stamps[key]) for key in sorted(kinds)),tuple((key,tuple(sorted(names))) for key,names in sorted(members.items())),False)
                value.check();result.append(value)
            ledger=cls(tuple(result),frozenset(files));ledger.check();return ledger
