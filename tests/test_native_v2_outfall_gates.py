"""Seven outfall variant groups compared with literal native OUT/RPT oracles."""
from pathlib import Path
import hashlib,os,tempfile,unittest
from datetime import timedelta

from easysewer import get_native_capabilities
from easysewer.io.output import OutputReader
from easysewer.model import Ref,FileReference
from easysewer.model.resources import CurvePoint,SeriesPoint,FileTimeSeries
from easysewer.runtime._solver_worker import _configure_error_mode
from test_outfall_gates_v2 import KINDS,UNITS,source,created,load,portable,row
from test_native_v2_regulator_fields import FAMILIES,library
from test_native_v2_title_report_gates import solve

EVIDENCE=[]
TAILS={'omitted':(None,False),'open':(False,False),'gate':(True,False),
       'route-open':(False,True),'route-gate':(True,True)}


def observed(test,lib,folder,text):
    folder.mkdir(parents=True)
    result=solve(test,lib,folder,text)
    with OutputReader(folder/'model.out') as reader:
        flow=reader.series(Ref(collection='swmm:links',key='P'),'swmm:flow').values
        runoff=reader.series(Ref(collection='swmm:subcatchments',key='S'),'swmm:runoff').values
    metadata=dict(flow_min=min(flow),flow_max=max(flow),flow_sum=sum(flow),runoff_max=max(runoff),
        out_sha256=hashlib.sha256(result['out']).hexdigest(),
        normalized_report_sha256=hashlib.sha256(result['report']).hexdigest(),
        files={suffix:dict(path=str(folder/('model'+suffix)),sha256=hashlib.sha256((folder/('model'+suffix)).read_bytes()).hexdigest())
            for suffix in ('.inp','.rpt','.out')})
    return result,metadata


@unittest.skipUnless(get_native_capabilities()['swmm_solver'] and get_native_capabilities()['flexible_ponding'],
                     'Both packaged native engines required')
