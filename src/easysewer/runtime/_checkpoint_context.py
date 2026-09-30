"""Bounded, content-addressed bootstrap transfer outside the JSON RPC frame."""
import hashlib
import os
from pathlib import Path

from ._checkpoint_container import Limits, SHA, _Blobs, _regular, _stream, _relative, _snapshot_files, snapshot_codec_version
from ._result_codec import Codec, exact
from ._workspace import fingerprint, remove_owned_tree
from .results import RunSnapshot
from ..io.json import JsonDocument


def write(directory, snapshot, *, limits=Limits(), schema=None):
    if type(snapshot) is not RunSnapshot or type(limits) is not Limits:
        raise TypeError('Checkpoint context requires RunSnapshot and Limits')
    if snapshot_codec_version(snapshot) in ('1.4','1.5','1.6','1.7','1.8','1.9'):
        _snapshot_files(snapshot)
    declarations=None
    if schema is not None:
        from ._checkpoint_declarations import prepare
        declarations=prepare(snapshot,schema)
    root = Path(directory).absolute(); root.mkdir()
    identity = fingerprint(root)[:2]
    try:
        (root/'blobs').mkdir()
        blobs = _Blobs(root/'blobs', limits, lambda: None)
        encoded = Codec(blobs, result_version=snapshot_codec_version(snapshot)).encode(snapshot)
        data = dict(kind='easysewer:checkpoint-context', version=7 if snapshot_codec_version(snapshot)=='1.9' else 6 if snapshot_codec_version(snapshot) == '1.8' else 5 if snapshot_codec_version(snapshot) == '1.7' else 4 if snapshot_codec_version(snapshot) == '1.6' else 3 if snapshot_codec_version(snapshot) == '1.5' else 2 if snapshot_codec_version(snapshot) == '1.4' else 1, snapshot=encoded,
                    blobs=[dict(sha256=k,size=v) for k,v in sorted(blobs.inventory.items())])
        if declarations is not None:data.update(version=8,snapshot_version=snapshot_codec_version(snapshot),declarations=declarations)
        raw = JsonDocument.from_data(data).to_bytes()
        if len(raw)>limits.manifest_bytes: raise ValueError('Checkpoint context exceeds byte limit')
        with (root/'context.json').open('xb') as stream:
            if stream.write(raw)!=len(raw): raise OSError('Short checkpoint context write')
            stream.flush(); os.fsync(stream.fileno())
        return hashlib.sha256(raw).hexdigest(), identity
    except BaseException as error:
        try: remove_owned_tree(root,parent=root.parent,identity=identity)
        except BaseException as cleanup:
            error.checkpoint_cleanup_error=f'{type(cleanup).__name__}: {cleanup}'
        raise


def read(directory, expected_sha256, *, limits=Limits(), _with_declarations=False):
    if type(expected_sha256) is not str or not SHA.fullmatch(expected_sha256):
        raise ValueError('Checkpoint context requires its expected SHA256')
    root=Path(directory).absolute()
    if root.is_symlink() or not root.is_dir() or (root/'blobs').is_symlink():
        raise ValueError('Invalid checkpoint context directory')
    if {p.name for p in root.iterdir()} != {'blobs','context.json'}:
        raise ValueError('Invalid checkpoint context inventory')
    path=root/'context.json'
    if _regular(path).st_size>limits.manifest_bytes: raise ValueError('Checkpoint context exceeds byte limit')
    with path.open('rb') as stream: raw=stream.read(limits.manifest_bytes+1)
    if len(raw)>limits.manifest_bytes or hashlib.sha256(raw).hexdigest()!=expected_sha256:
        raise ValueError('Checkpoint context content changed')
    data=JsonDocument.from_bytes(raw).data
    extended=data.get('version')==8
    exact(data,('kind','version','snapshot','blobs','snapshot_version','declarations') if extended else ('kind','version','snapshot','blobs'))
    if data['kind']!='easysewer:checkpoint-context' or type(data['version']) is not int or data['version'] not in (1,2,3,4,5,6,7,8):
        raise ValueError('Unsupported checkpoint context')
    if type(data['blobs']) is not list: raise ValueError('Invalid checkpoint context blobs')
    blobs=_Blobs(root/'blobs',limits,lambda:None)
    for item in data['blobs']:
        exact(item,('sha256','size'))
        if item['sha256'] in blobs.inventory: raise ValueError('Duplicate context blob')
        blobs._reserve(item['sha256'],item['size'])
    if {p.name for p in (root/'blobs').iterdir()}!=set(blobs.inventory):
        raise ValueError('Checkpoint context blob inventory changed')
    for sha,size in blobs.inventory.items():
        if _stream(root/'blobs'/sha,size)!=sha: raise ValueError('Checkpoint context blob changed')
    if extended and data['snapshot_version'] not in ('1.1','1.4','1.5','1.6','1.7','1.8','1.9'):raise ValueError('Unsupported bootstrap snapshot codec')
    snapshot=Codec(blobs,result_version=data['snapshot_version'] if extended else '1.9' if data['version']==7 else '1.8' if data['version']==6 else '1.7' if data['version']==5 else '1.6' if data['version']==4 else '1.5' if data['version']==3 else '1.4' if data['version']==2 else '1.1').decode(data['snapshot'])
    if type(snapshot) is not RunSnapshot or blobs.used!=set(blobs.inventory):
        raise ValueError('Invalid checkpoint context snapshot')
    if snapshot_codec_version(snapshot) in ('1.4','1.5','1.6','1.7','1.8','1.9'):
        _snapshot_files(snapshot)
    declarations=None
    if extended:
        if data['snapshot_version']!=snapshot_codec_version(snapshot):raise ValueError('Bootstrap snapshot codec differs from resource shape')
        from ._checkpoint_declarations import decode
        declarations=decode(snapshot,data['declarations'])
        from ._checkpoint_container import execution_layout
        execution_layout(snapshot,Path(snapshot.execution_directory),_declarations=declarations)
    return (snapshot,declarations) if _with_declarations else snapshot


