from dataclasses import replace
from pathlib import Path
from easysewer.model import Model,FileReference
from easysewer.model.resources import FileTimeSeries
from easysewer.io.inp import InpDocument
from easysewer.io.json import JsonDocument
from easysewer.runtime import DirectoryAdapter,FlexiblePondingPolicy
from easysewer.runtime._preparation import inventory,stage
from test_native_v2_lid_report_io import fixture as lid_fixture
from test_native_public_directory_checkpoint_v2 import valid
from test_checkpoint_container_v2 import snapshot
from test_flexible_v2 import configuration
from directory_roundtrip_fixture import row
import native_directory_lifecycle_fixture as nf

def model_at(root,family,nested=True):
 source=root/'source';(source/'sub').mkdir(parents=True);(source/'state').write_bytes(b'initial');(source/'sub/lid.txt').write_bytes(b'prior report');(source/'sub/alias').hardlink_to(source/'sub/lid.txt');(source/'rain').write_text(''.join('01/30/2020 00:%02d %s\n'%(n,.6 if n in (2,4) else 0) for n in range(7)))
 model=Model.from_document(InpDocument.from_text(lid_fixture(detail=str(source/'sub/lid.txt'))),schema=nf.schema(),strict=True);model.timeseries.replace('Rain',FileTimeSeries(id='Rain',file=FileReference(path=str(source/'rain'))));model.collection('test:directory').add(row('D',str(source),access='read_write'))
 if nested:model.collection('test:directory').add(row('N',str(source/'sub'),access='read_write'));model.collection('test:directory').add(row('R',str(source)))
 if family=='custom':model.update_options(allow_ponding=True)
 return source,model

def prepare(root,family):
 source,model=model_at(root,family);work=root/'work';work.mkdir();plans=inventory(model,input_directory=root,working_directory=root,directory_adapters={'test:directory.format':DirectoryAdapter(inspector=valid)});resources,_,_=stage(model,plans,work,'assets',checkpoint=lambda:None);value=snapshot(work,model=model,backend=nf.backend(family).probe(),resources=resources);record=next(x for x in resources if x.owner.key=='D');current=work/record.relative_path
 if family=='custom':
  settings=configuration(root/'published',FlexiblePondingPolicy(record_steps=True));value=replace(value,config_json=settings.to_json_document().to_bytes());path=Path(record.relative_path)/'backend';plan=nf.backend(family).prepare_run(model,settings,value,artifact_directory=path);value=replace(value,backend_settings=plan.parameters.to_bytes());(work/path).mkdir()
 (work/'model.inp').write_bytes(value.input_bytes);return source,work,value,current

def finish(session,work,value):
 for _ in range(1000):
  if session.step(max_steps=3).finished:break
 else:raise AssertionError('native completion timeout')
 outputs=session.checkpoint_outputs;session.end();session.report();session.close();raw=next(x.path for x in outputs if x.role=='run:output').read_bytes();lid=next(x.path for x in outputs if x.role=='swmm:lid_report').read_bytes();trace=JsonDocument.from_bytes(value.backend_settings).data.get('trace') if value.backend_settings else None
 return raw,lid,(work/trace).read_bytes() if trace else None
