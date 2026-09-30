"""Data-only executed resource declarations verified by the trusted parent.

The parent runs explicit codecs against captured INP before transport. The worker
checks this bounded bootstrap data against the snapshot and applies the same
portable layout/ownership rules. No extension code or import names are carried.
"""
from pathlib import Path
from ..model import Model,Ref,FileReference
from ..model.file_resources import FileUse
from ..io.inp import InpDocument
from ._result_codec import exact
from ._directory_tree import relative_path
from ..validation._cooperative import checkpointed


def validate(snapshot,uses):
    if type(uses) is not tuple or any(type(u) is not FileUse for u in uses):raise TypeError('Expected immutable executed resource declarations')
    records={}
    for record in checkpointed(snapshot.resources):
        key=(record.owner.canonical,record.field)
        if key in records:raise ValueError('Duplicate snapshot resource declaration')
        records[key]=record
    seen=set()
    for use in checkpointed(uses):
        relative_path(use.file.path)
        if use.file.base_directory is not None:raise ValueError('Executed declaration cannot carry an external base directory')
        key=(use.owner.canonical,use.path)
        if key in seen:raise ValueError('Duplicate executed resource declaration')
        seen.add(key);record=records.pop(key,None)
        if record is None:
            temp=snapshot.options.values.temp_directory
            if not (use.owner==Ref(collection='swmm:options',key='settings') and use.path==('temp_directory',) and use.role=='swmm:temporary_directory' and use.format=='core:directory' and use.kind=='directory' and use.access=='write' and use.active and use.required and temp==use.file):
                raise ValueError('Executed declaration is absent from snapshot')
        elif any(getattr(record,n)!=getattr(use,n) for n in ('role','format','kind','access','active','required')) or record.relative_path!=use.file.path:
            raise ValueError('Executed declaration differs from snapshot')
    if records:raise ValueError('Executed declarations omit snapshot consumers')
    return uses


def encode(snapshot,uses):
    validate(snapshot,uses)
    return dict(contract='easysewer:execution-declarations:1',input_sha256=snapshot.input_sha256,resources=[dict(
        owner=dict(collection=u.owner.collection,key=list(u.owner.key) if isinstance(u.owner.key,tuple) else u.owner.key),
        field=list(u.path),file=dict(path=u.file.path,flavor=u.file.flavor,direction=u.file.direction),
        role=u.role,format=u.format,base=u.base,kind=u.kind,access=u.access,required=u.required,active=u.active) for u in checkpointed(uses)])


def decode(snapshot,data):
    exact(data,('contract','input_sha256','resources'))
    if data['contract']!='easysewer:execution-declarations:1' or data['input_sha256']!=snapshot.input_sha256 or type(data['resources']) is not list:raise ValueError('Invalid executed declaration binding')
    result=[]
    for row in checkpointed(data['resources']):
        exact(row,('owner','field','file','role','format','base','kind','access','required','active'));exact(row['owner'],('collection','key'));exact(row['file'],('path','flavor','direction'))
        if type(row['field']) is not list:raise ValueError('Executed declaration field must be an array')
        key=row['owner']['key'];key=tuple(key) if type(key) is list else key
        result.append(FileUse(owner=Ref(collection=row['owner']['collection'],key=key),path=tuple(row['field']),file=FileReference(**row['file']),**{n:row[n] for n in ('role','format','base','kind','access','required','active')}))
    return validate(snapshot,tuple(result))


def prepare(snapshot,schema):
    from ._preparation import inventory
    from ._checkpoint_container import execution_layout
    root=Path(snapshot.execution_directory)
    from ._execution_model import execution_model
    model=execution_model(snapshot,schema=schema)
    claims=tuple(r for r in snapshot.resources if r.kind=='directory' and r.role!='swmm:temporary_directory')
    uses=tuple(p.use for p in inventory(model,input_directory=root,working_directory=root,_captured_directories=claims))
    validate(snapshot,uses);execution_layout(snapshot,root,_declarations=uses)
    return encode(snapshot,uses)