class NativeOutfallGateTests(unittest.TestCase):
    def test_all_boundaries_gates_routes_units_and_created_models_match_full_native_results(self):
        _configure_error_mode()
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(os.environ.get('EASYSEWER_OUTFALL_GATE_OUTPUT',temporary))/'full'
            for family,name,symbol in FAMILIES:
                lib,path=library(name,symbol)
                for units in UNITS:
                    for kind in KINDS:
                        for direction in ('reverse','forward'):
                            results={};rows=[]
                            for case,(gated,route) in TAILS.items():
                                with self.subTest(family=family,units=units,boundary=kind,direction=direction,case=case):
                                    base=root/family/units/kind/direction/case
                                    text=source(kind,gated,route,units,direction)
                                    expected,entry=observed(self,lib,base/'literal',text)
                                    model=load(text);fresh=created(kind,gated,route,units,direction)
                                    forms={'created':fresh.to_document().text,
                                        'normalized':model.to_document(normalize=True).text,
                                        'json':portable(model).to_document(normalize=True).text}
                                    variants={}
                                    for form,inp in forms.items():
                                        actual,metadata=observed(self,lib,base/form,inp)
                                        self.assertEqual(actual,expected,(family,units,kind,direction,case,form))
                                        variants[form]=metadata
                                    results[case]=(expected,entry)
                                    rows.append(dict(case=case,literal=entry,variants=variants))
                            self.assertEqual(results['omitted'][0],results['open'][0])
                            if direction=='reverse' and kind in ('FIXED','TIDAL','TIMESERIES'):
                                self.assertLess(results['route-open'][1]['flow_min'],0)
                                self.assertEqual(results['route-gate'][1]['flow_min'],0)
                                self.assertEqual(results['route-gate'][1]['flow_max'],0)
                                self.assertNotEqual(results['route-open'][0]['out'],results['route-gate'][0]['out'])
                            if direction=='forward':
                                self.assertGreater(results['route-gate'][1]['runoff_max'],0)
                                self.assertEqual(results['omitted'][1]['runoff_max'],0)
                                self.assertEqual(results['route-gate'][0],results['route-open'][0])
                            EVIDENCE.append(dict(kind='native-outfall-matrix',family=family,units=units,boundary=kind,
                                direction=direction,library_sha256=hashlib.sha256(Path(path).read_bytes()).hexdigest(),rows=rows))

    def test_varying_tidal_and_series_stages_and_external_files_preserve_full_results(self):
        _configure_error_mode()
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(os.environ.get('EASYSEWER_OUTFALL_GATE_OUTPUT',temporary))/'varying'
            for family,name,symbol in FAMILIES:
                lib,_=library(name,symbol)
                for units in UNITS:
                    for kind in ('TIDAL','TIMESERIES'):
                        for gated in (False,True):
                            for route in (False,True):
                                with self.subTest(family=family,units=units,kind=kind,gated=gated,route=route):
                                    base=root/family/units/kind/str(gated)/str(route)
                                    text=source(kind,gated,route,units)
                                    fresh=created(kind,gated,route,units)
                                    if kind=='TIDAL':
                                        text=text.replace('Tide TIDAL 0 14\nTide 24 14', 'Tide TIDAL 0 14\nTide .1 8\nTide 24 8')
                                        fresh.curves.update('Tide',points=(CurvePoint(x=0,y=14),CurvePoint(x=.1,y=8),CurvePoint(x=24,y=8)))
                                    else:
                                        text=text.replace('Stage 0:00 14\nStage 24:00 14','Stage 0:00 14\nStage 0:06 8\nStage 24:00 8')
                                        fresh.timeseries.update('Stage',points=tuple(SeriesPoint(time=t,value=v) for t,v in
                                            ((timedelta(0),14),(timedelta(minutes=6),8),(timedelta(hours=24),8))))
                                    expected,metadata=observed(self,lib,base/'literal',text)
                                    if gated:self.assertEqual(metadata['flow_min'],0)
                                    else:
                                        self.assertLess(metadata['flow_min'],0)
                                        self.assertGreater(metadata['flow_max'],0)
                                    model=load(text);forms={'created':fresh.to_document().text,
                                        'normalized':model.to_document(normalize=True).text,'json':portable(model).to_document(normalize=True).text}
                                    file_evidence=None
                                    if kind=='TIMESERIES':
                                        file=(base/'水位 data.dat').resolve()
                                        file.write_bytes(b'01/01/2004 0:00 14\n0:06 8\n24:00 8\n')
                                        file_evidence=dict(path=str(file),sha256=hashlib.sha256(file.read_bytes()).hexdigest())
                                        external_text=text.replace('Stage 0:00 14\nStage 0:06 8\nStage 24:00 8',f'Stage FILE "{file}"')
                                        file_model=load(external_text,source_path=str((base/'external'/'model.inp').resolve()))
                                        fresh.timeseries.replace('Stage',FileTimeSeries(id='Stage',file=FileReference(path=str(file),direction='input')))
                                        forms.update(external=external_text,external_json=portable(file_model).to_document(normalize=True).text,
                                            external_created=fresh.to_document().text)
                                    retained={}
                                    for form,inp in forms.items():
                                        actual,entry=observed(self,lib,base/form,inp)
                                        self.assertEqual(actual,expected,form);retained[form]=entry
                                    if file_evidence:self.assertEqual(hashlib.sha256(file.read_bytes()).hexdigest(),file_evidence['sha256'])
                                    EVIDENCE.append(dict(kind='varying-boundary',family=family,units=units,boundary=kind,gated=gated,
                                        route=route,literal=metadata,variants=retained,external_file=file_evidence))

    def test_bad_gate_with_and_without_route_is_rejected_and_healthy_input_reopens(self):
        _configure_error_mode()
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(os.environ.get('EASYSEWER_OUTFALL_GATE_OUTPUT',temporary))/'invalid'
            for family,name,symbol in FAMILIES:
                lib,_=library(name,symbol)
                for units in UNITS:
                    for kind in KINDS:
                        for route in (False,True):
                            with self.subTest(family=family,units=units,boundary=kind,route=route):
                                base=root/family/units/kind/str(route);base.mkdir(parents=True)
                                text=source(kind,False,route,units)
                                bad=text.replace(row(kind,False,route),row(kind,False,route).replace(' NO',' MAYBE'))
                                model=load(bad,strict=False);self.assertFalse(model.validate().is_valid)
                                inp,rpt,out=(base/('model'+suffix) for suffix in ('.inp','.rpt','.out'))
                                inp.write_text(bad,encoding='utf-8');out.write_bytes(b'previous-output')
                                try:code=lib.swmm_open(os.fsencode(inp),os.fsencode(rpt),os.fsencode(out))
                                finally:self.assertEqual(lib.swmm_close(),0)
                                self.assertEqual(code,200)
                                self.assertEqual(out.read_bytes(),b'previous-output')
                                self.assertIn(b'ERROR 205',rpt.read_bytes())
                                bad_record=dict(code=code,report_sha256=hashlib.sha256(rpt.read_bytes()).hexdigest())
                                _,retry=observed(self,lib,base/'retry',text)
                                EVIDENCE.append(dict(kind='invalid-gate-retry',family=family,units=units,boundary=kind,
                                    route=route,invalid=bad_record,retry=retry,previous_output_preserved=True))


if __name__=='__main__':unittest.main()
