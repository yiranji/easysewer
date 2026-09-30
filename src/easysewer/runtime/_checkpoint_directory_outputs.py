"""Checkpoint write-only forests and explicit native stream/member bindings.

Output content is separate from immutable startup inputs. A restored native
stream opens an independent prepared file through a private hardlink; publishing
the prepared directory retains that stream's identity at its declared path.
"""
from pathlib import Path
from ._result_codec import Codec, exact
from ._directory_tree import DirectoryManifest, DirectoryLimits, relative_path, _node
from ._directory_state import observe
from ..validation._cooperative import checkpointed

VERSION='1.7'
MUTABLE_VERSION='1.8'
VERSIONS=(VERSION,MUTABLE_VERSION)
BASE_VERSIONS=('1.0','1.1','1.2','1.3','1.4','1.5','1.6')
EXTRA=('base_schema_version','output_directory_states','output_directory_bindings')


def unwrap(data):
    if data.get('schema_version') not in VERSIONS:return data
    if not all(key in data for key in EXTRA) or data['base_schema_version'] not in BASE_VERSIONS:
        raise ValueError('Invalid output-directory checkpoint base format')
    base={key:value for key,value in data.items() if key not in EXTRA}
    base['schema_version']=data['base_schema_version']
    return base


def standalone_roots(snapshot):
    from ._checkpoint_container import _snapshot_trees, _snapshot_files
    from ._directory_state import mutable_groups
    from ._mutable_outputs import checkpoint_owners
    owned=checkpoint_owners(snapshot)
    names=[];spellings={}
    for resource in checkpointed(snapshot.resources):
        if resource.kind=='directory' and resource.active and resource.access=='write' and resource.role!='swmm:temporary_directory':
            name=resource.relative_path;path=relative_path(name)
            if name in owned:continue
            if name in names:raise ValueError('Duplicate checkpoint output directory')
            names.append(name)
            for node in (path,*path.parents):
                if node==Path('.'):continue
                text=node.as_posix()
                if spellings.setdefault(text.casefold(),text)!=text:raise ValueError('Output directories have conflicting portable spelling')
    if not names:return ()
    forbidden=set(_snapshot_trees(snapshot))|set(_snapshot_files(snapshot))|set(mutable_groups(snapshot.resources))|{'model.inp','model.rpt','model.out'}
    forbidden.update(r.relative_path for r in snapshot.resources if r.access!='write')
    forbidden.update(r.relative_path for r in snapshot.resources if r.kind=='directory' and r.role=='swmm:temporary_directory')
    for name in names:
        for other in forbidden:
            if overlaps(name,other):raise ValueError('Checkpoint output directory overlaps an input or scratch directory')
    return tuple(sorted(name for name in names if not any(name.startswith(other+'/') for other in names if other!=name)))


def roots(snapshot):
    from ._mutable_outputs import checkpoint_owners
    return tuple(sorted(set(standalone_roots(snapshot))|set(checkpoint_owners(snapshot).values())))


def overlaps(a,b):
    a,b=a.casefold(),b.casefold()
    return a==b or a.startswith(b+'/') or b.startswith(a+'/')


def owner(names,path):
    relative_path(path)
    return next((name for name in names if path.startswith(name+'/')),None)


def expected_bindings(snapshot,locations):
    names=roots(snapshot)
    return dict(outputs=[dict(index=item['index'],relative_path=item['relative_path']) for item in locations['outputs']
                         if owner(names,item['relative_path']) is not None],
                trace=locations['trace'] if locations['trace'] is not None and owner(names,locations['trace']) is not None else None)


def capture(snapshot,blobs,checkpoint):
    from ._checkpoint_container import _tree_files, _verify_directory_states
    from ._checkpoint_directory_inputs import _parents
    workspace=Path(snapshot.execution_directory).resolve(strict=True);states={}
    for name in standalone_roots(snapshot):
        path=workspace/relative_path(name);_parents(workspace,path)
        tree=observe(path,allow_absent=True,limits=DirectoryLimits(total_bytes=blobs.limits.total_bytes),checkpoint=checkpoint)
        states[name]=tree
        for relative,desc in _tree_files(name,tree).items():blobs.put_file(workspace/relative_path(relative),desc)
    _verify_directory_states(workspace,states,checkpoint)
    return states


