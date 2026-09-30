"""Every TAGS variant is inert for complete standard/custom solver output."""
import hashlib,os,re,tempfile,unittest
from pathlib import Path
from easysewer import get_native_capabilities
from easysewer.model import Ref
from easysewer.model.project import ObjectTag
from easysewer.runtime._solver_worker import _configure_error_mode
from easysewer.utils import probe_library_path
from test_native_v2_standard_io import direct_library,execute
from test_tag_gates_v2 import CASES,TARGETS,UNITS,base_model,parse,values
from test_scenario_v2 import portable

EVIDENCE=[]


@unittest.skipUnless(get_native_capabilities()['swmm_solver'] and get_native_capabilities()['flexible_ponding'],
                    'Both packaged native solvers required')
class NativeTagGateTests(unittest.TestCase):
    def test_tag_record_byte_limit_terminators_rejection_and_same_process_retry(self):
        _configure_error_mode()
        sha=lambda data:hashlib.sha256(data).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(os.environ.get('EASYSEWER_TAG_GATE_OUTPUT',tmp)).resolve()
            for family,name,symbol in (('standard','swmm5','swmm_getEasySewerStandardFixes'),
                                       ('custom','flexible_ponding','swmm_getEasySewerNativeIOFixes')):
                lib,library=direct_library(probe_library_path(name),revision_symbol=symbol)
                base=(base_model().to_document().text+'[TAGS]\n').encode('utf-8')
                observations=[]
                for length in (1022,1023,1024,1025):
                    for encoding in ('ascii','utf8'):
                        budget=length-len(b'Node J ""')
                        payload=b'x'*budget if encoding=='ascii' else '雨'.encode()*(budget//3)+b'x'*(budget%3)
                        line=b'Node J "'+payload+b'"';self.assertEqual(len(line),length)
                        for ending in (b'',b'\n',b'\r\n'):
                            case=root/'boundaries'/family/f'{length}-{encoding}-{len(ending)}';case.mkdir(parents=True)
                            inp,rpt,out=(case/('model'+suffix) for suffix in ('.inp','.rpt','.out'))
                            inp.write_bytes(base+line+ending);out.write_bytes(b'old-output')
                            try:code=lib.swmm_open(os.fsencode(inp),os.fsencode(rpt),os.fsencode(out))
                            finally:self.assertEqual(lib.swmm_close(),0)
                            self.assertEqual(code,0 if length<1024 else 200,(family,length,encoding,ending))
                            self.assertEqual(out.read_bytes(),b'old-output')
                            observations.append(dict(length=length,encoding=encoding,terminator=ending.hex(),code=code,
                                input_sha256=sha(inp.read_bytes()),report_sha256=sha(rpt.read_bytes())))
                retry=root/'boundaries'/family/'retry';retry.mkdir(parents=True)
                self.assertTrue(all(code==0 for code in execute(lib,retry,base_model().to_document().text)))
                EVIDENCE.append(dict(kind='physical-tag-record-boundary',family=family,cases=observations,
                    valid_open=sum(r['code']==0 for r in observations),refused_open=sum(r['code']==200 for r in observations),
                    same_process_retry_out_sha256=sha((retry/'model.out').read_bytes()),library_sha256=sha(library.read_bytes())))

    def test_all_variants_six_units_literal_created_edited_and_json_keep_full_outputs(self):
        _configure_error_mode()
        sha=lambda data:hashlib.sha256(data).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(os.environ.get('EASYSEWER_TAG_GATE_OUTPUT',tmp)).resolve()
            root.mkdir(parents=True,exist_ok=True)
            for family,name,symbol in (('standard','swmm5','swmm_getEasySewerStandardFixes'),
                                       ('custom','flexible_ponding','swmm_getEasySewerNativeIOFixes')):
                lib,library=direct_library(probe_library_path(name),revision_symbol=symbol)
                for units in UNITS:
                    base=base_model(units);baseline=base.to_document().text
                    literal=baseline+'[TAGS]\n'+''.join(body for body,_ in CASES.values())+'[TAGS]\nLink P ""\n'
                    loaded=parse(literal)
                    expected=('rain zone','catch zone','node zone','')
                    created=base.copy()
                    for (_,ns,key),text in zip(TARGETS,expected):
                        created.tags.add(ObjectTag(target=Ref(collection=ns,key=key),text=text))
                    self.assertEqual(values(loaded),values(created))
                    edited=loaded.copy()
                    for key in tuple(edited.tags):edited.tags.update(key,text='edited');edited.tags.remove(key)
                    self.assertFalse(edited.tags)
                    sources={'baseline':baseline,'literal':literal,'created':created.to_document().text,
                             'json':portable(loaded).to_document().text,'edited-cleared':edited.to_document().text}
                    outputs=[];runs=[]
                    for label,source in sources.items():
                        directory=root/family/units/label;directory.mkdir(parents=True,exist_ok=False)
                        codes=execute(lib,directory,source)
                        self.assertTrue(all(code==0 for code in codes),(family,units,label,codes))
                        out=(directory/'model.out').read_bytes();rpt=(directory/'model.rpt').read_bytes()
                        normalized=re.sub(rb'(?m)^[ \t]*(?:Analysis (?:begun|ended)|Total elapsed time)[^\r\n]*',b'',rpt)
                        outputs.append((out,normalized))
                        runs.append(dict(label=label,codes=codes,
                            files={suffix:dict(path=str(directory/('model'+suffix)),
                                sha256=sha((directory/('model'+suffix)).read_bytes())) for suffix in ('.inp','.rpt','.out')},
                            normalized_report_sha256=sha(normalized)))
                    self.assertTrue(all(pair==outputs[0] for pair in outputs[1:]),(family,units))
                    self.assertGreater(len(outputs[0][0]),100)
                    EVIDENCE.append(dict(family=family,units=units,variants=list(CASES),
                        library_sha256=sha(library.read_bytes()),library=str(library),runs=runs,
                        complete_out_equal=True,complete_normalized_report_equal=True))


if __name__=='__main__':unittest.main()
