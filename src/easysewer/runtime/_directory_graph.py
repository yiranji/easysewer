"""Private multi-root directory graph planning and capture.

Only declared source roots are traversed. A graph layout records consumer views
inside a forest; the forest manifest binds aliases across roots as well as
within them. File and directory views bind staged resources and portable group evidence.
Public Runner/worker integration is handled separately.
"""
from dataclasses import dataclass, replace
from pathlib import Path
import os

from ._directory_tree import (DirectoryLimits, DirectoryEntry, DirectoryManifest,
    _node, _stamp, _scan, _file_digest, relative_path, inspect_tree, verify_tree, link_member)
from ._directory_state import verify_absent
from ._workspace import copy_input
from ..validation._cooperative import checkpoint_scope, checkpoint as work_checkpoint, checkpointed


@dataclass(frozen=True, kw_only=True)
class DirectoryRequest:
    key: str
    source: Path
    access: str
    required: bool
    limits: DirectoryLimits = DirectoryLimits()
    kind: str = 'directory'

    def __post_init__(self):
        if type(self.key) is not str or not self.key:
            raise ValueError('Directory graph requires a consumer identity')
        if not isinstance(self.source, Path) or not self.source.is_absolute():
            raise TypeError('Directory graph sources must be explicit absolute host paths')
        if self.access not in ('read', 'read_write') or type(self.required) is not bool:
            raise ValueError('Directory graph input access or requirement is invalid')
        if self.kind not in ('directory','file'):raise ValueError('Invalid resource graph kind')
        if type(self.limits) is not DirectoryLimits:raise TypeError('Expected DirectoryLimits')


@dataclass(frozen=True, kw_only=True)
class DirectoryView:
    key: str
    path: str
    access: str
    required: bool
    limits: DirectoryLimits
    kind: str = 'directory'

    def __post_init__(self):
        relative_path(self.path)
        if type(self.key) is not str or not self.key:raise ValueError('Missing consumer identity')
        if self.access not in ('read', 'read_write') or type(self.required) is not bool:
            raise ValueError('Invalid consumer view policy')
        if self.kind not in ('directory','file'):raise ValueError('Invalid resource view kind')
        if type(self.limits) is not DirectoryLimits:raise TypeError('Expected view limits')


@dataclass(frozen=True, kw_only=True)
class DirectoryLayout:
    roots: tuple[str, ...]
    views: tuple[DirectoryView, ...]
    limits: DirectoryLimits

    def __post_init__(self):
        if type(self.roots) is not tuple or not self.roots or tuple(sorted(set(self.roots))) != self.roots:
            raise ValueError('Graph roots must be a nonempty sorted unique tuple')
        for name in self.roots:
            if len(relative_path(name).parts) != 1:raise ValueError('Graph roots must be single portable names')
        if len({name.casefold() for name in self.roots}) != len(self.roots):raise ValueError('Graph root names collide')
        if type(self.views) is not tuple or not self.views or any(type(v) is not DirectoryView for v in self.views):
            raise TypeError('Graph views must be immutable consumer declarations')
        paths={}
        for view in checkpointed(self.views):
            if paths.setdefault(view.path,view.kind)!=view.kind:raise ValueError('Shared view kinds disagree')
        for view in checkpointed(self.views):
            for parent in relative_path(view.path).parents:
                if paths.get(parent.as_posix())=='file':raise ValueError('File view cannot contain another consumer')
        keys=[v.key for v in self.views]
        if len(set(keys)) != len(keys):raise ValueError('Graph consumer identities must be unique')
        if any(v.path.split('/')[0] not in self.roots for v in self.views):raise ValueError('View escapes declared graph roots')
        if {v.path for v in self.views if '/' not in v.path} != set(self.roots):raise ValueError('Every forest root requires a declared consumer')
        if type(self.limits) is not DirectoryLimits:raise TypeError('Expected graph limits')

    @property
    def mutable(self):
        return any(v.access == 'read_write' for v in self.views)

    def view(self, key):
        for view in self.views:
            if view.key == key:return view
        raise KeyError(key)


def _bound(manifest, limits):
    if type(manifest) is DirectoryEntry:
        if manifest.size > limits.total_bytes:raise ValueError('File graph view exceeds byte budget')
        return
    if manifest.total_bytes > limits.total_bytes or len(manifest.entries) > limits.entries or any(
            len(e.path.split('/')) > limits.depth for e in checkpointed(manifest.entries)):
        raise ValueError('Directory graph or consumer view exceeds its budget')


