"""Independent literal quality inputs and real native concentration histories."""

from dataclasses import replace
from datetime import date, time, timedelta
from pathlib import Path
import tempfile
import unittest

from easysewer import get_native_capabilities
from easysewer.model import Model
from easysewer.model import quality as q
from easysewer.io.inp import InpDocument
from easysewer.io.hotstart import HotstartData, HotstartLayout
from easysewer.io.hotstart_manifest import HotstartManifest
from easysewer.io.runoff_cache import RunoffData, RunoffLayout
from easysewer.io.cache_manifest import CacheManifest
from easysewer.io.routing import RoutingInterface
from easysewer.runtime import check_files
from test_files_v2 import bind
from test_hydrology_v2 import hydrology_model
from test_native_v2_files import selected
import test_native_v2_project as native_project
import test_native_v2_hotstart as native_hotstart
from test_quality_v2 import quality_model, ref
from test_scenario_v2 import portable


BUILD_ROWS={'NONE':'NONE','POW':'POW 10 2 1.2','EXP':'EXP 10 .4 .7','SAT':'SAT 10 .8 3','EXT':'EXT 10 2 Accumulation'}
WASH_ROWS={'NONE':'NONE','EXP':'EXP 1.1 1.2 40 10','RC':'RC 2 1.2 40 10','EMC':'EMC 3 .7 40 10'}


def base_model(units='CFS'):
    model=selected(hydrology_model()); model.reinterpret_units(units)
    model.update_options(end_date=date(2020,1,30),end_time=time(8),routing_step=timedelta(seconds=60),dry_days=5)
    return model


def literal_quality(build='POW',wash='EXP',basis='AREA',units='CFS'):
    source=base_model(units).to_document().text+('[POLLUTANTS]\nQ0 MG/L 2 3 4 -.01 NO * 0 1 .5\nQ1 UG/L 2 3 4 -.01 NO * 0 1 .5\n'
        '[LANDUSES]\nLand 1 .5 .1\n[COVERAGES]\nS Land 100\n[BUILDUP]\n')
    for id in ('Q0','Q1'):
        source+=f'Land {id} {BUILD_ROWS[build]}'+(' '+basis if build!='NONE' else '')+'\n'
    source+='[WASHOFF]\n'+''.join(f'Land {id} {WASH_ROWS[wash]}\n' for id in ('Q0','Q1'))
    if build=='EXT':source+='[TIMESERIES]\nAccumulation 0 2\nAccumulation 8 3\n'
    return source


