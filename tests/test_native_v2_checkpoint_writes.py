"""All native output owners with a content-verifying private prefix provider.

This is an internal native integration harness, not the public worker/container.
"""
import base64
import ctypes as c
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import tempfile
import unittest

from test_native_v2_checkpoint_output import Engine as OutputEngine, fixture as output_fixture, OPEN
from test_native_v2_checkpoint_lid import fixture as lid_fixture
from test_native_v2_checkpoint_climate import fixture as climate_fixture
from test_native_v2_standard_io import handles

PREFIX=c.CFUNCTYPE(c.c_int,c.c_void_p,c.c_int,c.c_uint32,c.c_int,c.c_uint64,c.c_void_p,c.c_size_t)
EVIDENCE=[]


class Engine(OutputEngine):
    def __init__(self,*args):
        super().__init__(*args)
        for name,args,result in (
            ('es_test_writes_save',[c.c_void_p,c.c_size_t,c.POINTER(c.c_size_t),c.c_int],c.c_int),
            ('es_test_writes_restore',[c.c_void_p,c.c_size_t,c.c_int,c.c_int,OPEN,PREFIX,c.c_int,c.POINTER(c.c_int),c.POINTER(c.c_int)],c.c_int),
            ('es_test_writes_info',[c.c_uint32,c.POINTER(c.c_int),c.POINTER(c.c_int),c.POINTER(c.c_uint64),c.c_void_p,c.c_size_t],c.c_int),
            ('es_test_writes_drop',[],c.c_int),('es_test_writes_calls',[],c.c_int),
            ('es_test_writes_hit',[],c.c_int),('es_test_writes_arm',[c.c_int],None),
        ):
            f=getattr(self.lib,name);f.argtypes,f.restype=args,result
        self.prefixes={};self.prefix_calls=0;self.prefix_mode='normal';self.prefix_fail=-1
        self.staging=self.root/'prefixes';self.staging.mkdir();self.prepared=[]
        def provider(context,role,index,text,bytes_,path,capacity):
            call=self.prefix_calls;self.prefix_calls+=1
            if call==self.prefix_fail:return 7
            try:
                entry=self.prefixes[index];raw=entry['data']
                if (role,text,len(raw))!=(entry['role'],entry['text'],bytes_):return 3
                if hashlib.sha256(raw).hexdigest()!=entry['sha256']:return 3
                destination=self.staging/str(len(self.prepared))
                if self.prefix_mode=='alias':destination=Path(entry['path'])
                elif self.prefix_mode=='duplicate' and index:
                    destination=self.prepared[-1];destination.write_bytes(raw)
                elif self.prefix_mode=='hardlink':os.link(entry['path'],destination)
                elif self.prefix_mode=='missing':pass
                elif self.prefix_mode=='short':destination.write_bytes(raw[:-1])
                else:destination.write_bytes(raw)
                self.prepared.append(destination)
                encoded=os.fsencode(destination)
                if len(encoded)>=capacity:return 2
                c.memmove(path,encoded+b'\0',len(encoded)+1)
                return 0
            except (KeyError,OSError,ValueError):return 7
        self.write_provider=PREFIX(provider)

    def dump(self,only=False):
        size=c.c_size_t();self.check(self.lib.es_test_writes_save(None,0,c.byref(size),only))
        raw=c.create_string_buffer(size.value)
        self.check(self.lib.es_test_writes_save(raw,size.value,c.byref(size),only))
        return raw.raw

    def inventory(self):
        result={}
        self.lib.es_test_writes_arm(-1)
        for index in range(10000):
            role,text,bytes_=c.c_int(),c.c_int(),c.c_uint64();path=c.create_string_buffer(4096)
            count=self.lib.es_test_writes_info(index,c.byref(role),c.byref(text),c.byref(bytes_),path,len(path))
            if count==0:return result
            self.check(-count if count<0 else 0)
            name=Path(os.fsdecode(path.value));raw=name.read_bytes()
            assert len(raw)==bytes_.value
            result[index]=dict(role=role.value,text=text.value,path=str(name),data=raw,sha256=hashlib.sha256(raw).hexdigest())
        raise AssertionError('Unbounded output inventory')

    def capture(self):
        raw=self.dump();self.prefixes=self.inventory();return raw

    def restore(self,raw,fail_at=-1,only=False,write_fail=-1):
        cleanup,committed=c.c_int(),c.c_int();self.prefix_calls=0
        error=self.lib.es_test_writes_restore(c.create_string_buffer(bytes(raw)),len(raw),fail_at,write_fail,
            self.provider,self.write_provider,only,c.byref(cleanup),c.byref(committed))
        self.cleanup,self.committed=cleanup.value,bool(committed.value)
        self.write_calls,self.write_hit=self.lib.es_test_writes_calls(),self.lib.es_test_writes_hit()
        return error

    def damage(self):
        super().damage();self.check(self.lib.es_test_writes_drop())

    def finish(self,roots=()):
        outputs=self.inventory()
        try:
            self.check(self.lib.swmm_end());self.check(self.lib.swmm_report())
        finally:
            self.lib.swmm_close();self.closed=True
        result={}
        for index,item in outputs.items():
            raw=Path(item['path']).read_bytes()
            if item['role']==0:
                raw=re.sub(rb'(?m)^[ \t]*(?:Analysis (?:begun|ended)|Total elapsed time)[^\r\n]*',b'',raw)
            if item['text']:
                for root in (*roots,self.root):raw=raw.replace(os.fsencode(root),b'<workspace>')
            result[str(index)]=dict(role=item['role'],text=item['text'],sha256=hashlib.sha256(raw).hexdigest(),size=len(raw))
        return result


