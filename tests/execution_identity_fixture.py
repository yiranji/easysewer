from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
import hashlib
from easysewer.model import Model,FileReference
from easysewer.runtime import DirectoryAdapter
from easysewer.runtime._preparation import inventory,stage
from directory_roundtrip_fixture import row
from native_mutable_output_checkpoint_fixture import model_at as base_model,nf,valid
from test_lid_v2 import usage
from test_checkpoint_container_v2 import snapshot
from test_runner_v2 import config

def model_at(root,family='standard',mode='custom'):
 source,model=base_model(root,family);target=root/'external';model.lid_usage.add(usage('second',area=50,initial_saturation=0,report_file=FileReference(path=str(target/'sub/lid.txt'),direction='output')));model.collection('test:directory').add(row('O',str(target),access='write'))
 if mode=='swapped':model.lid_usage.rename('lid-usage-1','lid-usage-2');model.lid_usage.rename('second','lid-usage-1')
 elif mode=='reordered':model.lid_usage.rename('lid-usage-1','first');model.lid_usage.move('second',before='first')
 return source,model

def prepared(root,mode='custom',normalize=False):
 source,model=model_at(root,mode=mode);original=model.to_json_document().to_bytes();work=root/'work';work.mkdir();plans=inventory(model,input_directory=root,working_directory=root,directory_adapters={'test:directory.format':DirectoryAdapter(inspector=valid)});resources,_,_=stage(model,plans,work,'assets',checkpoint=lambda:None);value=snapshot(work,model=model,resources=resources);raw=model.to_document(normalize=normalize).text.encode('utf-8');value=replace(value,model_json=original,model_sha256=hashlib.sha256(original).hexdigest(),input_bytes=raw,input_sha256=hashlib.sha256(raw).hexdigest(),config_json=config(root/'published',normalize_inp=normalize).to_json_document().to_bytes());return source,work,value,model

def native_prepared(root,family):
 import native_mutable_output_checkpoint_fixture as fixture
 original=[]
 def create(where,kind):
  source,model=model_at(where,kind);original.append(model.to_json_document().to_bytes());return source,model
 with patch.object(fixture,'model_at',create):source,work,value,current=fixture.prepare(root,family)
 value=replace(value,model_json=original[0],model_sha256=hashlib.sha256(original[0]).hexdigest());return source,work,value,current
