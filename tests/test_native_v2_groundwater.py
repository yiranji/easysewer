"""Independent literal groundwater inputs and full native hydraulic/quality data."""

from dataclasses import replace
from datetime import date, time, timedelta
from itertools import product
from pathlib import Path
import tempfile
import unittest

from easysewer import get_native_capabilities
from easysewer.model import Model
from easysewer.model import groundwater as g, expressions as e
from easysewer.io.inp import InpDocument
from easysewer.io.inp.groundwater import GroundwaterExpressionCodec
from easysewer.io.hotstart import HotstartData, HotstartLayout
from easysewer.io.hotstart_manifest import HotstartManifest
from easysewer.io.runoff_cache import RunoffData, RunoffLayout
from easysewer.io.cache_manifest import CacheManifest
from easysewer.runtime import check_files
from test_files_v2 import bind
from test_quality_v2 import quality_model, ref
from test_groundwater_v2 import groundwater_model
from test_native_v2_files import selected
from test_scenario_v2 import portable
import test_native_v2_project as native_project
import test_native_v2_hotstart as native_hotstart


def literal_groundwater(*,units='CFS',tail='*',expressions=True):
    model=selected(quality_model());model.reinterpret_units(units)
    source=model.to_document().text+('[PATTERNS]\nET MONTHLY .5 1.5\n[AQUIFERS]\n'
        'Aquifer .45 .1 .25 .2 10 15 .5 5 .001 -10 2 .3 ET\n'
        '[GROUNDWATER]\nS Aquifer J 20 .001 1.2 .0001 1.1 .00001 0 '+tail+'\n')
    if expressions:
        source+='[GWF]\nS LATERAL 0.002 * (HGW - HCB) + 0.0001 * HSW\nS DEEP 0.001 * (HGW / HGS)\n'
    return source


