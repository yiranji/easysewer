from dataclasses import dataclass
from easysewer.model import Model,CollectionSpec,Ref,FileReference
from easysewer.model.file_resources import FileUse
from easysewer.io.inp.network import default_schema
from easysewer.schema import DecodedFeature,FeatureDescriptor
from easysewer.schema.structured import FeatureData
@dataclass(frozen=True,kw_only=True)
class Data:
 id:str
 file:FileReference
 kind:str='directory'
 access:str='read'
 active:bool=True
class Codec:
 collections=(CollectionSpec(key='test:directory',record_type=Data,key_of=lambda v:v.id,identity_field='id'),)
 def decode(self,document,profile):return DecodedFeature(value=FeatureData())
 def encode(self,store,profile):return ()
 def validate(self,store,profile):return ()
 def file_uses(self,store,profile):
  for row in store.collection('test:directory').values():
   yield FileUse(owner=Ref(collection='test:directory',key=row.id),path=('file',),file=row.file,role='test:directory.consumer',format='test:directory.format',kind=row.kind,access=row.access,active=row.active)
def model(*rows):
 schema=default_schema();schema.register(FeatureDescriptor(key='test:directory',sections={'DIRECTORY'}),Codec());result=Model(schema=schema)
 for row in rows:result.collection('test:directory').add(row)
 return result
def row(name,path,**kw):return Data(id=name,file=FileReference(path=str(path),direction='output' if kw.get('access')=='write' else 'input'),**kw)