@unittest.skipUnless(get_native_capabilities()['swmm_solver'] and get_native_capabilities()['swmm_output'],'Native solver/output unavailable')
class NativeQualityTests(unittest.TestCase):
    def solve(self,root,name,source):
        result=native_project.NativeProjectTests.solve(self,root,name,source,report_encoding='cp1252')
        result['quality']=native_hotstart.NativeHotstartTests.quality(self,root,name)
        return result

    def test_all_buildup_washoff_variants_normalizers_and_unit_systems(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for units in ('CFS','CMS'):
                for build in BUILD_ROWS:
                    for wash in WASH_ROWS:
                        for basis in ('AREA','CURBLENGTH') if build!='NONE' else ('AREA',):
                            with self.subTest(units=units,build=build,wash=wash,basis=basis):
                                source=literal_quality(build,wash,basis,units)
                                model=Model.from_document(InpDocument.from_text(source),strict=True)
                                expected=self.solve(root,'literal',source)
                                actual=self.solve(root,'rebuilt',portable(model).to_document().text)
                                self.assertEqual(expected,actual)
                                self.assertEqual(expected['ids'][3],('Q0','Q1'))
                                self.assertGreater(max(expected['series'][0][0][4]),0)
                                self.assertTrue(all(max(values)>0 for values in actual['quality'][0][0]))

    def test_formula_and_loading_edits_change_real_pollutant_series(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for build in ('POW','EXP','SAT','EXT'):
                source=literal_quality(build,'EXP')
                model=Model.from_document(InpDocument.from_text(source),strict=True)
                original=self.solve(root,'original',source)
                f=model.buildup[('Land','Q0')].function
                model.buildup.update(('Land','Q0'),function=replace(f,maximum=.001))
                actual=self.solve(root,'edited',portable(model).to_document().text)
                literal=source.replace(f'Land Q0 {build} 10 ',f'Land Q0 {build} .001 ')
                self.assertEqual(actual,self.solve(root,'oracle',literal))
                self.assertTrue(original['quality']!=actual['quality'],build)
            for wash,old,new in (('EXP','1.1','2.2'),('RC','2','4'),('EMC','3','6')):
                source=literal_quality('NONE',wash)
                model=Model.from_document(InpDocument.from_text(source),strict=True)
                original=self.solve(root,'wash-original',source)
                f=model.washoff[('Land','Q0')].function
                changed=replace(f,concentration=6) if wash=='EMC' else replace(f,coefficient=float(new))
                model.washoff.update(('Land','Q0'),function=changed)
                if wash=='EXP':
                    # Exponential wash-off consumes accumulated surface mass.
                    model.loadings.add(q.InitialLoading(subcatchment=ref('subcatchments','S'),pollutant=ref('pollutants','Q0'),mass_per_area=5))
                    source+='[LOADINGS]\nS Q0 5\n'
                    original=self.solve(root,'wash-original',source)
                actual=self.solve(root,'wash-edited',model.to_document().text)
                self.assertEqual(actual,self.solve(root,'wash-oracle',source.replace(f'Land Q0 {wash} {old} ',f'Land Q0 {wash} {new} ')))
                self.assertTrue(original['quality']!=actual['quality'],wash)
            # EXP consumes available mass; an unconstrained RC curve can have
            # the same concentration at either initial loading.
            source=literal_quality('NONE','EXP')+'[LOADINGS]\nS Q0 .001 Q1 .001\n'
            model=Model.from_document(InpDocument.from_text(source),strict=True)
            original=self.solve(root,'loaded',source)
            model.loadings.update(('S','Q0'),mass_per_area=100)
            changed=self.solve(root,'larger',model.to_document().text)
            self.assertEqual(changed,self.solve(root,'literal-larger',source.replace('Q0 .001','Q0 100')))
            self.assertTrue(original['quality']!=changed['quality'])

    def test_count_units_curb_normalization_and_snow_only_have_native_effects(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for basis in ('AREA','CURBLENGTH'):
                model=selected(quality_model('EXT','EXP',basis))
                for id in model.pollutants:model.reinterpret_pollutant_units(id,'#/L')
                model.convert_units('CMS')
                base=base_model();base.convert_units('CMS')
                factor=2.2956e-5/.92903e-5 if basis=='AREA' else 1.
                source=base.to_document().text+('[POLLUTANTS]\nQ0 #/L 2 3 4 -.01 NO * 0 1 .5\nQ1 #/L 2 3 4 -.01 NO * 0 1 .5\n'
                    '[LANDUSES]\nLand 1 .5 .1\n[COVERAGES]\nS Land 100\n[BUILDUP]\n')
                source+=''.join(f'Land {id} EXT {10*factor:.17g} 2 Accumulation {basis}\n' for id in ('Q0','Q1'))+'[WASHOFF]\n'
                source+=''.join(f'Land {id} EXP {1.1/25.4**1.2:.17g} 1.2 40 10\n' for id in ('Q0','Q1'))
                source+=f'[TIMESERIES]\nAccumulation 0 {2*factor:.17g}\nAccumulation 8 {3*factor:.17g}\n'
                self.assertEqual(self.solve(root,'literal',source),self.solve(root,'converted',portable(model).to_document().text))
            # Antecedent DRY_DAYS buildup is initialized even for snow-only
            # pollutants. Start without it to exercise ongoing accumulation.
            source=literal_quality('EXT','EXP')
            model=Model.from_document(InpDocument.from_text(source),strict=True)
            model.update_options(dry_days=0)
            source=model.to_document().text
            original=self.solve(root,'normal',source)
            model.pollutants.update('Q0',snow_only=True)
            actual=self.solve(root,'snow-only',model.to_document().text)
            self.assertEqual(actual,self.solve(root,'snow-oracle',source.replace('Q0 MG/L 2 3 4 -0.01 NO','Q0 MG/L 2 3 4 -0.01 YES').replace('Q0 MG/L 2 3 4 -.01 NO','Q0 MG/L 2 3 4 -.01 YES')))
            self.assertTrue(original['quality']!=actual['quality'])

    def test_hydraulic_unit_conversion_has_independent_quality_coefficients(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for build in ('POW','EXP','SAT','EXT'):
                for wash in ('EXP','RC','EMC'):
                    with self.subTest(build=build,wash=wash):
                        model=selected(quality_model(build,wash));model.convert_units('CMS')
                        base=base_model();base.convert_units('CMS')
                        factor=(1/2.203)/(.92903e-5/2.2956e-5)
                        builds={'POW':f'POW {10*factor:.17g} {2*factor:.17g} 1.2',
                            'EXP':f'EXP {10*factor:.17g} .4 .7','SAT':f'SAT {10*factor:.17g} .8 3',
                            'EXT':f'EXT {10*factor:.17g} 2 Accumulation'}
                        washes={'EXP':f'EXP {1.1/25.4**1.2:.17g} 1.2 40 10','RC':f'RC {2/.02832**1.2:.17g} 1.2 40 10','EMC':'EMC 3 .7 40 10'}
                        source=base.to_document().text+('[POLLUTANTS]\nQ0 MG/L 2 3 4 -.01 NO * 0 1 .5\nQ1 UG/L 2 3 4 -.01 NO * 0 1 .5\n'
                            '[LANDUSES]\nLand 1 .5 .1\n[COVERAGES]\nS Land 100\n[BUILDUP]\n')
                        source+=''.join(f'Land {id} {builds[build]} AREA\n' for id in ('Q0','Q1'))+'[WASHOFF]\n'
                        source+=''.join(f'Land {id} {washes[wash]}\n' for id in ('Q0','Q1'))
                        if build=='EXT':source+=f'[TIMESERIES]\nAccumulation 0 {2*factor:.17g}\nAccumulation 8 {3*factor:.17g}\n'
                        self.assertEqual(self.solve(root,'independent',source),self.solve(root,'converted',portable(model).to_document().text))

    def test_concentration_mass_dwf_patterns_and_copollutant_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            source=literal_quality('POW','EMC').replace('Q0 MG/L 2 3 4 -.01 NO * 0','Q0 MG/L 2 3 4 -.01 NO Q1 .2').replace('Q1 UG/L 2 3 4 -.01 NO * 0','Q1 UG/L 2 3 4 -.01 NO Q0 .1')
            source+=('[TIMESERIES]\nQuality 0 2\nQuality 8 4\nMass 0 .01\nMass 8 .02\n'
                '[PATTERNS]\nDaily DAILY 1 2 3 4 5 6 7\nMonthly MONTHLY 1 2\n'
                '[INFLOWS]\nJ FLOW "" FLOW 1 1 .1\nJ Q0 Quality CONCEN 1 .5 2 Daily\nJ Q1 Mass MASS 126 .5 .01 Daily\n'
                '[DWF]\nJ FLOW .1 Monthly Daily\nJ Q0 2 Monthly Daily\nJ Q1 3 Daily Monthly\n')
            model=Model.from_document(InpDocument.from_text(source),strict=True)
            original=self.solve(root,'literal',source)
            self.assertEqual(original,self.solve(root,'rebuilt',portable(model).to_document().text))
            model.inflows.update(('J','POLLUTANT:Q1'),mass_factor=252)
            edited=self.solve(root,'mass-edited',model.to_document().text)
            self.assertEqual(edited,self.solve(root,'mass-oracle',source.replace('MASS 126','MASS 252')))
            self.assertNotEqual(original['quality'][1],edited['quality'][1])
            model=Model.from_document(InpDocument.from_text(source),strict=True)
            model.pollutants.move('Q0')
            moved=self.solve(root,'ordered',portable(model).to_document().text)
            self.assertEqual(moved['ids'][3],('Q1','Q0'))
            self.assertNotEqual(original['quality'][0][0][0],moved['quality'][0][0][1])

    def test_concentration_unit_conversion_matches_explicit_mg_ug_input(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for wash in ('EXP','RC','EMC'):
                source=literal_quality('POW',wash)+('[TIMESERIES]\nConc 0 2\nConc 8 3\n[INFLOWS]\n'
                    'J FLOW "" FLOW 1 1 .1\nJ Q0 Conc CONCEN 1 1 .2\nO Q0 "" MASS 126 1 .01\n[DWF]\nJ FLOW .1\nJ Q0 3\n')
                model=Model.from_document(InpDocument.from_text(source),strict=True)
                model.convert_pollutant_units('Q0','UG/L')
                oracle=source.replace('Q0 MG/L 2 3 4 -.01 NO * 0 1 .5','Q0 UG/L 2000 3000 4000 -.01 NO * 0 1000 500')
                oracle=oracle.replace('Conc 0 2','Conc 0 2000').replace('Conc 8 3','Conc 8 3000').replace('CONCEN 1 1 .2','CONCEN 1 1 200').replace('MASS 126 1 .01','MASS 126000 1 .01').replace('J Q0 3','J Q0 3000')
                if wash=='RC':oracle=oracle.replace('Land Q0 RC 2','Land Q0 RC 2000')
                if wash=='EMC':oracle=oracle.replace('Land Q0 EMC 3','Land Q0 EMC 3000')
                self.assertEqual(self.solve(root,'oracle',oracle),self.solve(root,'converted',portable(model).to_document().text))

    def test_typed_quality_establishes_hotstart_and_runoff_cache_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);model=selected(quality_model());source=model.to_document().text
            for kind,Data,Layout,Manifest in (('HOTSTART',HotstartData,HotstartLayout,HotstartManifest),('RUNOFF',RunoffData,RunoffLayout,CacheManifest)):
                cache=root/(kind+'.bin');copied=root/(kind+'-copy.bin')
                self.solve(root,'producer',source+f'[FILES]\nSAVE {kind} "{cache}"\n')
                layout=Layout.from_model(model);raw=cache.read_bytes();data=Data.from_bytes(raw,layout=layout)
                self.assertEqual(data.to_bytes(),raw);data.write(copied)
                expected=self.solve(root,'original-cache',source+f'[FILES]\nUSE {kind} "{cache}"\n')
                actual=self.solve(root,'copied-cache',source+f'[FILES]\nUSE {kind} "{copied}"\n')
                if kind=='RUNOFF':actual['report']=actual['report'].replace(str(copied),str(cache))
                self.assertEqual(expected,actual)
                consumer=model.copy();bind(consumer,kind,'USE',copied)
                self.assertTrue(check_files(consumer,interface_manifests={kind:Manifest.asserted(raw,layout=layout)}).complete)

    def test_routing_interface_maps_both_pollutants_and_rejects_wrong_units(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);cache=root/'routing.txt';model=selected(quality_model())
            self.solve(root,'producer',model.to_document().text+f'[FILES]\nSAVE OUTFLOWS "{cache}"\n')
            data=RoutingInterface.read(cache)
            self.assertEqual(tuple(c.name for c in data.constituents),('FLOW','Q0','Q1'))
            bind(model,'INFLOWS','USE',cache)
            self.assertTrue(check_files(model).complete,check_files(model).report)
            expected=self.solve(root,'consumer',model.to_document().text)
            copy=root/'copy.txt';data.write(copy)
            consumer=model.copy();consumer.files.remove(('INFLOWS','USE'));bind(consumer,'INFLOWS','USE',copy)
            self.assertEqual(expected,self.solve(root,'copy-consumer',consumer.to_document().text))
            bad=replace(data,constituents=(data.constituents[0],replace(data.constituents[1],units='UG/L'),data.constituents[2]))
            bad.write(copy)
            self.assertFalse(check_files(consumer).report.is_valid)

    def test_sweeping_and_bmp_removal_have_independent_native_effects(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            source=literal_quality('POW','EXP').replace('Land 1 .5 .1','Land .02 .5 .01')
            original=self.solve(root,'original',source)
            for field,number,old,new in (('sweep_availability',0,'Land .02 .5 .01','Land .02 0 .01'),
                    ('bmp_removal',100,'Land Q0 EXP 1.1 1.2 40 10','Land Q0 EXP 1.1 1.2 40 100')):
                model=Model.from_document(InpDocument.from_text(source),strict=True)
                if field=='sweep_availability':model.landuses.update('Land',**{field:number})
                else:model.washoff.update(('Land','Q0'),**{field:number})
                actual=self.solve(root,'edited',portable(model).to_document().text)
                self.assertEqual(actual,self.solve(root,'oracle',source.replace(old,new)))
                self.assertTrue(original['quality']!=actual['quality'],field)

    def test_native_constituent_prefixes_and_ignored_tail_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            base=literal_quality('NONE','EMC')
            tail=('[PATTERNS]\nDaily DAILY 1\n[INFLOWS]\n'
                'J FLOWER "" ignored ignored 1 .1 Daily trailing\n'
                'J Q0 "" CONCENTRATION ignored 1 2 Daily trailing\n'
                '[DWF]\nJ FLOWER .2 Daily "" "" "" ignored\n')
            for defined in (False,True):
                source=base+tail
                if defined:
                    source=source.replace('Q0 MG/L','FLOWER MG/L').replace('Land Q0','Land FLOWER').replace('J Q0','J FLOWER').replace('J FLOWER "" ignored ignored 1 .1 Daily trailing','J FLOW "" FLOW 1 1 .1')
                model=Model.from_document(InpDocument.from_text(source),strict=True)
                self.assertEqual(self.solve(root,'literal',source),self.solve(root,'normalized',portable(model).to_document().text))


if __name__=='__main__':
    unittest.main()