def view_manifest(tree, path, kind='directory'):
    """Project a forest onto one consumer without confusing external aliases."""
    relative_path(path)
    root = next((e for e in checkpointed(tree.entries) if e.path == path), None)
    if root is None:
        # A missing child under an actual file is an invalid view, not absence.
        parts=path.split('/')
        parents={'/'.join(parts[:i]) for i in range(1,len(parts))}
        if any(e.path in parents and e.kind != 'directory' for e in checkpointed(tree.entries)):
            raise ValueError('Directory view has a non-directory parent')
        return None
    if root.kind != kind:raise ValueError('Resource view changed kind')
    if kind=='file':return replace(root,path='file',hardlink_to=None)
    prefix=path+'/';entries=[];canonical={};linked=False
    for entry in checkpointed(tree.entries):
        if not entry.path.startswith(prefix):continue
        name=entry.path[len(prefix):];target=None
        if entry.kind == 'file':
            identity=entry.hardlink_to or entry.path
            first=canonical.setdefault(identity,name)
            if first != name:target=first;linked=True
        entries.append(replace(entry,path=name,hardlink_to=target))
    return DirectoryManifest(entries=tuple(entries),contract='easysewer:directory-tree:2' if linked else 'easysewer:directory-tree:1')


@dataclass(frozen=True, kw_only=True)
class DirectoryGraphState:
    layout: DirectoryLayout
    tree: DirectoryManifest

    def __post_init__(self):
        if type(self.layout) is not DirectoryLayout or type(self.tree) is not DirectoryManifest:
            raise TypeError('Directory graph requires explicit layout and forest evidence')
        for entry in checkpointed(self.tree.entries):
            if entry.path.split('/')[0] not in self.layout.roots:raise ValueError('Forest contains an undeclared root')
            if '/' not in entry.path and not any(v.path==entry.path and v.kind==entry.kind for v in self.layout.views):raise ValueError('Forest root differs from its declared kind')
        _bound(self.tree,self.layout.limits)
        for view in checkpointed(self.layout.views):
            manifest=view_manifest(self.tree,view.path,view.kind)
            if manifest is not None:_bound(manifest,view.limits)

    def view(self, key):
        view=self.layout.view(key)
        return view_manifest(self.tree,view.path,view.kind)


@dataclass(frozen=True, kw_only=True)
class _Observation:
    path: Path
    tree: DirectoryManifest | DirectoryEntry | None
    identities: tuple


@dataclass(frozen=True, kw_only=True)
class DirectoryGraphPlan:
    state: DirectoryGraphState
    sources: tuple[tuple[str, _Observation], ...]
    observations: tuple[_Observation, ...]


def _parents(path):
    # Check declared spelling before resolve, so parent reparse points cannot
    # hide beneath an ordinary final directory. Missing ancestors are allowed.
    for parent in reversed((path,*path.parents)):
        work_checkpoint()
        try:_node(parent,'directory')
        except FileNotFoundError:continue


def _observe(path, limits, kind='directory'):
    _parents(path if kind=='directory' else path.parent)
    try:_,info=_node(path,kind)
    except FileNotFoundError:
        verify_absent(path)
        return _Observation(path=path,tree=None,identities=())
    if kind=='file':
        stamp=_stamp(info)
        if not info.st_ino:raise ValueError('Resource topology requires stable nonzero file identities')
        if info.st_size>limits.total_bytes:raise ValueError('File graph view exceeds byte budget')
        digest,size=_file_digest(path,stamp)
        return _Observation(path=path,tree=DirectoryEntry(path='file',kind='file',sha256=digest,size=size),identities=(('',stamp),))
    tree,identities=_scan(path,limits)
    return _Observation(path=path,tree=tree,identities=tuple(sorted(identities.items())))


def _observe_once(observations, path, limits, kind):
    # Consumers with the same source and limits share one observation within
    # this phase. Callers create a fresh map for every verification, so checks
    # before and after copying still read the source again.
    work_checkpoint()
    key=(path,limits,kind)
    if key not in observations:observations[key]=_observe(path,limits,kind)
    return observations[key]


