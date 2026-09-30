"""Real INP/JSON extension codec; no native directory-consumer claim."""
from dataclasses import dataclass
from easysewer.model import Model,CollectionSpec,Ref,FileReference
from easysewer.model.file_resources import FileUse
from easysewer.io.inp.network import default_schema
from easysewer.io.json.types import JsonType,JsonField
from easysewer.schema import DecodedFeature,FeatureDescriptor
from easysewer.schema.structured import FeatureData,RecordEntry,SourceBinding,EncodedRow
from easysewer.runtime.results import ResourceSnapshot
from easysewer.runtime._directory_tree import inspect_tree
from test_options_v2 import network
from test_checkpoint_container_v2 import snapshot

@dataclass(frozen=True,kw_only=True)
class Directory:
 id:str
 file:FileReference
 kind:str='directory'
 access:str='read'
 active:bool=True
 required:bool=True

class Codec:
 collections=(CollectionSpec(key='test:directory',record_type=Directory,key_of=lambda r:r.id,identity_field='id'),)
 def decode(self,document,profile):
  records=[];bindings=[]
  for line in document.records('TEST_DIRECTORY'):
   name,path,kind,access,active,required=line.values
   if active not in ('0','1') or required not in ('0','1'):raise ValueError('Invalid fixture directory flags')
   row=Directory(id=name,file=FileReference(path=path,direction='output' if access=='write' else 'input'),kind=kind,access=access,active=active=='1',required=required=='1')
   records.append(RecordEntry(collection='test:directory',value=row));bindings.append(SourceBinding(line=line.number,key=(name,)))
  return DecodedFeature(value=FeatureData(records=tuple(records),bindings=tuple(bindings)),claimed_lines=frozenset(b.line for b in bindings))
 def encode(self,store,profile):
  for row in store.collection('test:directory').values():
   yield EncodedRow(key=(row.id,),section='TEST_DIRECTORY',values=(row.id,row.file.path,row.kind,row.access,str(int(row.active)),str(int(row.required))),owners=(Ref(collection='test:directory',key=row.id),))
 def validate(self,store,profile):return ()
 def file_uses(self,store,profile):
  for row in store.collection('test:directory').values():
   yield FileUse(owner=Ref(collection='test:directory',key=row.id),path=('file',),file=row.file,role='test:directory.consumer',format='test:directory.format',kind=row.kind,access=row.access,active=row.active,required=row.required)

def schema():
 value=default_schema();value.register(FeatureDescriptor(key='test:directory',sections={'TEST_DIRECTORY'}),Codec())
 shapes={'id':('string',),'file':('object','core:file'),'kind':('string',),'access':('string',),'active':('boolean',),'required':('boolean',)}
 value.register_json(JsonType(key='test:directory.row',value_type=Directory,fields=tuple(JsonField(name=n,attribute=n,shape=v) for n,v in shapes.items())))
 return value

def row(name,path,**options):return Directory(id=name,file=FileReference(path=path,direction='output' if options.get('access')=='write' else 'input'),**options)

def prepared(root,rows):
 model=Model.from_document(network().to_document(),schema=schema())
 for item in rows:model.collection('test:directory').add(item)
 resources=[]
 for use in model.file_uses():
  if use.owner.collection!='test:directory':continue
  path=root/use.file.path
  tree=inspect_tree(path) if use.active and use.access!='write' and path.is_dir() else None
  resources.append(ResourceSnapshot(owner=use.owner,field=use.path,role=use.role,format=use.format,kind=use.kind,access=use.access,active=use.active,required=use.required,original_path=str(path),relative_path=use.file.path,sha256=None,size=None,tree=tree))
 return snapshot(root,model=model,resources=tuple(resources)),model
