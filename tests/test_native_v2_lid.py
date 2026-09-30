"""Independent literal LID inputs and full native hydraulics/quality histories."""

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from easysewer import get_native_capabilities
from easysewer.io.inp import InpDocument
from easysewer.model import Model
from easysewer.model.lid import LID_KINDS
from test_quality_v2 import ref
from test_scenario_v2 import portable
import test_native_v2_quality as quality
import test_native_v2_project as project
import test_native_v2_hotstart as hotstart


LITERAL_LAYERS = {
    'SURFACE':'3 .1 .1 2 2', 'SOIL':'12 .5 .2 .1 1 5 3', 'PAVEMENT':'4 .25 .1 5 10',
    'STORAGE':'12 .5 .1 10', 'DRAIN':'.2 .5 1 2', 'DRAINMAT':'2 .6 .1',
}
TYPE_LAYERS = {'BC':('SURFACE','SOIL','STORAGE','DRAIN'), 'RG':('SURFACE','SOIL'),
    'GR':('SURFACE','SOIL','DRAINMAT'), 'IT':('SURFACE','STORAGE','DRAIN'),
    'PP':('SURFACE','PAVEMENT','SOIL','STORAGE','DRAIN'), 'RB':('STORAGE','DRAIN'),
    'RD':('SURFACE','DRAIN'), 'VS':('SURFACE',)}


def literal_lid(kind, *, advanced=False, detail=None, replicate=2, saturation=50, to_pervious=0, drain='*'):
    text='[LID_CONTROLS]\nL '+kind+'\n'
    for layer in TYPE_LAYERS[kind]:
        text+='L '+layer+' '+LITERAL_LAYERS[layer]
        if advanced:
            text+= {'PAVEMENT':' 2 .5','STORAGE':' YES','DRAIN':' 2 .5 Head'}.get(layer,'')
        text+='\n'
    text+='L REMOVALS Q0 25 Q1 50\n'
    if advanced and 'DRAIN' in TYPE_LAYERS[kind]:
        text+='[CURVES]\nHead CONTROL 0 .5\nHead 36 1\n'
    text+=f'[LID_USAGE]\nS L {replicate} 100 10 {saturation} 20 {to_pervious} '
    text+=f'"{detail}"' if detail else '*'
    text+=f' {drain} 10\n'
    return text


def literal_source(kind, units='CFS', **kwargs):
    return quality.literal_quality('NONE','EMC',units=units)+literal_lid(kind,**kwargs)


def si_lid(text):
    """Independent positional conversion of the literal LID fixture only."""
    output=[]; section=None; kind=None
    for line in text.splitlines():
        if line.startswith('['):
            section=line
        v=line.split()
        if section=='[LID_CONTROLS]' and v and v[0]=='L':
            if v[1] in LID_KINDS:
                kind=v[1]
            else:
                indices={'SURFACE':(2,), 'SOIL':(2,6,8), 'PAVEMENT':(2,5), 'STORAGE':(2,4), 'DRAIN':(4,6,7), 'DRAINMAT':(2,)}.get(v[1],())
                for i in indices:
                    if i < len(v): v[i]=repr(float(v[i])*25.4)
                if v[1]=='DRAIN': v[2]=repr(float(v[2])*25.4**(1 if kind=='RD' else 1-float(v[3])))
        if section=='[CURVES]' and v and v[0]=='Head':
            i=2 if v[1]=='CONTROL' else 1
            v[i]=repr(float(v[i])*25.4)
        if section=='[LID_USAGE]' and v and v[0]=='S':
            v[3]=repr(float(v[3])*.3048**2); v[4]=repr(float(v[4])*.3048)
        output.append(' '.join(v))
    return '\n'.join(output)+'\n'