@unittest.skipUnless(get_native_capabilities()['swmm_solver'] and get_native_capabilities()['swmm_output'],'Native solver/output unavailable')
class NativeGroundwaterTests(unittest.TestCase):
    def solve(self,root,name,source,*,allow_error=False):
        result=native_project.NativeProjectTests.solve(self,root,name,source,report_encoding='cp1252',allow_error=allow_error)
        if 'error' not in result:
            result['quality']=native_hotstart.NativeHotstartTests.quality(self,root,name)
        return result

    def test_all_sixteen_local_overrides_in_both_unit_systems(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for units in ('CFS','CMS'):
                for flags in product((False,True),repeat=4):
                    tail=' '.join(value if flag else '*' for value,flag in zip(('-1','-12','3','.35'),flags))
                    with self.subTest(units=units,tail=tail):
                        source=literal_groundwater(units=units,tail=tail)
                        model=Model.from_document(InpDocument.from_text(source),strict=True)
                        expected=self.solve(root,'original',source)
                        actual=self.solve(root,'rebuilt',portable(model).to_document().text)
                        self.assertEqual(expected,actual)
                        self.assertGreater(max(actual['series'][0][0][5]),0)
                        self.assertGreater(max(actual['series'][0][0][7]),.1)
                        self.assertTrue(all(max(series)>0 for series in actual['quality'][1][0]))

    def test_parameters_and_both_flow_expressions_change_real_results(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=literal_groundwater()
            original=self.solve(root,'original',source)
            cases=(('aquifers','Aquifer','conductivity',.4,'.25 .2 10','.25 .4 10'),
                ('aquifers','Aquifer','tension_slope',30,'10 15 .5','10 30 .5'),
                ('groundwater','S','groundwater_coefficient',.01,'20 .001 1.2','20 .01 1.2'),
                ('groundwater','S','fixed_surface_depth',3,'.00001 0 *','.00001 3 *'))
            for namespace,key,field,value,old,new in cases:
                with self.subTest(field=field):
                    model=Model.from_document(InpDocument.from_text(source),strict=True)
                    model.collection('swmm:'+namespace).update(key,**{field:value})
                    actual=self.solve(root,'edited',portable(model).to_document().text)
                    self.assertEqual(actual,self.solve(root,'oracle',source.replace(old,new)))
                    self.assertTrue(actual['series']!=original['series'],field)
            for kind,old,new in (('LATERAL','0.002 * (HGW - HCB) + 0.0001 * HSW','0.02 * (HGW - HCB)'),
                    ('DEEP','0.001 * (HGW / HGS)','0.1 * (HGW / HGS)')):
                model=Model.from_document(InpDocument.from_text(source),strict=True)
                model.gwf.update(('S',kind),expression=GroundwaterExpressionCodec().parse(new))
                actual=self.solve(root,'expression',portable(model).to_document().text)
                self.assertEqual(actual,self.solve(root,'expression-oracle',source.replace(old,new)))
                self.assertTrue(actual['series']!=original['series'],kind)

    def test_every_variable_math_function_and_native_prefix(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);base=literal_groundwater(expressions=False)
            formulas=[f'.000001 * {name}' for name in g.VARIABLE_DIMENSIONS]
            formulas += [f'.000001 * {name}(.5)' for name in e.FUNCTIONS]
            formulas += ['.000001 * KSuffix','.000001 * HgwExtra','-2^2 * .000001','2^-2 * .000001','+(HGW - HCB) * .000001']
            for expression in formulas:
                with self.subTest(expression=expression):
                    source=base+'[GWF]\nS LAT '+expression+'\n'
                    parsed=Model.from_document(InpDocument.from_text(source),strict=True)
                    self.assertEqual(self.solve(root,'literal',source),self.solve(root,'normalized',portable(parsed).to_document().text))

    def test_unit_conversion_has_independent_coefficients_and_expression_oracle(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);length=.3048;flux=3048/43560
            for target in ('GPM','MGD','CMS','LPS','MLD'):
                model=selected(groundwater_model());model.convert_units(target)
                base=selected(quality_model());base.convert_units(target)
                if target in ('GPM','MGD'):
                    oracle=literal_groundwater().replace('FLOW_UNITS CFS','FLOW_UNITS '+target)
                    # Convert the independently established hydrology/quality
                    # base as well; only GW coefficients are held unchanged.
                    oracle=base.to_document().text+oracle[oracle.index('[PATTERNS]'):]
                else:
                    oracle=base.to_document().text+('[PATTERNS]\nET MONTHLY .5 1.5\n[AQUIFERS]\n'
                        f'Aquifer .45 .1 .25 {.2*25.4:.17g} 10 {15*length:.17g} .5 {5*length:.17g} {.001*25.4:.17g} {-10*length:.17g} {2*length:.17g} .3 ET\n'
                        '[GROUNDWATER]\n'
                        f'S Aquifer J {20*length:.17g} {.001*flux/length**1.2:.17g} 1.2 {.0001*flux/length**1.1:.17g} 1.1 {.00001*flux/length**2:.17g} 0 *\n'
                        '[GWF]\n'
                        f'S LATERAL {flux:.17g} * (0.002 * ((HGW / {length:.17g}) - (HCB / {length:.17g})) + 0.0001 * (HSW / {length:.17g}))\n'
                        f'S DEEP 25.4 * (0.001 * ((HGW / {length:.17g}) / (HGS / {length:.17g})))\n')
                with self.subTest(target=target):
                    self.assertEqual(self.solve(root,'independent',oracle),self.solve(root,'converted',portable(model).to_document().text))

    def test_monthly_evaporation_pattern_changes_across_month_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=literal_groundwater()+'[EVAPORATION]\nCONSTANT .2\n'
            model=Model.from_document(InpDocument.from_text(source),strict=True)
            model.update_options(end_date=date(2020,2,2),end_time=time(),routing_step=timedelta(seconds=60))
            source=model.to_document().text
            original=self.solve(root,'original',source)
            model.patterns.update('ET',factors=(1.5,.5))
            actual=self.solve(root,'edited',model.to_document().text)
            self.assertEqual(actual,self.solve(root,'oracle',source.replace('ET MONTHLY .5 1.5','ET MONTHLY 1.5 .5')))
            self.assertTrue(actual['series']!=original['series'])

    def test_lateral_is_additive_and_deep_replaces_default_seepage(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=literal_groundwater()
            original=self.solve(root,'normal',source)
            model=Model.from_document(InpDocument.from_text(source),strict=True)
            model.aquifers.update('Aquifer',deep_seepage=10)
            unchanged=self.solve(root,'deep-replaced',model.to_document().text)
            self.assertEqual(original,unchanged)
            model.gwf.remove(('S','DEEP'))
            changed=self.solve(root,'default-deep',model.to_document().text)
            self.assertTrue(changed['series']!=original['series'])
            model=Model.from_document(InpDocument.from_text(source),strict=True)
            model.gwf.update(('S','LATERAL'),expression=e.ExpressionNumber(value=0))
            baseflow=self.solve(root,'zero-extra',model.to_document().text)
            self.assertGreater(max(baseflow['series'][0][0][5]),0)
            self.assertTrue(baseflow['series']!=original['series'])

    def test_native_requires_threshold_slot_and_monthly_pattern(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);base=literal_groundwater()
            for source in (base.replace('.00001 0 *','.00001 0'),base.replace('ET MONTHLY','ET DAILY')):
                result=self.solve(root,'invalid',source,allow_error=True)
                self.assertIn('error',result)
                parsed=Model.from_document(InpDocument.from_text(source))
                self.assertFalse(parsed.validate().is_valid)

    def test_tension_slope_uses_native_length_units_despite_manual_label(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);model=selected(groundwater_model())
            us=self.solve(root,'us',model.to_document().text)
            model.convert_units('CMS')
            si=self.solve(root,'si',model.to_document().text)
            # Full moisture history agrees; interpreting the manual's in/mm
            # label as a 25.4 multiplier measurably changes the physical model.
            for a,b in zip(us['series'][0][0][7],si['series'][0][0][7]):self.assertAlmostEqual(a,b,delta=1e-7)
            model.aquifers.update('Aquifer',tension_slope=15*25.4)
            manual=self.solve(root,'manual',model.to_document().text)
            self.assertGreater(max(abs(a-b) for a,b in zip(us['series'][0][0][7],manual['series'][0][0][7])),.001)

    def test_numeric_missing_sentinels_roundtrip_as_explicit_absence(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for units in ('CFS','CMS'):
                for index in range(4):
                    tail=['*']*4
                    tail[index]='-1e10' if units=='CFS' or index==3 else '-3048000000'
                    source=literal_groundwater(units=units,tail=' '.join(tail))
                    model=Model.from_document(InpDocument.from_text(source),strict=True)
                    self.assertTrue(all(getattr(model.groundwater['S'],name) is None for name in ('threshold_elevation','bottom_elevation','water_table_elevation','upper_moisture')))
                    self.assertIn('groundwater.native_missing',{d.code for d in model.validate().diagnostics})
                    self.assertEqual(self.solve(root,'numeric',source),self.solve(root,'absent',portable(model).to_document().text))

    def test_reverse_exchange_interaction_clamp_and_groundwater_pollution(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            source=literal_groundwater(expressions=False).replace('.001 1.2 .0001 1.1 .00001 0','.001 1.2 .02 1.1 0 5')+'[INFLOWS]\nJ FLOW "" FLOW 1 1 .2\n'
            model=Model.from_document(InpDocument.from_text(source),strict=True)
            reverse=self.solve(root,'reverse',source)
            self.assertLess(min(reverse['series'][0][0][5]),0)
            model.groundwater.update('S',interaction_coefficient=.0000001)
            clipped=self.solve(root,'clipped',portable(model).to_document().text)
            self.assertEqual(clipped,self.solve(root,'clipped-oracle',source.replace('.02 1.1 0 5','.02 1.1 .0000001 5')))
            self.assertEqual(min(clipped['series'][0][0][5]),0)
            source=literal_groundwater()
            model=Model.from_document(InpDocument.from_text(source),strict=True)
            original=self.solve(root,'quality',source)
            model.pollutants.update('Q0',groundwater_concentration=30)
            actual=self.solve(root,'quality-edited',portable(model).to_document().text)
            self.assertEqual(actual,self.solve(root,'quality-oracle',source.replace('Q0 MG/L 2 3 4','Q0 MG/L 2 30 4')))
            self.assertTrue(actual['quality']!=original['quality'])

    def test_groundwater_state_cache_identity_and_reuse_with_quality(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=literal_groundwater();model=Model.from_document(InpDocument.from_text(source),strict=True)
            for kind,Data,Layout,Manifest in (('HOTSTART',HotstartData,HotstartLayout,HotstartManifest),('RUNOFF',RunoffData,RunoffLayout,CacheManifest)):
                cache=root/(kind+'.bin');copy=root/(kind+'-copy.bin')
                self.solve(root,'producer',source+f'[FILES]\nSAVE {kind} "{cache}"\n')
                raw=cache.read_bytes();layout=Layout.from_model(model);data=Data.from_bytes(raw,layout=layout)
                self.assertEqual(raw,data.to_bytes());data.write(copy)
                if kind=='HOTSTART':
                    self.assertEqual(layout.subcatchments[0].groundwater,'Aquifer')
                    self.assertGreater(data.subcatchments[0].groundwater.flow,0)
                else:self.assertGreater(max(f.samples[0].groundwater_flow for f in data.frames),0)
                expected=self.solve(root,'original-cache',source+f'[FILES]\nUSE {kind} "{cache}"\n')
                actual=self.solve(root,'rewritten-cache',source+f'[FILES]\nUSE {kind} "{copy}"\n')
                if kind=='RUNOFF':actual['report']=actual['report'].replace(str(copy),str(cache))
                self.assertEqual(expected,actual)
                consumer=model.copy();bind(consumer,kind,'USE',copy)
                self.assertTrue(check_files(consumer,interface_manifests={kind:Manifest.asserted(raw,layout=layout)}).complete)


if __name__=='__main__':
    unittest.main()
