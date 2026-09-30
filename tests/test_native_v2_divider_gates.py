"""Complete native results for active/inactive divider laws and optional fields."""
import hashlib
import math
import os
from pathlib import Path
import tempfile
import unittest
from dataclasses import replace

from easysewer import get_native_capabilities
from easysewer.io.output import OutputReader
from easysewer.model import Ref,FileReference
from easysewer.runtime import Runner,RunConfig,FlexiblePondingBackend
from easysewer.runtime._solver_worker import _configure_error_mode
from easysewer.results import swmm_output_variables
from test_divider_gates_v2 import KINDS,UNITS,TAILS,Q_FROM_CFS,source,created,load,portable,row
from test_native_v2_regulator_fields import FAMILIES,library
from test_native_v2_title_report_gates import solve

EVIDENCE=[]

def observed(test,lib,folder,text):
    folder.mkdir(parents=True)
    result=solve(test,lib,folder,text)
    with OutputReader(folder/'model.out') as output:
        diverted=output.series(Ref(collection='swmm:links',key='D'),'swmm:flow').values
        main=output.series(Ref(collection='swmm:links',key='P'),'swmm:flow').values
        test.assertTrue(all(math.isfinite(v) for v in (*diverted,*main)))
    record=dict(diverted_min=min(diverted),diverted_max=max(diverted),diverted_last=diverted[-1],
        main_max=max(main),main_last=main[-1],out_sha256=hashlib.sha256(result['out']).hexdigest(),
        normalized_report_sha256=hashlib.sha256(result['report']).hexdigest(),
        files={s:dict(path=str(folder/('model'+s)),sha256=hashlib.sha256((folder/('model'+s)).read_bytes()).hexdigest()) for s in ('.inp','.rpt','.out')})
    return result,record

@unittest.skipUnless(get_native_capabilities()['swmm_solver'] and get_native_capabilities()['flexible_ponding'],
                     'Both packaged native engines required')