def _forest(sources):
    entries=[];identities={}
    for name,observation in checkpointed(sources):
        if observation.tree is None:continue
        stamps=dict(observation.identities)
        if type(observation.tree) is DirectoryEntry:
            entries.append(replace(observation.tree,path=name));identities[name]=stamps[''][:2];continue
        entries.append(DirectoryEntry(path=name,kind='directory'))
        for entry in checkpointed(observation.tree.entries):
            path=name+'/'+entry.path;entries.append(replace(entry,path=path,hardlink_to=None))
            if entry.kind=='file':identities[path]=stamps[entry.path][:2]
    canonical={};ordered=[];linked=False
    for entry in checkpointed(sorted(entries,key=lambda e:e.path)):
        if entry.kind=='file':
            first=canonical.setdefault(identities[entry.path],entry.path)
            if first!=entry.path:entry=replace(entry,hardlink_to=first);linked=True
        ordered.append(entry)
    return DirectoryManifest(entries=tuple(ordered),contract='easysewer:directory-tree:2' if linked else 'easysewer:directory-tree:1')


def plan_graphs(requests, *, limits=DirectoryLimits(), checkpoint=None):
    """Group nested or file-sharing roots; unconnected resources stay separate.

    Each consumer is independently observed with its own limits. The forest
    budget counts top-root paths once, not once per overlapping consumer view.
    Grouping never reads the common ancestor or unrelated sibling directories.
    """
    with checkpoint_scope(checkpoint):
        if type(requests) is not tuple or any(type(r) is not DirectoryRequest for r in requests):
            raise TypeError('Directory requests must be an immutable tuple')
        if type(limits) is not DirectoryLimits:raise TypeError('Expected graph limits')
        if len(requests)>limits.entries:raise ValueError('Directory consumer inventory exceeds entry budget')
        if len({r.key for r in requests})!=len(requests):raise ValueError('Duplicate consumer identity')
        observations=[];observed_sources={}
        for request in checkpointed(requests):
            observed=_observe_once(observed_sources,request.source,request.limits,request.kind)
            if request.required and observed.tree is None:raise FileNotFoundError('Required directory is absent: '+str(request.source))
            observations.append(observed)
        paths=[o.path.resolve() for o in observations];parent=list(range(len(requests)))
        def find(i):
            while parent[i]!=i:i=parent[i]
            return i
        def join(a,b):
            a,b=find(a),find(b)
            if a!=b:parent[max(a,b)]=min(a,b)
        seen={}; declared={}
        for index,path in checkpointed(enumerate(paths)):
            first=declared.setdefault(path,index);join(index,first)
        for index,observation in checkpointed(enumerate(observations)):
            for ancestor in checkpointed(paths[index].parents):
                if ancestor in declared:join(index,declared[ancestor])
            if observation.tree is not None:
                stamps=dict(observation.identities)
                if type(observation.tree) is DirectoryEntry:
                    identity=stamps[''][:2];first=seen.setdefault(identity,index);join(index,first)
                for entry in checkpointed(observation.tree.entries if type(observation.tree) is DirectoryManifest else ()):
                    if entry.kind=='file':
                        identity=stamps[entry.path][:2]
                        first=seen.setdefault(identity,index);join(index,first)
        groups={}
        for i in checkpointed(range(len(requests))):groups.setdefault(find(i),[]).append(i)
        plans=[]
        for members in checkpointed(groups.values()):
            top=[]
            for i in checkpointed(members):
                if declared[paths[i]] != i:continue
                if any(p in declared for p in checkpointed(paths[i].parents)):continue
                top.append(i)
            top_by_path={paths[i]:i for i in top}
            roots=tuple('r'+str(i).zfill(6) for i in top);sources=tuple(zip(roots,(observations[i] for i in top)))
            views=[]
            for i in checkpointed(members):
                ancestor=next(top_by_path[p] for p in checkpointed((paths[i],*paths[i].parents)) if p in top_by_path);suffix=paths[i].relative_to(paths[ancestor]).as_posix();name='r'+str(ancestor).zfill(6)
                view=DirectoryView(key=requests[i].key,path=name if suffix=='.' else name+'/'+suffix,access=requests[i].access,required=requests[i].required,limits=requests[i].limits,kind=requests[i].kind);views.append(view)
            state=DirectoryGraphState(layout=DirectoryLayout(roots=roots,views=tuple(views),limits=limits),tree=_forest(sources))
            for i,view in zip(members,views):
                if state.view(view.key)!=observations[i].tree:raise ValueError('Overlapping directory observations disagree')
            plans.append(DirectoryGraphPlan(state=state,sources=sources,observations=tuple(observations[i] for i in members)))
        # Re-observation also catches a shared alias changed between consumers.
        for plan in checkpointed(plans):verify_plan(plan)
        return tuple(plans)


