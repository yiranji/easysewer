"""Pinned native parser oracle and real Runner rejection/recovery."""
import hashlib,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from easysewer.io.inp import InpDocument
from easysewer.runtime import Runner
from easysewer.runtime._solver_worker import _configure_error_mode
from easysewer.runtime._process_session import ProcessSession
from easysewer.utils import probe_library_path
from test_native_v2_standard_io import direct_library
from test_native_v2_junction_gates import FAMILIES
from test_junction_gates_v2 import source,load
from test_runner_v2 import config
from test_physical_inp_v2 import fixture,physical_issues,KINDS

EVIDENCE=[]
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
def files(folder):return {s:dict(path=str(folder/('model'+s)),sha256=sha(folder/('model'+s))) for s in ('.inp','.rpt','.out')}

class NativePhysicalInpTests(unittest.TestCase):
    def open_case(self,lib,folder,raw,expected):
        folder.mkdir(parents=True)
        inp,rpt,out=[folder/('model'+s) for s in ('.inp','.rpt','.out')]
        inp.write_bytes(raw);out.write_bytes(b'previous-output')
        try:code=lib.swmm_open(os.fsencode(inp),os.fsencode(rpt),os.fsencode(out))
        finally:self.assertEqual(lib.swmm_close(),0)
        self.assertEqual(code,expected,(folder,rpt.read_text(errors='replace')))
        self.assertEqual(out.read_bytes(),b'previous-output')
        return code

    def test_record_kinds_physical_bytes_match_both_native_engines(self):
        _configure_error_mode()
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(os.environ.get('EASYSEWER_PHYSICAL_INP_OUTPUT',temporary))
            for family,name,symbol in FAMILIES:
                lib,path=direct_library(str(probe_library_path(name)),revision_symbol=symbol);rows=[]
                for kind in KINDS:
                    for encoding in (('ascii',) if kind=='blank' else ('ascii','utf8')):
                        for length in (1022,1023,1024,1025):
                            for ending in (b'',b'\n',b'\r\n',b'\r'):
                                raw,line,_=fixture(kind,length,encoding,ending)
                                folder=root/'lengths'/family/f'{kind}-{encoding}-{length}-{ending.hex() or "eof"}'
                                code=self.open_case(lib,folder,raw,200 if length>=1024 else 0)
                                issues=physical_issues(InpDocument.from_bytes(raw,source=str(folder/'model.inp')))
                                self.assertEqual(bool(issues),code!=0)
                                if issues:
                                    self.assertEqual(issues[0].span.line,line)
                                    self.assertIn(b'ERROR 201', (folder/'model.rpt').read_bytes())
                                rows.append(dict(kind=kind,encoding=encoding,length=length,ending=ending.hex(),native_code=code,
                                    diagnostics=[d.code for d in issues],line=line,files=files(folder)))
                EVIDENCE.append(dict(kind='physical-length',family=family,library_sha256=sha(path),rows=rows))

    def test_invalid_controls_in_comments_are_rejected_before_tokenization(self):
        _configure_error_mode()
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(os.environ.get('EASYSEWER_PHYSICAL_INP_OUTPUT',temporary))
            for family,name,symbol in FAMILIES:
                lib,path=direct_library(str(probe_library_path(name)),revision_symbol=symbol);rows=[]
                for kind in ('junction','comment','header'):
                    for value in (0,26):
                        for ending in (b'',b'\n',b'\r\n',b'\r'):
                            raw,line,_=fixture(kind,60,'ascii',ending)
                            position=raw.rfind(b'x');raw=raw[:position]+bytes([value])+raw[position+1:]
                            folder=root/'controls'/family/f'{kind}-{value}-{ending.hex() or "eof"}'
                            code=self.open_case(lib,folder,raw,200)
                            issues=physical_issues(InpDocument.from_bytes(raw))
                            self.assertIn('inp.native_control_character',{d.code for d in issues})
                            rows.append(dict(kind=kind,value=value,ending=ending.hex(),native_code=code,files=files(folder)))
                EVIDENCE.append(dict(kind='control-character',family=family,library_sha256=sha(path),rows=rows))

    def test_runner_rejects_before_project_open_keeps_old_outputs_and_retries(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(os.environ.get('EASYSEWER_PHYSICAL_INP_OUTPUT',temporary))
            for backend in ('swmm:standard','easysewer:flexible-ponding'):
                folder=root/'runner'/backend.split(':')[-1];folder.mkdir(parents=True)
                old={folder/('model'+s):('old'+s).encode() for s in ('.inp','.rpt','.out')}
                for path,data in old.items():path.write_bytes(data)
                raw,_,_=fixture('junction',1024,'utf8')
                raw=raw.replace(b'ALLOW_PONDING NO',b'ALLOW_PONDING YES')
                model=load(raw);before=model.to_json_document()
                with patch.object(ProcessSession,'open',side_effect=AssertionError('Invalid INP must not reach native project open')) as opened:
                    result=Runner().run(model,config(folder,backend=backend,overwrite=True,keep_failed_artifacts=False))
                self.assertFalse(opened.called);self.assertEqual(result.status,'rejected');self.assertFalse(result.native_completed)
                self.assertIn('inp.native_line_length',{d.code for d in result.diagnostics.errors})
                self.assertEqual({d.code for d in result.diagnostics.errors},{'inp.native_line_length','run.rejected'})
                self.assertEqual(model.to_json_document(),before)
                for path,data in old.items():self.assertEqual(path.read_bytes(),data)
                healthy=load(source(' 1 .2 .3 20',ponding=True))
                retry=Runner().run(healthy,config(folder,backend=backend,overwrite=True,keep_failed_artifacts=False))
                self.assertEqual(retry.status,'succeeded',retry.diagnostics);self.assertTrue(retry.native_completed)
                EVIDENCE.append(dict(kind='runner-retry',backend=backend,rejected_before_open=True,source_unchanged=True,old_outputs_preserved=True,files=files(folder)))