def fixture(root,name,units='CFS'):
    root=Path(root)
    if name in ('lid','all','all-si'):
        source=lid_fixture(root,'BC',variant='multiple',units=units)
    else:source=output_fixture(root,name,units,True)
    if name in ('runoff','all','all-si'):source+=f'\n[FILES]\nSAVE RUNOFF "{root / "saved-runoff.bin"}"\n'
    if name in ('hydraulic','all','all-si','split'):source+=f'\n[FILES]\nSAVE OUTFLOWS "{root / "saved-outflows.txt"}"\n'
    source+=f'\n[FILES]\nSAVE HOTSTART "{root / "saved-hotstart.bin"}"\n'
    return source


def write_layout(raw):
    start=raw.index(b'ESWRITE1');pos=start+8
    count=struct.unpack_from('<I',raw,pos)[0];bindings=[pos];pos+=4;lengths=[]
    for _ in range(count):
        for _ in range(3):bindings.append(pos);pos+=4
        for _ in range(2):
            bindings.append(pos);size=struct.unpack_from('<I',raw,pos)[0];pos+=4
            if size:bindings.append(pos)
            pos+=size
        lengths.append(pos);pos+=8
    assert pos==len(raw)
    return dict(start=start,count=count,bindings=bindings,lengths=lengths)


def encoded_prefixes(prefixes):
    return {str(k):dict(v,data=base64.b64encode(v['data']).decode()) for k,v in prefixes.items()}


def decoded_prefixes(prefixes):
    return {int(k):dict(v,data=base64.b64decode(v['data'])) for k,v in prefixes.items()}


@unittest.skipUnless(os.environ.get('EASYSEWER_CHECKPOINT_STANDARD') and os.environ.get('EASYSEWER_CHECKPOINT_CUSTOM'),
                     'Requires internal output-stream transaction candidates')