def verify_plan(plan, *, checkpoint=None):
    with checkpoint_scope(checkpoint):
        if type(plan) is not DirectoryGraphPlan:raise TypeError('Expected DirectoryGraphPlan')
        observed_sources={}
        for observation,view in checkpointed(zip(plan.observations,plan.state.layout.views)):
            actual=_observe_once(observed_sources,observation.path,view.limits,view.kind)
            if actual!=observation:raise ValueError('Declared resource graph changed after observation')


def inspect_graph(root, layout, *, checkpoint=None):
    with checkpoint_scope(checkpoint):
        if type(layout) is not DirectoryLayout:raise TypeError('Expected DirectoryLayout')
        return DirectoryGraphState(layout=layout,tree=inspect_tree(root,limits=layout.limits))


def capture_graph(plan, target, *, checkpoint=None):
    """Create one private forest, preserving all declared consumer relationships.

    Partial newly created output belongs to the caller on failure. No source
    directory, external alias or existing destination is modified or deleted.
    """
    with checkpoint_scope(checkpoint):
        if type(plan) is not DirectoryGraphPlan:raise TypeError('Expected DirectoryGraphPlan')
        target=Path(target).absolute();resolved=target.resolve();_node(target.parent,'directory')
        for observation in checkpointed(plan.observations):
            source=observation.path.resolve()
            if source.is_relative_to(resolved) or resolved.is_relative_to(source):raise ValueError('Directory graph capture overlaps a declared source')
        verify_plan(plan);work_checkpoint();target.mkdir();directories={'':_stamp(_node(target,'directory')[1])[:2]};files={};sources=dict(plan.sources);source_stamps={name:dict(source.identities) for name,source in plan.sources}
        for entry in checkpointed(plan.state.tree.entries,interval=1):
            parts=relative_path(entry.path).parts
            for count in range(len(parts)):
                name='/'.join(parts[:count]);parent=target.joinpath(*parts[:count])
                if _stamp(_node(parent,'directory')[1])[:2]!=directories[name]:raise ValueError('Private forest parent identity changed')
            rootname=parts[0];source=sources[rootname];local='/'.join(parts[1:]);stamps=source_stamps[rootname]
            for count in range(len(parts)):
                key='/'.join(parts[1:count+1]);path=source.path.joinpath(*parts[1:count+1])
                if _stamp(_node(path)[1])!=stamps[key]:raise ValueError('Source forest member changed during capture')
            destination=target/entry.path
            if entry.kind=='directory':destination.mkdir();directories[entry.path]=_stamp(_node(destination,'directory')[1])[:2]
            elif entry.hardlink_to is not None:link_member(target,entry,expected_identity=files[entry.hardlink_to])
            else:
                created=[]
                if copy_input(source.path/local,destination,checkpoint=work_checkpoint,on_create=created.append,expected_size=entry.size)!=(entry.sha256,entry.size):raise ValueError('Source forest bytes changed during capture')
                files[entry.path]=created[0]
        verify_plan(plan)
        if _stamp(_node(target,'directory')[1])[:2]!=directories['']:raise ValueError('Private forest root identity changed')
        actual=inspect_graph(target,plan.state.layout)
        if actual!=plan.state:raise ValueError('Captured directory graph differs from source evidence')
        return actual



def consumer_key(owner, field):
    """Portable identity, bound to the actual declared owner and field."""
    import json
    from ..model.identity import Ref
    if type(owner) is not Ref or type(field) is not tuple or any(type(v) not in (str,int) for v in field):
        raise TypeError('Directory graph consumer requires owner and field')
    owner=owner.canonical
    return json.dumps([owner.collection,owner.key,list(field)],ensure_ascii=True,separators=(',',':'))


@dataclass(frozen=True, kw_only=True)
class DirectoryGroupSnapshot:
    key: str
    relative_path: str
    initial_relative_path: str | None
    state: DirectoryGraphState

    def __post_init__(self):
        if len(relative_path(self.key).parts)!=1:raise ValueError('Group key must be a portable name')
        relative_path(self.relative_path)
        if type(self.state) is not DirectoryGraphState:raise TypeError('Expected graph state')
        if self.state.layout.mutable != (self.initial_relative_path is not None):
            raise ValueError('Mutable graph requires independent initial evidence')
        if self.initial_relative_path is not None:
            relative_path(self.initial_relative_path)
            if _overlap(self.relative_path,self.initial_relative_path):raise ValueError('Initial and current graph roots overlap')
        for view in checkpointed(self.state.layout.views):
            if view.required and self.state.view(view.key) is None:raise ValueError('Required initial graph view is absent')

    @property
    def tree(self):return self.state.tree


def _overlap(a,b):
    a,b=a.casefold(),b.casefold()
    return a==b or a.startswith(b+'/') or b.startswith(a+'/')


