"""Complete local directory identities and resumable authorized cleanup.

Only the journal owner may authorize deletion. Evidence binds every member;
retries accept only unchanged subsets of a previously authorized tree.
"""
from pathlib import Path
from ..validation._cooperative import checkpoint_scope


def state(path, *, limits=None, checkpoint=None):
    from ._directory_tree import _scan, DirectoryLimits
    with checkpoint_scope(checkpoint):
        tree, identities = _scan(Path(path), limits or DirectoryLimits())
    rows = [('', 'directory', identities[''][:2])]
    for item in tree.entries:
        row = (item.path, item.kind, identities[item.path][:2])
        rows.append(row + ((item.sha256,item.size) if item.kind=='file' else ()))
    return tuple(sorted(rows))


def remaining(path, evidence, *, checkpoint=None):
    from ._directory_tree import DirectoryLimits
    rows = {r[0]:(r[0],r[1],tuple(r[2]),*r[3:]) for r in evidence}
    limits = DirectoryLimits(total_bytes=max(1,sum(r[4] for r in evidence if r[1]=='file')),
        entries=max(1,len(rows)-1),depth=max(1,max(len(n.split('/')) for n in rows)))
    observed = state(path,limits=limits,checkpoint=checkpoint)
    if any(rows.get(row[0])!=row for row in observed):
        raise ValueError(f'Directory changed after cleanup was authorized: {path}')
    return observed


def remove(path, evidence, *, checkpoint=None):
    from ._workspace import remove_owned_tree
    path=Path(path)
    if not path.exists() and not path.is_symlink():return
    remaining(path,evidence,checkpoint=checkpoint)
    remove_owned_tree(path,parent=path.parent,identity=tuple(evidence[0][2]))
