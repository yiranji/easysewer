from dataclasses import replace
from pathlib import Path
from easysewer.model import Model,FileReference
from easysewer.io.inp import InpDocument
from easysewer.runtime import DirectoryAdapter,FlexiblePondingPolicy
from easysewer.runtime.results import ResourceSnapshot
from easysewer.runtime._preparation import inventory
from test_native_v2_lid_report_io import fixture as lid_fixture
from test_checkpoint_container_v2 import snapshot
from test_native_public_directory_checkpoint_v2 import valid
from test_flexible_v2 import configuration
from directory_roundtrip_fixture import row
import native_directory_lifecycle_fixture as nf

def prepare(root,family):
 work=root/'work';work.mkdir();(work/'assets/out/sub').mkdir(parents=True);(work/'assets/tmp').mkdir();model=Model.from_document(InpDocument.from_text(lid_fixture(detail='assets/out/sub/lid.txt')),schema=nf.schema(),strict=True);model.collection('test:directory').add(row('O','assets/out',access='write'));model.collection('test:directory').add(row('N','assets/out/sub',access='write'));model.update_options(temp_directory=FileReference(path='assets/tmp',direction='output'))
 if family=='custom':model.update_options(allow_ponding=True)
 plans=inventory(model,input_directory=work,working_directory=work,directory_adapters={'test:directory.format':DirectoryAdapter(inspector=valid)});resources=tuple(ResourceSnapshot(owner=p.use.owner,field=p.use.path,role=p.use.role,format=p.use.format,kind=p.use.kind,access=p.use.access,active=p.use.active,required=p.use.required,original_path=str(p.original) if p.original else None,relative_path=p.use.file.path,sha256=None,size=None) for p in plans);value=snapshot(work,model=model,backend=nf.backend(family).probe(),resources=resources)
 if family=='custom':
  settings=configuration(root/'published',FlexiblePondingPolicy(record_steps=True));value=replace(value,config_json=settings.to_json_document().to_bytes());plan=nf.backend(family).prepare_run(model,settings,value,artifact_directory=Path('assets/out/backend'));value=replace(value,backend_settings=plan.parameters.to_bytes());(work/'assets/out/backend').mkdir()
 (work/'model.inp').write_bytes(value.input_bytes);return work,value