class NativeDividerGateTests(unittest.TestCase):
    def test_all_laws_optional_tails_units_and_supported_routing_match_full_results(self):
        _configure_error_mode()
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(os.environ.get('EASYSEWER_DIVIDER_GATE_OUTPUT',temporary))/'full'
            for family,name,symbol in FAMILIES:
                lib,path=library(name,symbol);dynamic={}
                for routing in (('STEADY','KINWAVE','DYNWAVE') if family=='standard' else ('DYNWAVE',)):
                    for units in UNITS:
                        for kind in KINDS:
                            omitted=None
                            for tail in TAILS:
                                with self.subTest(family=family,routing=routing,units=units,kind=kind,tail=tail):
                                    base=root/family/routing/units/kind/tail
                                    text=source(kind,tail,units,routing)
                                    expected,record=observed(self,lib,base/'literal',text)
                                    model=load(text);fresh=created(kind,tail,units,routing)
                                    forms={'created':fresh.to_document().text,'normalized':model.to_document(normalize=True).text,
                                           'json':portable(model).to_document(normalize=True).text}
                                    variants={}
                                    for form,inp in forms.items():
                                        actual,entry=observed(self,lib,base/form,inp)
                                        self.assertEqual(actual,expected,(family,routing,units,kind,tail,form));variants[form]=entry
                                    self.assertGreater(record['diverted_max'],0)
                                    if tail=='omitted':omitted=expected
                                    if tail=='zero':self.assertEqual(expected,omitted)
                                    if routing=='DYNWAVE':
                                        key=(units,tail)
                                        if kind=='OVERFLOW':dynamic[key]=expected
                                        else:self.assertEqual(expected,dynamic[key],'Inactive law changed dynamic-wave results')
                                    EVIDENCE.append(dict(kind='divider-native-matrix',family=family,routing=routing,units=units,
                                        law=kind,tail=tail,literal=record,variants=variants,library_sha256=hashlib.sha256(Path(path).read_bytes()).hexdigest()))

    def test_low_flow_thresholds_and_native_link_sorting_preserve_complete_results(self):
        _configure_error_mode()
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(os.environ.get('EASYSEWER_DIVIDER_GATE_OUTPUT',temporary))/'thresholds'
            lib,_=library('swmm5','swmm_getEasySewerStandardFixes')
            for units in UNITS:
                for kind in KINDS:
                    records=[]
                    for inflow in (.1,5):
                        with self.subTest(units=units,kind=kind,inflow=inflow):
                            base=root/units/kind/str(inflow)
                            text=source(kind,'omitted',units,'STEADY',inflow)
                            expected,record=observed(self,lib,base/'literal',text)
                            model=created(kind,'omitted',units,'STEADY',inflow)
                            actual,entry=observed(self,lib,base/'created',model.to_document().text)
                            self.assertEqual(actual,expected)
                            # Changing declaration order changes OUT identifiers; compare all
                            # per-ID sampled values as well as normalized report content.
                            model.links.move('P',before='D')
                            moved,moved_entry=observed(self,lib,base/'reordered',model.to_document().text)
                            with OutputReader(base/'literal/model.out') as left,OutputReader(base/'reordered/model.out') as right:
                                for variable in swmm_output_variables().entries:
                                    if variable.pollutant or variable.collection=='swmm:subcatchments':continue
                                    keys=(None,) if variable.collection=='swmm:system' else left.metadata.names(variable.collection)
                                    for key in keys:
                                        owner=None if key is None else Ref(collection=variable.collection,key=key)
                                        a,b=left.series(owner,variable.key),right.series(owner,variable.key)
                                        self.assertEqual(replace(a,source=b.source),b)
                            self.assertEqual(sorted(moved['report'].splitlines()),sorted(expected['report'].splitlines()))
                            self.assertEqual(record['diverted_last'],moved_entry['diverted_last'])
                            if inflow==.1 and kind!='TABULAR':self.assertEqual(record['diverted_max'],0)
                            else:self.assertGreater(record['diverted_max'],0)
                            records.append(dict(inflow=inflow,literal=record,created=entry,reordered=moved_entry))
                    self.assertGreater(records[1]['literal']['diverted_last'],records[0]['literal']['diverted_last'])
                    EVIDENCE.append(dict(kind='low-high-threshold',units=units,law=kind,rows=records))

    def test_missing_diversion_and_invalid_weir_reject_without_losing_retry(self):
        _configure_error_mode()
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(os.environ.get('EASYSEWER_DIVIDER_GATE_OUTPUT',temporary))/'invalid'
            for family,name,symbol in FAMILIES:
                lib,_=library(name,symbol)
                routing='STEADY' if family=='standard' else 'DYNWAVE'
                for units in UNITS:
                    for kind in KINDS:
                        for tail in ('omitted','full'):
                            with self.subTest(family=family,units=units,kind=kind,tail=tail):
                                base=root/family/units/kind/tail;base.mkdir(parents=True)
                                text=source(kind,tail,units,routing)
                                bad=text.replace(row(kind,tail,units),row(kind,tail,units).replace(' D ',' * ',1))
                                model=load(bad)
                                self.assertIn('divider.missing_link',{d.code for d in model.validate(for_run=True).errors})
                                inp,rpt,out=(base/('model'+s) for s in ('.inp','.rpt','.out'))
                                inp.write_text(bad,encoding='utf-8');out.write_bytes(b'previous-output')
                                try:code=lib.swmm_open(os.fsencode(inp),os.fsencode(rpt),os.fsencode(out))
                                finally:self.assertEqual(lib.swmm_close(),0)
                                self.assertNotEqual(code,0);self.assertIn(b'ERROR 136',rpt.read_bytes())
                                self.assertEqual(out.read_bytes(),b'previous-output')
                                invalid={s:dict(path=str(base/('model'+s)),sha256=hashlib.sha256((base/('model'+s)).read_bytes()).hexdigest()) for s in ('.inp','.rpt','.out')}
                                _,retry=observed(self,lib,base/'retry',text)
                                EVIDENCE.append(dict(kind='missing-diversion-retry',family=family,units=units,law=kind,tail=tail,code=code,
                                    invalid_files=invalid,retry=retry))
                    for minimum in (0,1,2):
                        with self.subTest(family=family,units=units,weir_minimum=minimum):
                            base=root/family/units/'weir-range'/str(minimum);flow=Q_FROM_CFS[units]
                            text=source('WEIR','omitted',units,routing)
                            replacement=f'J {10 if units in UNITS[:3] else 3.048:.17g} D WEIR {minimum*flow:.17g} 1 {flow:.17g}'
                            text=text.replace(row('WEIR','omitted',units),replacement);model=load(text)
                            self.assertEqual(model.validate(for_run=True).is_valid,minimum<=1)
                            if minimum<=1:
                                expected,record=observed(self,lib,base/'literal',text)
                                actual,entry=observed(self,lib,base/'json',portable(model).to_document(normalize=True).text)
                                self.assertEqual(actual,expected)
                                EVIDENCE.append(dict(kind='weir-range-valid',family=family,units=units,minimum=minimum,literal=record,json=entry))
                            else:
                                base.mkdir(parents=True);inp,rpt,out=(base/('model'+s) for s in ('.inp','.rpt','.out'))
                                inp.write_text(text,encoding='utf-8');out.write_bytes(b'previous-output')
                                try:code=lib.swmm_open(os.fsencode(inp),os.fsencode(rpt),os.fsencode(out))
                                finally:self.assertEqual(lib.swmm_close(),0)
                                self.assertNotEqual(code,0);self.assertIn(b'ERROR 137',rpt.read_bytes())
                                self.assertEqual(out.read_bytes(),b'previous-output')
                                _,retry=observed(self,lib,base/'retry',source('WEIR','omitted',units,routing))
                                EVIDENCE.append(dict(kind='weir-range-invalid',family=family,units=units,code=code,
                                    invalid_files={s:dict(path=str(base/('model'+s)),sha256=hashlib.sha256((base/('model'+s)).read_bytes()).hexdigest()) for s in ('.inp','.rpt','.out')},retry=retry))

    def test_custom_backend_rejects_non_dynamic_routing_before_solving(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(os.environ.get('EASYSEWER_DIVIDER_GATE_OUTPUT',temporary))/'rejected-custom'
            for kind in KINDS:
                for routing in ('STEADY','KINWAVE'):
                    model=created(kind,'full',routing=routing)
                    destination=root/kind/routing
                    result=Runner().run(model,RunConfig(backend=FlexiblePondingBackend.key,
                        output_directory=FileReference(path=str(destination),direction='output')))
                    self.assertEqual(result.status,'rejected',(result.failure,result.diagnostics))
                    self.assertFalse(result.native_completed)
                    self.assertIn('flexible.routing',{d.code for d in result.diagnostics.diagnostics})
                    self.assertFalse((destination/'model.out').exists())
                    EVIDENCE.append(dict(kind='custom-routing-rejected',law=kind,routing=routing,native_completed=False))

if __name__=='__main__':unittest.main()
