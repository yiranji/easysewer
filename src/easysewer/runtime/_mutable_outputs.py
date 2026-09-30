"""Explicit output members in private mutable trees; initial trees stay immutable."""
from dataclasses import replace
from pathlib import Path
from ._directory_tree import DirectoryEntry, DirectoryManifest, relative_path
from ..validation._cooperative import checkpointed


def child(path, root):
    return path != root and path.startswith(root + '/')


def owner(records, output):
    if not output.active or output.access != 'write' or output.role == 'swmm:temporary_directory':
        return None
    if output.relative_path is None:return None
    relative_path(output.relative_path)
    parents = [r for r in checkpointed(records) if r.active and r.access == 'read_write'
        and r.kind == 'directory' and r.initial_relative_path is not None
        and r.role != 'swmm:temporary_directory' and child(output.relative_path, r.relative_path)]
    if not parents:return None
    if output.initial_relative_path is not None or output.directory_group is not None or output.tree is not None or output.sha256 is not None:
        raise ValueError('Mutable output cannot claim captured input evidence')
    # Shared read directory views are allowed; explicit file readers never become writers.
    for other in checkpointed(records):
        if other is output or other.relative_path is None:continue
        a,b=output.relative_path.casefold(),other.relative_path.casefold()
        overlap=a==b or output.kind=='directory' and b.startswith(a+'/')
        nested_writer=output.kind=='directory' and other.active and other.access=='write' and child(other.relative_path,output.relative_path)
        if overlap and not nested_writer:raise ValueError('Mutable output overlaps another declared consumer')
        if other.kind=='directory' and a.startswith(b+'/'):
            if other.role=='swmm:temporary_directory' or not other.active:
                raise ValueError('Mutable output overlaps an inactive or scratch consumer')
            if other.access!='write' and other.initial_relative_path is None:
                raise ValueError('Mutable output overlaps an immutable input tree')
    return min(parents,key=lambda r:len(r.relative_path.split('/')))


def startup_tree(records, root, tree):
    outputs=[r for r in checkpointed(records) if owner(records,r) is not None and child(r.relative_path,root)]
    if not outputs:return tree
    entries={e.path:e for e in tree.entries} if tree is not None else {}
    removed={r.relative_path[len(root)+1:] for r in outputs if r.kind=='file'}
    for name in removed:
        previous=entries.get(name)
        if previous is not None and previous.kind!='file':raise ValueError('Native output member is an initial directory')
    # Rebind surviving hardlinks if a removed native output was their canonical member.
    survivors={};canonical={}
    for name,entry in sorted(entries.items()):
        if name in removed:continue
        if entry.kind=='file':
            first=canonical.setdefault(entry.hardlink_to or name,name)
            entry=replace(entry,hardlink_to=first if first!=name else None)
        survivors[name]=entry
    for record in outputs:
        name=record.relative_path[len(root)+1:];relative_path(name)
        parts=name.split('/');count=len(parts) if record.kind=='directory' else len(parts)-1
        for i in range(1,count+1):
            path='/'.join(parts[:i]);entry=survivors.setdefault(path,DirectoryEntry(path=path,kind='directory'))
            if entry.kind!='directory':raise ValueError('Mutable output parent is an initial file')
    values=tuple(survivors[n] for n in sorted(survivors))
    return DirectoryManifest(entries=values,contract='easysewer:directory-tree:2' if any(e.hardlink_to for e in values) else 'easysewer:directory-tree:1')


def prepare_startup(records, workspace):
    # Captured copies are private. Remove only precisely declared output file members.
    from ._directory_state import mutable_groups
    from ._directory_tree import _node, verify_tree, DirectoryLimits
    groups=mutable_groups(records)
    for root,record in checkpointed(groups.items()):
        expected=startup_tree(records,root,record.tree)
        if expected==record.tree:continue
        target=Path(workspace)/root
        for output in checkpointed(records):
            if owner(records,output) is None or not child(output.relative_path,root):continue
            path=Path(workspace)/output.relative_path
            if output.kind=='file':
                if path.exists() or path.is_symlink():_node(path,'file');path.unlink()
                path.parent.mkdir(parents=True,exist_ok=True)
            else:path.mkdir(parents=True,exist_ok=True)
        limits=DirectoryLimits(total_bytes=max(1,expected.total_bytes),entries=max(1,len(expected.entries)),depth=max((len(e.path.split('/')) for e in expected.entries),default=1))
        verify_tree(target,expected,limits=limits)


def checkpoint_records(snapshot):
    # Python trace is a declared backend output, not an INP FileUse.
    from types import SimpleNamespace
    from ..io.json import JsonDocument
    records=snapshot.resources
    trace=JsonDocument.from_bytes(snapshot.backend_settings).data.get('trace') if snapshot.backend_settings is not None else None
    if trace is not None:
        records=(*records,SimpleNamespace(active=True,access='write',role='run:trace',kind='file',relative_path=trace,
            initial_relative_path=None,directory_group=None,tree=None,sha256=None))
    return records


def checkpoint_owners(snapshot):
    from ._directory_state import mutable_groups
    groups=mutable_groups(snapshot.resources);records=checkpoint_records(snapshot);result={}
    for output in checkpointed(records):
        if not output.active or output.access!='write' or output.role=='swmm:temporary_directory':continue
        parent=owner(records,output)
        if parent is not None:
            root=parent.directory_group.relative_path if parent.directory_group is not None else parent.relative_path
            if root not in groups:raise ValueError('Mutable output has no validated current forest')
            result[output.relative_path]=root
        elif any(child(output.relative_path,root) or child(root,output.relative_path) or root==output.relative_path for root in groups):
            raise ValueError('Output has no writable view of its current forest')
    return result


def startup_states(snapshot,states):
    checkpoint_owners(snapshot)
    records=checkpoint_records(snapshot)
    return {root:startup_tree(records,root,tree) for root,tree in states.items()}