def output_inventory(snapshot, outputs, trace):
    """Bind native output indices to declared portable workspace destinations."""
    root=Path(snapshot.execution_directory).resolve()
    declared={'model.rpt','model.out'}
    declared.update(item.relative_path for item in snapshot.resources
                    if item.active and item.access=='write' and item.kind=='file')
    inputs=set(_snapshot_files(snapshot)) | {'model.inp'}
    rows=[];seen=set()
    for index,item in enumerate(outputs):
        path=Path(item.path).resolve()
        if not path.is_relative_to(root): raise ValueError('Checkpoint output escaped its workspace')
        name=path.relative_to(root).as_posix();_relative(name)
        if name not in declared or name in inputs or name.casefold() in seen:
            raise ValueError('Checkpoint output is undeclared or aliases another file')
        if item.index!=index: raise ValueError('Invalid checkpoint output order')
        seen.add(name.casefold())
        rows.append(dict(index=index,role=item.role,text=item.text,relative_path=name))
    expected_trace=None
    if snapshot.backend_settings is not None:
        expected_trace=JsonDocument.from_bytes(snapshot.backend_settings).data.get('trace')
    if trace is not None:
        path=Path(trace).resolve()
        if not path.is_relative_to(root): raise ValueError('Checkpoint trace escaped its workspace')
        trace=path.relative_to(root).as_posix();_relative(trace)
        if trace!=expected_trace or trace.casefold() in seen or trace in inputs:
            raise ValueError('Checkpoint trace differs from its declared output')
    elif expected_trace is not None:
        raise ValueError('Checkpoint trace was not opened')
    return dict(outputs=rows,trace=trace)


def write_outputs(directory, data, *, limits=Limits()):
    raw=JsonDocument.from_data(data).to_bytes()
    if len(raw)>limits.manifest_bytes: raise ValueError('Checkpoint output inventory exceeds byte limit')
    with (Path(directory)/'outputs.json').open('xb') as stream:
        if stream.write(raw)!=len(raw): raise OSError('Short checkpoint output inventory write')
        stream.flush();os.fsync(stream.fileno())
    return hashlib.sha256(raw).hexdigest()


def read_outputs(directory, expected_sha256, snapshot, *, limits=Limits()):
    from types import SimpleNamespace
    path=Path(directory)/'outputs.json'
    if type(expected_sha256) is not str or not SHA.fullmatch(expected_sha256):
        raise ValueError('Invalid checkpoint output inventory digest')
    if _regular(path).st_size>limits.manifest_bytes: raise ValueError('Checkpoint output inventory exceeds byte limit')
    with path.open('rb') as stream:raw=stream.read(limits.manifest_bytes+1)
    if len(raw)>limits.manifest_bytes or hashlib.sha256(raw).hexdigest()!=expected_sha256:
        raise ValueError('Checkpoint output inventory changed')
    data=JsonDocument.from_bytes(raw).data;exact(data,('outputs','trace'))
    if type(data['outputs']) is not list: raise ValueError('Invalid checkpoint output inventory')
    outputs=[]
    for index,item in enumerate(data['outputs']):
        exact(item,('index','role','text','relative_path'))
        if (type(item['index']) is not int or item['index']!=index or type(item['role']) is not int or
            not 0<=item['role']<=5 or type(item['text']) is not bool):
            raise ValueError('Invalid checkpoint output descriptor')
        relative=_relative(item['relative_path'])
        outputs.append(SimpleNamespace(index=index,role=item['role'],text=item['text'],
            path=Path(snapshot.execution_directory)/relative))
    trace=None if data['trace'] is None else Path(snapshot.execution_directory)/_relative(data['trace'])
    if output_inventory(snapshot,outputs,trace)!=data: raise ValueError('Checkpoint output inventory mismatch')
    if [(v['role'],v['relative_path']) for v in data['outputs'] if v['role'] in (0,1)]!=[(0,'model.rpt'),(1,'model.out')]:
        raise ValueError('Checkpoint main output identities differ')
    return data
