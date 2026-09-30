"""Direct native syntax/line boundaries and preserved outputs after errors."""
import hashlib,os,tempfile,unittest
from pathlib import Path
from easysewer.runtime._solver_worker import _configure_error_mode
from easysewer.utils import probe_library_path
from test_native_v2_standard_io import direct_library
from test_native_v2_title_report_gates import solve
from test_junction_gates_v2 import source,load,UNITS
from test_native_v2_junction_gates import FAMILIES

EVIDENCE=[]
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()


class NativeJunctionBoundaryTests(unittest.TestCase):
    def opening(self,lib,folder,raw,expected):
        folder.mkdir(parents=True);inp,rpt,out=[folder/('model'+suffix) for suffix in ('.inp','.rpt','.out')]
        inp.write_bytes(raw);out.write_bytes(b'previous-output')
        try:code=lib.swmm_open(os.fsencode(inp),os.fsencode(rpt),os.fsencode(out))
        finally:self.assertEqual(lib.swmm_close(),0)
        self.assertEqual(code,expected,(folder,rpt.read_text(errors='replace')))
        self.assertEqual(out.read_bytes(),b'previous-output')
        if code==200:self.assertIn('[JUNC]',rpt.read_text(errors='replace'))
        if code==138:self.assertIn('ERROR 138',rpt.read_text(errors='replace'))
        return dict(case=folder.name,code=code,files={s:dict(path=str(folder/('model'+s)),sha256=sha(folder/('model'+s))) for s in ('.inp','.rpt','.out')})

    def test_scalar_bounds_missing_items_extra_fields_and_error_recovery(self):
        _configure_error_mode()
        cases=[('missing','J',200),('number','J bad',200),('negative-max','J 0 -1',200),
            ('negative-initial','J 0 1 -.1',200),('negative-surcharge','J 0 1 0 -.1',200),
            ('negative-area','J 0 1 0 0 -.1',200),('extra','J 0 1 0 0 20 extra',0),
            ('initial-bound','J 0 1 2 0',138),('negative-elevation','J -2',0)]
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(os.environ.get('EASYSEWER_JUNCTION_BOUNDARY_OUTPUT',temporary))
            for family,name,symbol in FAMILIES:
                lib,path=direct_library(str(probe_library_path(name)),revision_symbol=symbol)
                rows=[]
                for units in UNITS:
                    for label,row,expected in cases:
                        text=source().replace('FLOW_UNITS CFS','FLOW_UNITS '+units).replace('J 0 ; junction',row)
                        folder=root/'scalars'/family/(units+'-'+label)
                        data=self.opening(lib,folder,text.encode(),expected)
                        model=load(text,strict=False)
                        if label=='extra':
                            self.assertFalse(model.validate().is_valid)
                            self.assertEqual(model.document.text,text)
                        elif expected:
                            self.assertFalse(model.validate(for_run=True).is_valid)
                        else:self.assertTrue(model.validate(for_run=True).is_valid)
                        rows.append(dict(data,units=units,label=label))
                retry=root/'scalars'/family/'retry';retry.mkdir(parents=True)
                result=solve(self,lib,retry,source(' 1 .2 .3 20',ponding=True))
                EVIDENCE.append(dict(kind='scalar-boundary',family=family,library_sha256=sha(path),rows=rows,
                    retry=dict(out_sha256=hashlib.sha256(result['out']).hexdigest(),files={s:dict(path=str(retry/('model'+s)),sha256=sha(retry/('model'+s))) for s in ('.inp','.rpt','.out')})))

    def test_physical_line_byte_limit_comments_and_line_endings(self):
        _configure_error_mode()
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(os.environ.get('EASYSEWER_JUNCTION_BOUNDARY_OUTPUT',temporary))
            base=source(' 1 .2 .3 20').replace('[JUNCTIONS]\nJ 0 1 .2 .3 20 ; junction\n','').encode()+b'[JUNCTIONS]\n'
            prefix=b'J 0 1 .2 .3 20 ; '
            for family,name,symbol in FAMILIES:
                lib,path=direct_library(str(probe_library_path(name)),revision_symbol=symbol);rows=[]
                for length in (1022,1023,1024,1025):
                    for encoding in ('ascii','utf8'):
                        budget=length-len(prefix)
                        padding=b'x'*budget if encoding=='ascii' else '雨'.encode()*(budget//3)+b'x'*(budget%3)
                        line=prefix+padding;self.assertEqual(len(line),length)
                        for ending in (b'',b'\n',b'\r\n'):
                            raw=base+line+ending;folder=root/'physical'/family/f'{length}-{encoding}-{len(ending)}'
                            data=self.opening(lib,folder,raw,0 if length<1024 else 200)
                            model=load(raw)
                            self.assertEqual(model.to_document().to_bytes(),raw)
                            valid=model.validate(for_run=True).is_valid
                            rows.append(dict(data,length=length,encoding=encoding,terminator=ending.hex(),model_run_valid=valid))
                            with self.subTest(family=family,length=length,encoding=encoding,terminator=ending.hex()):
                                self.assertEqual(valid,length<1024)
                EVIDENCE.append(dict(kind='physical-line-boundary',family=family,library_sha256=sha(path),rows=rows))