def extend(data,snapshot,states,locations,outputs,trace,blobs):
    if not roots(snapshot):return data
    if locations is None:raise ValueError('Output-directory capture requires validated startup stream locations')
    bindings=expected_bindings(snapshot,locations)
    workspace=Path(snapshot.execution_directory).resolve(strict=True);active={item.index:item for item in outputs};seen=set()
    def linked(relative,path):
        target=workspace/relative_path(relative)
        from ._checkpoint_directory_inputs import _parents
        _parents(workspace,target);info=_node(target,'file')[1];live=_node(Path(path),'file')[1]
        if (info.st_dev,info.st_ino)!=(live.st_dev,live.st_ino) or (info.st_dev,info.st_ino) in seen:
            raise ValueError('Native output binding differs from its declared directory member')
        seen.add((info.st_dev,info.st_ino))
    for item in bindings['outputs']:linked(item['relative_path'],active[item['index']].path)
    if bindings['trace'] is not None:
        if trace is None:raise ValueError('Bound output trace is missing')
        linked(bindings['trace'],trace)
    from ._mutable_outputs import checkpoint_owners
    version=MUTABLE_VERSION if checkpoint_owners(snapshot) else VERSION
    result=dict(data,base_schema_version=data['schema_version'],schema_version=version,
        output_directory_states=[dict(relative_path=name,tree=Codec(blobs,result_version='1.9').encode(tree)) for name,tree in sorted(states.items())],
        output_directory_bindings=bindings)
    decode(result,snapshot,blobs)
    return result


def decode(data,snapshot,blobs=None):
    if data.get('schema_version') not in VERSIONS:return {},None
    unwrap(data);names=roots(snapshot)
    from ._mutable_outputs import checkpoint_owners
    owned=checkpoint_owners(snapshot)
    if bool(owned)!=(data['schema_version']==MUTABLE_VERSION):raise ValueError('Checkpoint version does not bind mutable output ownership')
    if not names:raise ValueError('Output-directory checkpoint must declare an output tree')
    entries=data['output_directory_states']
    if type(entries) is not list:raise TypeError('Expected output directory states')
    states={};order=[];members={};identities={}
    from ._checkpoint_container import descriptor
    for entry in checkpointed(entries):
        exact(entry,('relative_path','tree'));name=entry['relative_path'];relative_path(name);order.append(name)
        tree=Codec(blobs,result_version='1.9').decode(entry['tree'])
        if tree is not None and type(tree) is not DirectoryManifest:raise TypeError('Expected output directory manifest or absence')
        states[name]=tree
        for member in checkpointed(tree.entries if tree is not None else ()):
            if member.kind=='file':
                desc=dict(sha256=member.sha256,size=member.size);key=name+'/'+member.path;members[key]=desc;identities[key]=name+'/'+(member.hardlink_to or member.path)
                if blobs is not None:blobs.get_path(*descriptor(desc))
    if tuple(order)!=standalone_roots(snapshot):raise ValueError('Output states must exactly match sorted outer output directory roots')
    combined=dict(states)
    if owned:
        from ._checkpoint_container import _decode_directory_states
        current=_decode_directory_states(data,snapshot,blobs)
        for name in sorted(set(owned.values())):
            tree=current[name];combined[name]=tree
            for entry in checkpointed(tree.entries if tree is not None else ()):
                if entry.kind=='file':
                    key=name+'/'+entry.path;members[key]=dict(sha256=entry.sha256,size=entry.size);identities[key]=name+'/'+(entry.hardlink_to or entry.path)
    
    declared={r.relative_path for r in snapshot.resources if r.kind=='file' and r.active and r.access=='write'}|{'model.rpt','model.out'}
    kinds={name:('absent' if tree is None else 'directory') for name,tree in combined.items()}
    kinds.update((name+'/'+entry.path,entry.kind) for name,tree in combined.items() if tree is not None for entry in tree.entries)
    for resource in snapshot.resources:
        if resource.active and resource.access=='write' and owner(names,resource.relative_path) is not None:
            if resource.relative_path in kinds and kinds[resource.relative_path]!=resource.kind:
                raise ValueError('Captured output tree conflicts with a declared member kind')
    binding=data['output_directory_bindings'];exact(binding,('outputs','trace'))
    if type(binding['outputs']) is not list:raise TypeError('Expected output stream bindings')
    indices=[];claimed=set()
    def member(path,desc):
        relative_path(path)
        if owner(names,path) is None or path not in members or members[path]!=desc or identities[path] in claimed:
            raise ValueError('Output stream does not match a unique captured directory file')
        claimed.add(identities[path])
    for row in binding['outputs']:
        exact(row,('index','relative_path'));index=row['index'];path=row['relative_path']
        if type(index) is not int or not 0<=index<len(data['outputs']) or path not in declared:
            raise ValueError('Invalid native directory-output binding')
        indices.append(index);member(path,data['outputs'][index]['blob'])
    if indices!=sorted(set(indices)):raise ValueError('Native directory-output bindings must be sorted and unique')
    if binding['trace'] is not None:
        from ..io.json import JsonDocument
        expected=JsonDocument.from_bytes(snapshot.backend_settings).data.get('trace') if snapshot.backend_settings is not None else None
        if binding['trace']!=expected or data['trace'] is None:raise ValueError('Invalid directory trace binding')
        member(binding['trace'],data['trace'])
    return states,binding
