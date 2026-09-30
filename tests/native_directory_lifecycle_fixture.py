"""Actual native solver consumes a time-series file inside a declared directory.

A test-only TITLE codec carries the directory declaration; TITLE is native
presentation text. Custom schema is supplied only to the in-process lifecycle.
No claim of public worker schema transport is made.
"""
from dataclasses import replace
from datetime import timedelta
from easysewer.model.report import ReportSelection
from pathlib import Path
from easysewer.model import Model
from easysewer.io.inp import InpDocument
from easysewer.io.interface_inspection import InterfaceInspection
from easysewer.schema import FeatureDescriptor,DecodedFeature
from easysewer.schema.structured import ModelSchema,FeatureData,RecordEntry,SourceBinding,EncodedRow
from easysewer.io.inp.network import default_schema
from easysewer.runtime import StandardBackend,FlexiblePondingBackend,DirectoryAdapter,FlexiblePondingPolicy
from easysewer.runtime._preparation import inventory,stage
from easysewer.runtime._native_solver import NativeSolver
from easysewer.runtime._native_flexible import NativeFlexibleSolver
from easysewer.runtime._checkpoint_lifecycle import CheckpointLifecycle
from directory_roundtrip_fixture import Codec,row,schema as resource_schema
from test_options_v2 import network
from test_checkpoint_container_v2 import snapshot
from test_flexible_v2 import configuration
from test_checkpoint_directory_inputs_v2 import working

class TitleCodec(Codec):
 def decode(self,document,profile):
  records=[];bindings=[]
  for section in document.find_sections('TITLE'):
   for line in section.lines:
    fields=line.content.split()
    if not fields:continue
    name,path,kind,access,active,required=fields
    value=row(name,path,kind=kind,access=access,active=active=='1',required=required=='1');records.append(RecordEntry(collection='test:directory',value=value));bindings.append(SourceBinding(line=line.number,key=(name,)))
  return DecodedFeature(value=FeatureData(records=tuple(records),bindings=tuple(bindings)),claimed_lines=frozenset(b.line for b in bindings))
 def encode(self,store,profile):
  for value in super().encode(store,profile):yield replace(value,section='TITLE',values=(),raw_text=' '.join(value.values))

def schema():
 result=ModelSchema();source=resource_schema()
 for descriptor,codec in source.bindings:
  if descriptor.key not in ('easysewer:project','test:directory'):result.register(descriptor,codec)
 result.register(FeatureDescriptor(key='test:directory',sections={'TITLE'},raw_sections={'TITLE'}),TitleCodec());result.register_json(*source.json_types.declarations);return result

def backend(family):return (StandardBackend if family=='standard' else FlexiblePondingBackend)()
def new_solver(family):return (NativeSolver if family=='standard' else NativeFlexibleSolver)(backend(family).probe().library)
def prepare(root,family):
 source=root/'source';source.mkdir();(source/'data').write_text('01/01/2020 00:00 1\n01/01/2020 00:05 3\n01/01/2020 00:10 0.5\n');(source/'alias').hardlink_to(source/'data');(source/'empty').mkdir()
 text=network().to_document().text+'[TIMESERIES]\nS FILE source/data\n[INFLOWS]\nJ FLOW S FLOW 1 1\n'
 model=Model.from_document(InpDocument.from_text(text),schema=schema(),strict=True);model.collection('test:directory').add(row('D','source'));model.update_options(report_step=timedelta(seconds=60));model.update_report(nodes=ReportSelection(mode='ALL'),links=ReportSelection(mode='ALL'))
 if family=='custom':model.update_options(allow_ponding=True)
 work=root/'work';work.mkdir();plans=inventory(model,input_directory=root,working_directory=root,directory_adapters={'test:directory.format':DirectoryAdapter(inspector=lambda p,**k:InterfaceInspection(format=k['use'].format,status='validated'))});resources,_,_=stage(model,plans,work,'assets',checkpoint=lambda:None)
 value=snapshot(work,model=model,backend=backend(family).probe(),resources=resources)
 if family=='custom':
  settings=configuration(root/'published',FlexiblePondingPolicy(external_flooding_ratio=.37,depth_threshold_m=0,flow_threshold_cms=0,record_steps=True));value=replace(value,config_json=settings.to_json_document().to_bytes());plan=backend(family).prepare_run(model,settings,value,artifact_directory=Path('assets/backend'));value=replace(value,backend_settings=plan.parameters.to_bytes());(work/'assets/backend').mkdir()
 (work/'model.inp').write_bytes(value.input_bytes);return source,work,value

def finish(owner):
 for _ in range(1000):
  if owner.step(3)['finished']:break
 else:raise AssertionError('native run did not finish')
 outputs=owner.api.outputs();owner.end();owner.report();owner.cleanup();output=next(x for x in outputs if x.role==1)
 from easysewer.io.output_metadata import OutputMetadata
 metadata=OutputMetadata.read(output.path);assert metadata.periods==10 and metadata.names('swmm:nodes')==('J','O') and metadata.names('swmm:links')==('P',),metadata
 return Path(output.path).read_bytes()
