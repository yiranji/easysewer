"""Keep explicitly nested output consumers within one published directory tree."""
from dataclasses import dataclass, field
from pathlib import Path
from ._directory_tree import relative_path
from ..validation._cooperative import checkpointed


@dataclass(frozen=True)
class OutputLayout:
    relative: dict
    targets: tuple
    children: frozenset
    mutable: dict = field(default_factory=dict)


def layout(plans,asset_name):
    active={i:p for i,p in enumerate(plans) if p.use.active and p.use.access=='write' and p.use.role!='swmm:temporary_directory'}
    directories={i:p.original for i,p in active.items() if p.use.kind=='directory' and p.original is not None}
    mutable_roots={i:p.original.resolve() for i,p in enumerate(plans) if p.use.active and p.use.access=='read_write'
        and p.use.kind=='directory' and p.use.role!='swmm:temporary_directory' and p.original is not None and p.directory_adapter is not None}
    if not directories and not mutable_roots:return OutputLayout({},tuple((p.original,p.use.kind=='directory') for p in active.values()),frozenset())
    # Preserve the declared lexical path until OutputTransaction checks links.
    # Resolved paths are used only to recognize ownership and relative members.
    resolved={i:p.original.resolve() for i,p in active.items() if p.original is not None}
    seen={}
    for i,path in checkpointed(resolved.items()):
        if path in seen:raise ValueError('Output consumers claim the same destination')
        seen[path]=i
    mutable={}
    for i,path in checkpointed(resolved.items()):
        parents=[j for j,root in mutable_roots.items() if path!=root and path.is_relative_to(root)]
        if parents:
            mutable[i]=min(parents,key=lambda j:len(mutable_roots[j].parts))
            # Native file readers, inactive consumers, equal roots and inverse containment remain protected.
            for j,other in checkpointed(enumerate(plans)):
                if j==i or other.original is None or other.use.role=='swmm:temporary_directory':continue
                target=other.original.resolve()
                overlap=path==target or active[i].use.kind=='directory' and target.is_relative_to(path)
                if not overlap and active[i].use.kind=='file' and other.use.kind=='file' and path.exists() and target.exists():
                    overlap=path.samefile(target)
                nested_writer=active[i].use.kind=='directory' and other.use.active and other.use.access=='write' and target!=path and target.is_relative_to(path)
                if overlap and not nested_writer:raise ValueError('Mutable output overlaps another declared consumer')
                if other.use.kind=='directory' and path.is_relative_to(target) and other.use.access!='write':
                    if not other.use.active or not any(target.is_relative_to(root) or root.is_relative_to(target) for root in mutable_roots.values()) and other.use.access!='read_write':
                        raise ValueError('Mutable output overlaps a protected directory view')
    parents={}
    for i,path in checkpointed(resolved.items()):
        ancestors=[j for j in directories if j!=i and path.is_relative_to(resolved[j])]
        if ancestors:parents[i]=min(ancestors,key=lambda j:len(resolved[j].parts))
    relative={};namespace={}
    def claim(path,kind):
        name=path.as_posix();relative_path(name);old=namespace.setdefault(name.casefold(),(name,kind))
        if old!=(name,kind):raise ValueError('Output directory members alias or conflict on a portable filesystem')
    for i,p in checkpointed(active.items()):
        if i in mutable:continue
        owner=parents.get(i,i)
        base=Path(asset_name)/'outputs'/(f'r{owner}.dir' if active[owner].use.kind=='directory' else f'r{owner}.dat')
        relative[i]=base/resolved[i].relative_to(resolved[owner]) if i in parents else base
        claim(relative[i],p.use.kind)
        for parent in relative[i].parents:
            if parent!=Path('.'):claim(parent,'directory')
    return OutputLayout(relative,tuple((p.original,p.use.kind=='directory') for i,p in active.items() if i not in parents and i not in mutable),frozenset(set(parents)|set(mutable)),mutable)
