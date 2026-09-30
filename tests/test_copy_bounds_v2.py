"""Capture bounds and short-write failures with real temporary file contents."""
from pathlib import Path
import hashlib,os,tempfile,unittest
from unittest.mock import patch
from easysewer.runtime import _workspace as w
from easysewer.runtime._directory_tree import capture_tree,inspect_tree

class Stream:
 def __init__(self,stream,*,read=None,write=None):self.stream=stream;self.reader=read;self.writer=write
 def __enter__(self):self.stream.__enter__();return self
 def __exit__(self,*args):return self.stream.__exit__(*args)
 def __getattr__(self,name):return getattr(self.stream,name)
 def read(self,size=-1):return self.reader(self.stream,size) if self.reader else self.stream.read(size)
 def write(self,data):return self.writer(self.stream,data) if self.writer else self.stream.write(data)

class CopyBoundsTests(unittest.TestCase):
 def test_exact_empty_and_chunk_boundaries_keep_bytes_and_hashes(self):
  for size in (0,1,1024**2-1,1024**2,1024**2+1):
   with self.subTest(size=size),tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp);source=root/'source';target=root/'target';data=(b'abc123'*((size+5)//6))[:size];source.write_bytes(data);expected=(hashlib.sha256(data).hexdigest(),size)
    self.assertEqual(w.copy_input(source,target),expected);self.assertEqual(target.read_bytes(),data);self.assertEqual(w.digest_file(source),expected);self.assertEqual(w.digest_file(source,expected_size=size),expected)
 def test_copy_source_growth_never_writes_beyond_initial_size(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);source=root/'source';target=root/'target';source.write_bytes(b'a'*2*1024**2);opening=Path.open;reads=[]
   def read(stream,size):
    result=stream.read(size);reads.append(len(result))
    if len(reads)==1:
     with opening(source,'ab') as extra:extra.write(b'b'*1024**2)
    return result
   def opened(path,mode='r',*args,**kw):
    result=opening(path,mode,*args,**kw);return Stream(result,read=read) if path==source and mode=='rb' else result
   with patch.object(Path,'open',opened),self.assertRaisesRegex(ValueError,'grew'):w.copy_input(source,target)
   self.assertLessEqual(target.stat().st_size,2*1024**2);self.assertEqual(sum(reads),2*1024**2+1)
   self.assertEqual(w.copy_input(source,root/'retry'),w.digest_file(source))
 def test_digest_growth_has_one_byte_probe_and_no_unbounded_read(self):
  with tempfile.TemporaryDirectory() as tmp:
   source=Path(tmp)/'source';source.write_bytes(b'a'*2*1024**2);opening=Path.open;reads=[]
   def read(stream,size):
    result=stream.read(size);reads.append(len(result))
    if len(reads)==1:
     with opening(source,'ab') as extra:extra.write(b'b'*1024**2)
    return result
   def opened(path,mode='r',*args,**kw):
    result=opening(path,mode,*args,**kw);return Stream(result,read=read) if path==source and mode=='rb' else result
   with patch.object(Path,'open',opened),self.assertRaisesRegex(ValueError,'grew'):w.digest_file(source)
   self.assertEqual(sum(reads),2*1024**2+1)
 def test_second_verification_open_cannot_expand_captured_size(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);source=root/'source';target=root/'target';source.write_bytes(b'a'*1024**2);opening=Path.open;opens=[];extra_reads=[]
   def opened(path,mode='r',*args,**kw):
    if path==source and mode=='rb':
     opens.append(True)
     if len(opens)==2:
      with opening(source,'ab') as extra:extra.write(b'b'*1024**2)
    result=opening(path,mode,*args,**kw)
    return Stream(result,read=lambda stream,n:(extra_reads.append(n),stream.read(n))[1]) if path==source and mode=='rb' and len(opens)==2 else result
   with patch.object(Path,'open',opened),self.assertRaisesRegex(ValueError,'size changed'):w.copy_input(source,target)
   self.assertEqual(len(opens),2);self.assertEqual(extra_reads,[]);self.assertEqual(target.stat().st_size,1024**2)
 def test_short_writes_fail_immediately_without_success_digest(self):
  for mode in ('zero','partial','none'):
   with self.subTest(mode=mode),tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp);source=root/'source';target=root/'target';data=b'a'*2*1024**2;source.write_bytes(data);opening=Path.open;writes=[]
    def write(stream,data):
     writes.append(True)
     if mode=='none':return None
     return stream.write(data[:0] if mode=='zero' else data[:-1])
    def opened(path,mode='r',*args,**kw):
     result=opening(path,mode,*args,**kw);return Stream(result,write=write) if path==target and mode=='xb' else result
    with patch.object(Path,'open',opened),self.assertRaisesRegex(OSError,'Short write'):w.copy_input(source,target)
    self.assertEqual(len(writes),1);self.assertEqual(source.read_bytes(),data);self.assertEqual(w.copy_input(source,root/'retry'),w.digest_file(source))
 def test_truncated_source_is_not_accepted_by_copy_or_digest(self):
  for operation in ('copy','digest'):
   with self.subTest(operation=operation),tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp);source=root/'source';source.write_bytes(b'a'*2*1024**2);opening=Path.open;fired=[]
    def read(stream,size):
     result=stream.read(size)
     if not fired:
      fired.append(True)
      with opening(source,'r+b') as writer:writer.truncate(1024**2)
     return result
    def opened(path,mode='r',*args,**kw):
     result=opening(path,mode,*args,**kw);return Stream(result,read=read) if path==source and mode=='rb' else result
    with patch.object(Path,'open',opened),self.assertRaisesRegex(ValueError,'shrank'):
     if operation=='copy':w.copy_input(source,root/'target')
     else:w.digest_file(source)
    self.assertEqual(len(fired),1)
 def test_original_callback_and_write_errors_keep_identity_cause_and_retry(self):
  for mode in ('cancel','write'):
   with self.subTest(mode=mode),tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp);source=root/'source';target=root/'target';source.write_bytes(b'a'*2*1024**2);opening=Path.open;calls=[];error=OSError('original failure');cause=ValueError('root cause');error.__cause__=cause
    def checkpoint():
     calls.append(True)
     if mode=='cancel' and len(calls)==2:raise error
    def write(stream,data):raise error
    def opened(path,access='r',*args,**kw):
     result=opening(path,access,*args,**kw);return Stream(result,write=write) if mode=='write' and path==target and access=='xb' else result
    with patch.object(Path,'open',opened),self.assertRaises(OSError) as caught:w.copy_input(source,target,checkpoint=checkpoint)
    self.assertIs(caught.exception,error);self.assertIs(caught.exception.__cause__,cause);self.assertEqual(source.stat().st_size,2*1024**2);self.assertEqual(w.copy_input(source,root/'retry'),w.digest_file(source))
 def test_directory_capture_propagates_short_write_preserves_source_and_retries(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);source=root/'source';source.mkdir();(source/'member').write_bytes(b'ordinary member');(source/'empty').mkdir();expected=inspect_tree(source);opening=Path.open;target=root/'target';fired=[]
   def write(stream,data):fired.append(True);return stream.write(data[:-1])
   def opened(path,mode='r',*args,**kw):
    result=opening(path,mode,*args,**kw);return Stream(result,write=write) if path==target/'member' and mode=='xb' else result
   with patch.object(Path,'open',opened),self.assertRaisesRegex(OSError,'Short write'):capture_tree(source,target)
   self.assertEqual(len(fired),1);self.assertEqual(inspect_tree(source),expected);self.assertEqual(capture_tree(source,root/'retry'),expected)

 def test_declared_tree_size_is_checked_before_creating_member(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);source=root/'source';source.mkdir();member=source/'data';member.write_bytes(b'a'*1024**2);target=root/'target'
   from easysewer.runtime import _directory_tree as trees
   copy=trees.copy_input;events=[]
   def growing(src,dst,**kw):
    with member.open('ab') as stream:stream.write(b'b'*1024**2)
    events.append(True);return copy(src,dst,**kw)
   with patch.object(trees,'copy_input',growing),self.assertRaisesRegex(ValueError,'size changed before capture'):capture_tree(source,target,limits=trees.DirectoryLimits(total_bytes=1024**2))
   self.assertEqual(len(events),1);self.assertFalse((target/'data').exists())
 def test_publication_uses_previously_verified_source_size_for_files_and_trees(self):
  for kind in ('file','tree'):
   with self.subTest(kind=kind),tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp).resolve();source=root/'source';source.mkdir();member=source/'data';member.write_bytes(b'a'*1024**2);manifest=inspect_tree(source);old=root/'old'
    if kind=='tree':old.mkdir();(old/'data').write_bytes(b'old')
    else:old.write_bytes(b'old')
    transaction=w.OutputTransaction('size-bound',overwrite=True);transaction.reserve(((old,kind=='tree'),));copy=w.copy_input;events=[]
    def growing(src,dst,**kw):
     if Path(src)==member and not events:
      events.append(True)
      with member.open('ab') as stream:stream.write(b'b'*1024**2)
     return copy(src,dst,**kw)
    try:
     with patch.object(w,'copy_input',growing),self.assertRaisesRegex(ValueError,'size changed before capture'):
      transaction.publish({old:source if kind=='tree' else member},expected_trees={source:manifest} if kind=='tree' else None,expected_sources={member:(manifest.entries[0].sha256,manifest.entries[0].size)})
     self.assertEqual(len(events),1);self.assertEqual((old/'data' if kind=='tree' else old).read_bytes(),b'old')
    finally:transaction.close()
    self.assertFalse(list(root.glob('.easysewer-*')))

 def test_late_unrecorded_publication_members_are_refused_before_creation(self):
  from easysewer.runtime import _directory_tree as trees
  for kind in ('file','directory','kind-change'):
   with self.subTest(kind=kind),tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp);source=root/'source';source.mkdir();(source/'small').write_bytes(b'x');manifest=inspect_tree(source);target=root/'target';target.mkdir();verify=trees.verify_tree;fired=[]
    def changing(path,expected,**kwargs):
     result=verify(path,expected,**kwargs)
     if Path(path)==source and not fired:
      fired.append(True)
      if kind=='file':(source/'late').write_bytes(b'y'*1024**2)
      elif kind=='directory':(source/'late').mkdir();(source/'late/large').write_bytes(b'y'*1024**2)
      else:(source/'small').unlink();(source/'small').mkdir();(source/'small/large').write_bytes(b'y'*1024**2)
     return result
    with patch.object(trees,'verify_tree',changing),self.assertRaisesRegex(ValueError,'membership changed'):w.copy_tree(source,target,expected_tree=manifest)
    self.assertEqual(len(fired),1);self.assertFalse((target/('small' if kind=='kind-change' else 'late')).exists())
 def test_file_only_publication_inventory_refuses_unknown_file_before_copy(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);source=root/'source';source.mkdir();(source/'known').write_bytes(b'known');(source/'extra').write_bytes(b'x'*1024**2);target=root/'target';target.mkdir()
   with self.assertRaisesRegex(ValueError,'changed before publication'):w.copy_tree(source,target,expected_files={source.resolve()/'known':w.digest_file(source/'known')})
   self.assertFalse((target/'extra').exists())

 def test_directory_and_file_inventories_must_agree_before_any_member_is_copied(self):
  for linked in (False,True):
   for change in ('size','digest','extra','missing'):
    with self.subTest(linked=linked,change=change),tempfile.TemporaryDirectory() as tmp:
     root=Path(tmp).resolve();source=root/'source';source.mkdir();(source/'data').write_bytes(b'known')
     if linked:(source/'alias').hardlink_to(source/'data')
     manifest=inspect_tree(source);expected={source/e.path:(e.sha256,e.size) for e in manifest.entries if e.kind=='file'};changed=dict(expected);key=source/'data'
     if change=='size':changed[key]=(expected[key][0],expected[key][1]+1)
     elif change=='digest':changed[key]=('0'*64,expected[key][1])
     elif change=='extra':changed[source/'extra']=('0'*64,100)
     else:changed.pop(key)
     target=root/'target';target.mkdir()
     with self.assertRaisesRegex(ValueError,'evidence changed'):w.copy_tree(source,target,expected_tree=manifest,expected_files=changed)
     self.assertEqual(list(target.iterdir()),[]);self.assertEqual(inspect_tree(source),manifest)
     w.copy_tree(source,target,expected_tree=manifest,expected_files=expected);self.assertEqual(inspect_tree(target),manifest)
 def test_missing_single_file_publication_evidence_refuses_before_target_copy(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();source=root/'source';source.write_bytes(b'new');target=root/'target';target.write_bytes(b'old');transaction=w.OutputTransaction('missing-evidence',overwrite=True);transaction.reserve(((target,False),))
   try:
    with self.assertRaisesRegex(ValueError,'changed before publication'):transaction.publish({target:source},expected_sources={})
    self.assertEqual(target.read_bytes(),b'old');self.assertFalse(transaction.targets[0].temporary.exists())
   finally:transaction.close()
   self.assertFalse(list(root.glob('.easysewer-*')))