class NativeCheckpointWritesTests(unittest.TestCase):
    def engine(self,family,root,source):return Engine(os.environ['EASYSEWER_CHECKPOINT_'+family.upper()],root,source)

    def run_case(self,family,name,units='CFS',restore=False):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);e=self.engine(family,root,fixture(root,name,units));trace=[];roles=set()
            try:
                for step in range(20000):
                    raw=e.capture();write_layout(raw);roles.update(p['role'] for p in e.prefixes.values())
                    if restore and step%3==0:
                        previous=dict(e.prefixes)
                        e.damage();self.assertEqual(e.restore(raw),0)
                        self.assertTrue(e.committed);self.assertEqual(e.cleanup,0);self.assertEqual(e.dump(),raw)
                        for p in previous.values():self.assertEqual(Path(p['path']).read_bytes(),p['data'])
                    row=e.split_step(units) if name=='split' else e.step();trace.append(row)
                    if not row[0]:break
                else:self.fail('did not finish')
                result=e.finish()
            finally:e.close()
            return trace,result,sorted(roles)

    def test_restored_streams_keep_all_completed_native_artifacts_identical(self):
        for family in ('standard','custom'):
            for name in ('hydraulic','averages','runoff','lid','all','empty','no-routing','report-disabled')+(('split',) if family=='custom' else ()):
                for units in ('CFS','CMS'):
                    with self.subTest(family=family,case=name,units=units):
                        expected=self.run_case(family,name,units)
                        self.assertEqual(self.run_case(family,name,units,True),expected)
                        if name=='all':self.assertEqual(expected[2],list(range(6)))
                        EVIDENCE.append(dict(kind='restore',family=family,case=name,units=units,steps=len(expected[0]),roles=expected[2],outputs=expected[1]))

    def test_validation_rejects_before_provider_and_retains_live_outputs(self):
        for family in ('standard','custom'):
            with tempfile.TemporaryDirectory() as folder:
                root=Path(folder);e=self.engine(family,root,fixture(root,'all'))
                try:
                    for _ in range(7):e.step()
                    raw=e.capture();info=write_layout(raw);bad=[raw[:i] for i in range(info['start'],len(raw))]
                    bad.append(raw+b'x')
                    for offset in info['bindings']:
                        b=bytearray(raw);b[offset]^=1;bad.append(b)
                    for offset in info['lengths']:
                        b=bytearray(raw);struct.pack_into('<Q',b,offset,2**64-1);bad.append(b)
                    before=handles()
                    for b in bad:
                        self.assertNotEqual(e.restore(b),0);self.assertFalse(e.committed)
                        self.assertEqual(e.prefix_calls,0);self.assertEqual(e.dump(),raw)
                        self.assertEqual(handles(),before)
                    EVIDENCE.append(dict(kind='corruption',family=family,payloads=len(bad),streams=info['count']))
                finally:e.close()

    def test_provider_and_native_stage_failures_are_transactional(self):
        for family in ('standard','custom'):
            with tempfile.TemporaryDirectory() as folder:
                root=Path(folder);e=self.engine(family,root,fixture(root,'all'))
                try:
                    for _ in range(7):e.step()
                    raw=e.capture();before=handles()
                    for fail in range(len(e.prefixes)):
                        e.prefix_fail=fail
                        self.assertNotEqual(e.restore(raw),0);self.assertFalse(e.committed)
                        self.assertEqual(e.dump(),raw);self.assertEqual(handles(),before)
                    e.prefix_fail=-1
                    for mode in ('alias','hardlink','missing','short','duplicate'):
                        e.prefix_mode=mode
                        self.assertNotEqual(e.restore(raw),0);self.assertFalse(e.committed)
                        self.assertEqual(e.dump(),raw);self.assertEqual(handles(),before)
                        for p in e.prefixes.values():self.assertEqual(Path(p['path']).read_bytes(),p['data'])
                    e.prefix_mode='normal'
                    self.assertEqual(e.restore(raw),0);calls=e.write_calls
                    for fail in range(calls):
                        raw=e.capture()
                        error=e.restore(raw,write_fail=fail)
                        self.assertTrue(e.write_hit)
                        if e.committed:self.assertEqual((error,e.cleanup),(0,7))
                        else:self.assertIn(error,(6,7))
                        self.assertEqual(e.dump(),raw);self.assertEqual(handles(),before)
                        self.assertEqual(e.restore(raw),0)
                    EVIDENCE.append(dict(kind='faults',family=family,native_points=calls,streams=len(e.prefixes)))
                finally:e.close()

    def test_restore_earlier_checkpoint_preserves_retired_future_files(self):
        for family in ('standard','custom'):
            for name in ('all',)+(('split',) if family=='custom' else ()):
                with self.subTest(family=family,case=name),tempfile.TemporaryDirectory() as folder:
                    expected=self.run_case(family,name);root=Path(folder)
                    e=self.engine(family,root,fixture(root,name));trace=[]
                    try:
                        for _ in range(4):trace.append(e.split_step('CFS') if name=='split' else e.step())
                        raw=e.capture()
                        for _ in range(2):e.split_step('CFS') if name=='split' else e.step()
                        future=e.inventory()
                        self.assertEqual(e.restore(raw),0);self.assertTrue(e.committed)
                        self.assertEqual(e.dump(),raw)
                        for entry in future.values():self.assertEqual(Path(entry['path']).read_bytes(),entry['data'])
                        for _ in range(20000):
                            row=e.split_step('CFS') if name=='split' else e.step();trace.append(row)
                            if not row[0]:break
                        else:self.fail('did not finish')
                        self.assertEqual(trace,expected[0]);self.assertEqual(e.finish(),expected[1])
                        EVIDENCE.append(dict(kind='rewind',family=family,case=name))
                    finally:e.close()

    def test_capture_rejects_external_tail_and_can_restore_unmodified_snapshot(self):
        for family in ('standard','custom'):
            with tempfile.TemporaryDirectory() as folder:
                root=Path(folder);e=self.engine(family,root,fixture(root,'all'))
                try:
                    e.step();raw=e.capture();entry=next(p for p in e.prefixes.values() if p['role']==1)
                    path=Path(entry['path'])
                    with path.open('ab') as stream:stream.write(b'external-tail')
                    with self.assertRaises(AssertionError):e.dump()
                    self.assertEqual(e.restore(raw),0);self.assertEqual(e.dump(),raw)
                    self.assertEqual(path.read_bytes(),entry['data']+b'external-tail')
                    EVIDENCE.append(dict(kind='capture-tail',family=family))
                finally:e.close()

    def test_capture_flush_and_file_identity_failures_do_not_publish_a_snapshot(self):
        for family in ('standard','custom'):
            with tempfile.TemporaryDirectory() as folder:
                root=Path(folder);e=self.engine(family,root,fixture(root,'all'))
                try:
                    e.step();raw=e.capture()
                    for fail in (0,1):
                        e.lib.es_test_writes_arm(fail)
                        role,text,bytes_=c.c_int(),c.c_int(),c.c_uint64();path=c.create_string_buffer(4096)
                        self.assertEqual(e.lib.es_test_writes_info(0,c.byref(role),c.byref(text),c.byref(bytes_),path,len(path)),-7)
                        self.assertEqual(e.dump(),raw)
                    EVIDENCE.append(dict(kind='capture-fault',family=family,points=2))
                finally:e.close()

    def test_prefix_digest_mismatch_and_length_mismatch_reject_without_mutation(self):
        for family in ('standard','custom'):
            with tempfile.TemporaryDirectory() as folder:
                root=Path(folder);e=self.engine(family,root,fixture(root,'all'))
                try:
                    e.step();raw=e.capture();info=write_layout(raw)
                    for index,prefix in e.prefixes.items():
                        original=prefix['data'];prefix['data']=bytes([original[0]^1])+original[1:]
                        self.assertNotEqual(e.restore(raw),0);self.assertFalse(e.committed)
                        prefix['data']=original;self.assertEqual(e.dump(),raw)
                        b=bytearray(raw);struct.pack_into('<Q',b,info['lengths'][index],len(original)+1)
                        self.assertNotEqual(e.restore(b),0);self.assertFalse(e.committed)
                        self.assertEqual(e.dump(),raw)
                finally:e.close()

    def test_native_fresh_process_continues_from_captured_prefixes(self):
        for family in ('standard','custom'):
            for name in ('averages','all')+(('split',) if family=='custom' else ()):
                with self.subTest(family=family,case=name),tempfile.TemporaryDirectory() as folder:
                    expected=self.run_case(family,name)
                    root=Path(folder);first=root/'first';second=root/'second';first.mkdir();second.mkdir()
                    # A prior TEMPERATURE FILE project leaves an unused
                    # fileStartDate behind. It must not bind a later project
                    # without that input to the old process's history.
                    warm=root/'warm';warm.mkdir()
                    prior=self.engine(family,warm,climate_fixture(warm,shifted=True))
                    try:prior.step()
                    finally:prior.close()
                    source=fixture(first,name);e=self.engine(family,first,source);trace=[]
                    try:
                        for _ in range(4):trace.append(e.split_step('CFS') if name=='split' else e.step())
                        raw=e.capture();prefixes=encoded_prefixes(e.prefixes)
                    finally:e.close()
                    (root/'state').write_bytes(raw)
                    (root/'source').write_text(source.replace(str(first),str(second)),encoding='utf-8')
                    (root/'prefixes.json').write_text(json.dumps(prefixes),encoding='utf-8')
                    code='''import json,sys
from pathlib import Path
from test_native_v2_checkpoint_writes import Engine,decoded_prefixes
lib,root,name=sys.argv[1:];root=Path(root)
e=Engine(lib,root/'second',(root/'source').read_text(encoding='utf-8'))
try:
 e.prefixes=decoded_prefixes(json.loads((root/'prefixes.json').read_text()))
 error=e.restore((root/'state').read_bytes())
 assert error==0 and e.committed and e.cleanup==0,(error,e.committed,e.cleanup,e.prefix_calls)
 trace=[]
 for _ in range(20000):
  row=e.split_step('CFS') if name=='split' else e.step();trace.append(row)
  if not row[0]:break
 else:raise AssertionError('did not finish')
 outputs=e.finish((root/'first',))
 (root/'result.json').write_text(json.dumps(dict(trace=trace,outputs=outputs)))
finally:e.close()
'''
                    env=dict(os.environ,PYTHONPATH=os.pathsep.join((str(Path(__file__).parent),str(Path(__file__).parents[1]/'src'))))
                    p=subprocess.run([sys.executable,'-B','-c',code,os.environ['EASYSEWER_CHECKPOINT_'+family.upper()],str(root),name],
                        env=env,capture_output=True,text=True,timeout=60)
                    self.assertEqual(p.returncode,0,p.stdout+p.stderr)
                    actual=json.loads((root/'result.json').read_text())
                    self.assertEqual(trace+[tuple(v) for v in actual['trace']],expected[0])
                    self.assertEqual(actual['outputs'],expected[1])
                    EVIDENCE.append(dict(kind='fresh-native',family=family,case=name,outputs=actual['outputs']))


if __name__=='__main__':unittest.main()
