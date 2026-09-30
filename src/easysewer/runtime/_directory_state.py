"""Separate original directory evidence from shared writable execution trees."""
from pathlib import Path
from ._directory_tree import relative_path, DirectoryManifest, DirectoryLimits, inspect_tree, _node
from ..validation._cooperative import checkpointed, checkpoint_scope, checkpoint as work_checkpoint


def initial_path(resource):
    return resource.initial_relative_path or resource.relative_path


def mutable_groups(records):
    """Validate portable ownership without reading the host filesystem.

    All consumers sharing a mutable execution tree retain the same independent
    initial evidence. A read consumer sees the same tree as its read_write peer.
    Caller resources never acquire write permission from this relationship.
    """
    from ._directory_graph import resource_groups
    graph_groups=resource_groups(records)
    groups = {}
    for record in checkpointed(records):
        if record.directory_group is None and record.kind == 'directory' and record.active and record.access != 'write':
            groups.setdefault(record.relative_path, []).append(record)
    result = {}
    for path, group in checkpointed(groups.items()):
        if not any(r.initial_relative_path is not None for r in checkpointed(group)):
            continue
        first = group[0]
        if (first.initial_relative_path is None or not any(r.access == 'read_write' for r in checkpointed(group)) or
                any((r.initial_relative_path, r.tree) != (first.initial_relative_path, first.tree) for r in checkpointed(group))):
            raise ValueError('Shared mutable directory requires matching independent initial evidence and a writable consumer')
        relative_path(path); relative_path(first.initial_relative_path)
        result[path] = first
    def overlap(a, b):
        a, b = a.casefold(), b.casefold()
        return a == b or a.startswith(b+'/') or b.startswith(a+'/')
    for path, first in checkpointed(result.items()):
        original = first.initial_relative_path
        for record in checkpointed(records):
            other = record.relative_path
            from ._mutable_outputs import owner
            output_owner=owner(records,record)
            owned=output_owner is not None and output_owner.relative_path==path
            if other is not None and (overlap(original, other) or
                    other != path and overlap(path, other) and not owned):
                raise ValueError('Mutable directory execution/initial paths overlap another resource')
        if overlap(original, 'model.inp') or overlap(path, 'model.inp'):
            raise ValueError('Mutable directory overlaps executed input')
        for other, record in checkpointed(result.items()):
            if other != path and overlap(original, record.initial_relative_path):
                raise ValueError('Independent initial directory paths overlap')
    for group in graph_groups.values():
        if group.initial_relative_path is not None:result[group.relative_path]=group
    return result


def states_for(records, states=None):
    groups = mutable_groups(records)
    if states is None:
        return {path:record.tree for path,record in groups.items()}
    if type(states) is not dict or set(states) != set(groups):
        raise ValueError('Mutable directory state must cover exactly the declared execution trees')
    if any(tree is not None and type(tree) is not DirectoryManifest for tree in checkpointed(states.values())):
        raise TypeError('Mutable directory state requires complete DirectoryManifest evidence')
    from ._directory_graph import resource_groups, DirectoryGraphState
    for group in resource_groups(records).values():
        if group.initial_relative_path is not None:
            DirectoryGraphState(layout=group.state.layout,tree=states[group.relative_path])
    return states


def verify_absent(path, *, checkpoint=None):
    """Only a missing path is absent; links, non-directories and IO failures aren't."""
    with checkpoint_scope(checkpoint):
        path = Path(path); work_checkpoint()
        try:
            path.lstat()
        except FileNotFoundError:
            parent = path.parent
            while True:
                work_checkpoint()
                try:
                    _node(parent, 'directory')
                except FileNotFoundError:
                    if parent == parent.parent:raise
                    parent = parent.parent
                else:
                    return None
        raise ValueError('Directory state expected absence but a filesystem entry exists: '+str(path))


def observe(path, *, allow_absent=False, limits=DirectoryLimits(), checkpoint=None):
    if type(allow_absent) is not bool:raise TypeError('allow_absent must be a boolean')
    if checkpoint is not None:checkpoint()
    try:
        Path(path).lstat()
    except FileNotFoundError:
        if not allow_absent:raise
        return verify_absent(path,checkpoint=checkpoint)
    return inspect_tree(path,limits=limits,checkpoint=checkpoint)