@unittest.skipUnless(get_native_capabilities()['swmm_solver'] and get_native_capabilities()['swmm_output'],'Native solver/output unavailable')
class NativeLidTests(unittest.TestCase):
    def solve(self,root,name,source):
        result=project.NativeProjectTests.solve(self,root,name,source,report_encoding='cp1252')
        result['quality']=hotstart.NativeHotstartTests.quality(self,root,name,subcatchments=len(result['ids'][0]))
        return result

    def test_all_types_optional_layers_and_both_unit_systems(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for units in ('CFS','CMS'):
                for kind in LID_KINDS:
                    for advanced in (False,True):
                        with self.subTest(units=units,kind=kind,advanced=advanced):
                            detail=root/'detail.txt'
                            source=literal_source(kind,units,advanced=advanced,detail=detail)
                            model=Model.from_document(InpDocument.from_text(source),strict=True)
                            self.assertTrue(model.validate(for_run=True).is_valid,model.validate().errors)
                            expected=self.solve(root,'literal',source); report=detail.read_bytes()
                            actual=self.solve(root,'rebuilt',portable(model).to_document().text)
                            self.assertEqual(expected,actual)
                            self.assertEqual(report,detail.read_bytes())
                            self.assertIn(b'Storage',report)
                            self.assertIn('LID Performance Summary',actual['report'])

    def test_modified_parameters_repeated_deployments_shared_definitions(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for kind in LID_KINDS:
                source=literal_source(kind,advanced=True)+'[LID_USAGE]\nS L 3 200 20 20 30 1 * O 20\n'
                model=Model.from_document(InpDocument.from_text(source),strict=True)
                self.assertEqual(len(model.lid_usage),2)
                before=self.solve(root,'before',source)
                model.lid_usage.update('lid-usage-1',area=180,initial_saturation=90)
                if model.lid_controls['L'].surface:
                    model.lid_controls.update('L',surface=replace(model.lid_controls['L'].surface,storage_depth=6))
                oracle=source.replace('2 100 10 50','2 180 10 90').replace('L SURFACE 3 .1','L SURFACE 6 .1')
                actual=self.solve(root,'edited',portable(model).to_document().text)
                self.assertEqual(actual,self.solve(root,'oracle',oracle))
                self.assertTrue(before['series'] != actual['series'])
                model.lid_usage.move('lid-usage-2',before='lid-usage-1')
                reordered=self.solve(root,'reordered',model.to_document().text)
                literal=oracle.replace('S L 2 180 10 90 20 0 * * 10\n','').replace('S L 3 200 20 20 30 1 * O 20\n','S L 3 200 20 20 30 1 * O 20\nS L 2 180 10 90 20 0 * * 10\n')
                self.assertEqual(reordered,self.solve(root,'reordered-oracle',literal))

    def test_optional_pavement_soil_storage_and_additional_layers(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for soil in (False,True):
                for storage in (False,True):
                    source=literal_source('PP',advanced=True)
                    if not soil: source=source.replace('L SOIL 12 .5 .2 .1 1 5 3\n','')
                    if not storage: source=source.replace('L STORAGE 12 .5 .1 10 YES\n','')
                    model=Model.from_document(InpDocument.from_text(source),strict=True)
                    self.assertEqual(self.solve(root,'literal',source),self.solve(root,'model',portable(model).to_document().text))
            for kind in ('BC','RG'):
                source=literal_source(kind)
                if kind=='BC': source=source.replace('L STORAGE 12 .5 .1 10\n','')
                else: source+='[LID_CONTROLS]\nL STORAGE 12 .5 .1 10\nL DRAIN .2 .5 1 2\n'
                model=Model.from_document(InpDocument.from_text(source),strict=True)
                self.assertEqual(self.solve(root,'literal',source),self.solve(root,'model',portable(model).to_document().text))

    def test_subcatchment_drain_precedence_and_return_routing(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            base=quality.base_model()
            base.subcatchments.add(replace(base.subcatchments['S'],id='O',area=.5))
            source=base.to_document().text+'[POLLUTANTS]'+quality.literal_quality('NONE','EMC').split('[POLLUTANTS]',1)[1]
            # Both a native outfall and a subcatchment are named O.
            for returned in (0,1):
                literal=source+literal_lid('IT',advanced=True,drain='O',to_pervious=returned)
                model=Model.from_document(InpDocument.from_text(literal),strict=True)
                self.assertEqual(model.lid_usage['lid-usage-1'].drain_to.collection,'swmm:subcatchments')
                self.assertEqual(self.solve(root,'literal',literal),self.solve(root,'model',portable(model).to_document().text))
                model.subcatchments.rename('O','Receiver')
                baseline=Model.from_document(InpDocument.from_text(source),strict=True)
                baseline.subcatchments.rename('O','Receiver')
                renamed=baseline.to_document().text+literal_lid('IT',advanced=True,drain='Receiver',to_pervious=returned)
                self.assertEqual(self.solve(root,'renamed',portable(model).to_document().text),self.solve(root,'oracle',renamed))

    def test_all_flow_unit_conversions_against_independent_lid_numbers(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for kind in LID_KINDS:
                for target in ('GPM','MGD','CMS','LPS','MLD'):
                    with self.subTest(kind=kind,target=target):
                        source=literal_source(kind,advanced=True)
                        model=Model.from_document(InpDocument.from_text(source),strict=True)
                        model.convert_units(target)
                        base=Model.from_document(InpDocument.from_text(quality.literal_quality('NONE','EMC')),strict=True)
                        base.convert_units(target)
                        suffix=literal_lid(kind,advanced=True)
                        if target in ('CMS','LPS','MLD'): suffix=si_lid(suffix)
                        self.assertEqual(self.solve(root,'converted',portable(model).to_document().text),self.solve(root,'literal',base.to_document().text+suffix))

    def test_barrel_cover_default_zero_void_ratio_and_disabled_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            source=literal_source('RB')
            absent=self.solve(root,'omitted',source)
            no=self.solve(root,'no',source.replace('L STORAGE 12 .5 .1 10','L STORAGE 12 .5 .1 10 NO'))
            yes=self.solve(root,'yes',source.replace('L STORAGE 12 .5 .1 10','L STORAGE 12 .5 .1 10 YES'))
            self.assertEqual(absent,no); self.assertTrue(no['series'] != yes['series'])
            bad=project.NativeProjectTests.solve(self,root,'zero-void',source.replace('L STORAGE 12 .5','L STORAGE 12 0'),allow_error=True)
            self.assertIn('error',bad)
            disabled=source+'[LID_USAGE]\nS L 0 bad bad bad bad bad "missing-parent/ignored.txt" MissingNode\n'
            model=Model.from_document(InpDocument.from_text(disabled),strict=True)
            self.assertEqual(absent,self.solve(root,'disabled',disabled))
            self.assertEqual(absent,self.solve(root,'disabled-normalized',portable(model).to_document().text))

    def test_initial_state_draining_thresholds_curve_and_return_routing(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            variants=(('initial_saturation',100),('to_pervious',True),('drain_to',ref('nodes','O')),('from_pervious',50))
            source=literal_source('IT',advanced=True)
            baseline=self.solve(root,'base',source)
            for name,value in variants:
                model=Model.from_document(InpDocument.from_text(source),strict=True)
                model.lid_usage.update('lid-usage-1',**{name:value})
                old='S L 2 100 10 50 20 0 * * 10'
                new={'initial_saturation':'S L 2 100 10 100 20 0 * * 10','to_pervious':'S L 2 100 10 50 20 1 * * 10',
                    'drain_to':'S L 2 100 10 50 20 0 * O 10','from_pervious':'S L 2 100 10 50 20 0 * * 50'}[name]
                actual=self.solve(root,name,portable(model).to_document().text)
                self.assertEqual(actual,self.solve(root,'oracle',source.replace(old,new)))
                self.assertTrue(actual['series'] != baseline['series'])
            for old,new in (('.2 .5 1 2 2 .5 Head','.2 .5 1 2 10 8 Head'),('Head 36 1','Head 36 .1'),('L DRAIN .2','L DRAIN 2')):
                changed=source.replace(old,new)
                model=Model.from_document(InpDocument.from_text(changed),strict=True)
                actual=self.solve(root,'changed',portable(model).to_document().text)
                self.assertEqual(actual,self.solve(root,'oracle',changed))
                self.assertTrue(actual['series'] != baseline['series'])

    def test_removals_and_pollutant_units_use_public_protocol(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); source=literal_source('IT',advanced=True)
            expected=self.solve(root,'removals',source)
            none=self.solve(root,'none',source.replace('Q0 25 Q1 50','Q0 0 Q1 0'))
            self.assertEqual(expected['series'],none['series'])
            self.assertTrue(expected['quality'] != none['quality'])
            model=Model.from_document(InpDocument.from_text(source),strict=True)
            base=Model.from_document(InpDocument.from_text(quality.literal_quality('NONE','EMC')),strict=True)
            for id,units in (('Q0','UG/L'),('Q1','MG/L')):
                model.convert_pollutant_units(id,units); base.convert_pollutant_units(id,units)
            self.assertEqual(self.solve(root,'units',portable(model).to_document().text),self.solve(root,'oracle',base.to_document().text+literal_lid('IT',advanced=True)))

    def test_pavement_regeneration_and_barrel_dry_delay(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for kind,old,new in (('PP','4 .25 .1 5 10 2 .5','4 .25 .1 5 .2 .02 1'),('RB','L DRAIN .2 .5 1 2','L DRAIN .2 .5 1 0')):
                source=literal_source(kind,advanced=True)
                before=self.solve(root,'before',source)
                modified=source.replace(old,new)
                model=Model.from_document(InpDocument.from_text(modified),strict=True)
                actual=self.solve(root,'after',portable(model).to_document().text)
                self.assertEqual(actual,self.solve(root,'literal',modified))
                self.assertTrue(actual['series'] != before['series'])

    def test_hotstart_and_runoff_save_rewrite_consume_with_lid(self):
        from easysewer.io.hotstart import HotstartData, HotstartLayout
        from easysewer.io.runoff_cache import RunoffData, RunoffLayout
        from test_files_v2 import bind
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for kind in ('HOTSTART','RUNOFF'):
                model=Model.from_document(InpDocument.from_text(literal_source('PP',advanced=True)),strict=True)
                cache=root/(kind+'.bin'); bind(model,kind,'SAVE',cache)
                saved=self.solve(root,'save',model.to_document().text)
                raw=cache.read_bytes()
                data=HotstartData.from_bytes(raw,layout=HotstartLayout.from_model(model)) if kind=='HOTSTART' else RunoffData.from_bytes(raw,layout=RunoffLayout.from_model(model))
                self.assertEqual(raw,data.to_bytes())
                replacement=root/(kind+'-copy.bin'); replacement.write_bytes(data.to_bytes())
                # Native hotstart persists catchment states but no LID layer
                # state. This test proves format/consumption, not continuation.
                model.files.remove((kind,'SAVE')); bind(model,kind,'USE',cache)
                original=self.solve(root,'consume-original',model.to_document().text)
                model.files.remove((kind,'USE')); bind(model,kind,'USE',replacement)
                rewritten=self.solve(root,'consume-rewritten',portable(model).to_document().text)
                original['report']=original['report'].replace(str(cache),'CACHE')
                rewritten['report']=rewritten['report'].replace(str(replacement),'CACHE')
                self.assertEqual(original,rewritten)
                codes={d.code for d in model.validate(for_run=True).diagnostics}
                self.assertIn('lid.hotstart_state' if kind=='HOTSTART' else 'lid.runoff_drain_semantics',codes)

    def test_runoff_cache_loses_separate_drain_destination(self):
        from test_files_v2 import bind
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            source=literal_source('IT',advanced=True,drain='O',saturation=100).replace('L DRAIN .2','L DRAIN 2')
            model=Model.from_document(InpDocument.from_text(source),strict=True)
            cache=root/'runoff.bin'; bind(model,'RUNOFF','SAVE',cache)
            actual=self.solve(root,'generated',model.to_document().text)
            model.files.remove(('RUNOFF','SAVE')); bind(model,'RUNOFF','USE',cache)
            replay=self.solve(root,'replayed',model.to_document().text)
            self.assertGreater(abs(sum(actual['series'][1][0][4])-sum(replay['series'][1][0][4])),.01)
            self.assertTrue(actual['quality'] != replay['quality'])
            self.assertIn('lid.runoff_drain_semantics',{d.code for d in model.validate(for_run=True).errors})


if __name__ == '__main__':
    unittest.main()