def validate_member(resource):
    group=resource.directory_group
    if group is None:return
    if type(group) is not DirectoryGroupSnapshot or resource.kind not in ('directory','file') or not resource.active or resource.access not in ('read','read_write'):
        raise ValueError('Graph evidence requires an active directory input')
    try:view=group.state.layout.view(consumer_key(resource.owner,resource.field))
    except KeyError:raise ValueError('Resource owner/field is absent from its directory graph') from None
    if (view.access,view.required,view.kind)!=(resource.access,resource.required,resource.kind):raise ValueError('Graph consumer policy differs from resource')
    if resource.relative_path!=group.relative_path+'/'+view.path:raise ValueError('Resource path differs from graph view')
    initial=None if group.initial_relative_path is None else group.initial_relative_path+'/'+view.path
    evidence=group.state.view(view.key)
    if resource.initial_relative_path!=initial:raise ValueError('Resource initial path differs from its graph view')
    if view.kind=='directory':
        if resource.tree!=evidence:raise ValueError('Resource initial tree differs from its graph view')
    elif resource.tree is not None or (resource.sha256,resource.size)!=((evidence.sha256,evidence.size) if evidence is not None else (None,None)):
        raise ValueError('Resource initial file bytes differ from its graph view')


def resource_groups(records):
    groups={};members={}
    for resource in checkpointed(records):
        group=resource.directory_group
        if group is None:continue
        validate_member(resource)
        if groups.setdefault(group.key,group)!=group:raise ValueError('Shared graph records disagree')
        key=consumer_key(resource.owner,resource.field)
        if key in members.setdefault(group.key,set()):raise ValueError('Duplicate directory graph consumer')
        members[group.key].add(key)
    for key,group in checkpointed(groups.items()):
        if members[key]!={v.key for v in group.state.layout.views}:raise ValueError('Directory graph must cover exactly its consumers')
        for root in (group.relative_path,group.initial_relative_path):
            if root is None:continue
            if _overlap(root,'model.inp'):raise ValueError('Directory graph overlaps executed input')
            for other in checkpointed(groups.values()):
                if other.key==key:continue
                if any(path is not None and _overlap(root,path) for path in (other.relative_path,other.initial_relative_path)):
                    raise ValueError('Directory group roots overlap')
            for resource in checkpointed(records):
                if resource.directory_group is not None:continue
                from ._mutable_outputs import owner
                output_owner=owner(records,resource)
                if root==group.relative_path and output_owner is not None and output_owner.directory_group==group:continue
                if any(path is not None and _overlap(root,path) for path in (resource.relative_path,resource.initial_relative_path)):
                    raise ValueError('Directory graph overlaps an ungrouped resource')
    return groups



def execution_graphs(plans, *, checkpoint=None):
    with checkpoint_scope(checkpoint):return _execution_graphs(plans)


def _execution_graphs(plans):
    """Keep mutable executions in one forest to bind later cross-root aliases.

    Initial connectivity alone cannot predict hardlinks introduced by a
    consumer during execution. All declared directory views share one private
    forest when any is writable; unrelated initial files remain independent.
    Read-only-only plans retain their smaller connected groups.
    """
    if type(plans) is not tuple or any(type(p) is not DirectoryGraphPlan for p in plans):
        raise TypeError('Expected immutable directory graph plans')
    if len(plans)<2 or not any(p.state.layout.mutable for p in plans):return plans
    sources=tuple(sorted((pair for p in checkpointed(plans) for pair in p.sources),key=lambda pair:pair[0]))
    if len({name for name,_ in sources})!=len(sources):raise ValueError('Execution graph roots are not globally unique')
    pairs=[(view,observation) for p in checkpointed(plans) for view,observation in zip(p.state.layout.views,p.observations)]
    pairs.sort(key=lambda pair:pair[0].key)
    limits=plans[0].state.layout.limits
    if any(p.state.layout.limits!=limits for p in plans):raise ValueError('Execution graph plans have incompatible aggregate budgets')
    layout=DirectoryLayout(roots=tuple(name for name,_ in sources),views=tuple(v for v,_ in pairs),limits=limits)
    state=DirectoryGraphState(layout=layout,tree=_forest(sources))
    for view,observation in checkpointed(pairs):
        if state.view(view.key)!=observation.tree:raise ValueError('Merged graph changes a declared consumer view')
    plan=DirectoryGraphPlan(state=state,sources=sources,observations=tuple(o for _,o in pairs));verify_plan(plan)
    return (plan,)
