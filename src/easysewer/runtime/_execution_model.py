"""Recover captured consumer identities only with an exact executed-INP proof.

INP has no identifiers for some records (for example LID_USAGE). Re-parsing
assigns new local IDs, while RunSnapshot retains the user's Model identities.
Do not infer an identity mapping from a filename or weaken declaration checks.
"""
from collections import Counter
from dataclasses import replace
from ..model import Model, Ref
from ..model.file_resources import replace_path
from ..io.inp import InpDocument
from ..io.json import JsonDocument
from ..validation._cooperative import checkpointed


_POLICY=('role','format','kind','access','active','required')


def _address(use):return use.owner.canonical,use.path


def _implicit_temp(snapshot,use,addresses):
    return (_address(use) not in addresses and use.owner==Ref(collection='swmm:options',key='settings')
        and use.path==('temp_directory',) and use.role=='swmm:temporary_directory'
        and use.format=='core:directory' and use.kind=='directory' and use.access=='write'
        and use.active and use.required and use.file==snapshot.options.values.temp_directory)


def execution_model(snapshot,*,schema=None,strict=True,_defer_declarations=False):
    from ._checkpoint_declarations import validate
    parsed=Model.from_document(InpDocument.from_bytes(snapshot.input_bytes),schema=schema,profile=snapshot.profile,strict=strict)
    uses=tuple(parsed.file_uses());records=snapshot.resources
    addresses={(r.owner.canonical,r.field) for r in checkpointed(records)}
    def shape(owner,field,path,item):return (owner.collection,field,path,*(getattr(item,n) for n in _POLICY))
    expected=[(r.owner.canonical,shape(r.owner,r.field,r.relative_path,r)) for r in checkpointed(records)]
    observed=[(u.owner.canonical,shape(u.owner,u.path,u.file.path,u)) for u in checkpointed(uses) if not _implicit_temp(snapshot,u,addresses)]
    if Counter(observed)==Counter(expected):
        validate(snapshot,uses)
        return parsed
    if Counter(s for _,s in observed)!=Counter(s for _,s in expected):
        # Layout callers first diagnose path aliases, protected absences and missing
        # adapters, then validate every declaration before returning or creating files.
        # This never infers original identities for a changed resource inventory.
        if _defer_declarations:return parsed
        # Preserve strict rejection for changed paths, directions, consumers or policies.
        validate(snapshot,uses)
        raise ValueError('Executed resource inventory differs from captured declarations')
    captured=snapshot.model(schema=schema)
    original=tuple(captured.file_uses());by_address={_address(u):u for u in checkpointed(original)}
    if len(by_address)!=len(original) or len(addresses)!=len(records):raise ValueError('Duplicate captured consumer identity')
    if any(_address(u) not in addresses and not _implicit_temp(snapshot,u,addresses) for u in checkpointed(original)):
        raise ValueError('Captured Model has unrecorded resource consumers')
    with captured._store._permit_context_change('file_rebase'):
        for record in checkpointed(records):
            use=by_address.get((record.owner.canonical,record.field))
            if use is None or any(getattr(use,n)!=getattr(record,n) for n in _POLICY):
                raise ValueError('Captured Model consumer differs from snapshot')
            reference=replace(use.file,path=record.relative_path,base_directory=None,flavor='native')
            rows=captured.collection(use.owner.collection)
            rows.replace(use.owner.key,replace_path(rows[use.owner.key],use.path,reference))
    captured.update_options(temp_directory=snapshot.options.values.temp_directory)
    if captured.effective_options!=snapshot.options:raise ValueError('Captured execution options differ from snapshot')
    from .config import RunConfig
    config=RunConfig.from_json_document(JsonDocument.from_bytes(snapshot.config_json))
    if captured.to_document(normalize=config.normalize_inp).text.encode('utf-8')!=snapshot.input_bytes:
        raise ValueError('Captured consumer identities cannot reproduce the exact executed INP')
    validate(snapshot,tuple(captured.file_uses()))
    return captured
