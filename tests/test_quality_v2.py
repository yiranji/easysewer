"""Complete pollutant/land-use variants, graph persistence and dimensions."""

from dataclasses import replace
from datetime import date, time, timedelta
import unittest

from easysewer.model import Model, Ref
from easysewer.model import quality as q
from easysewer.model.inflows import ConcentrationInflow, MassInflow, DryWeatherConcentration, FlowInflow
from easysewer.model.resources import InlineTimeSeries, SeriesPoint, Pattern
from easysewer.io.inp import InpDocument
from easysewer.io.hotstart import HotstartLayout
from easysewer.io.runoff_cache import RunoffLayout
from easysewer.scenario import ScenarioPatch, RenameRecord, SetFields, FieldChange
from easysewer.validation import ValidationError
from test_hydrology_v2 import hydrology_model
from test_scenario_v2 import portable


def ref(namespace,key):
    return Ref(collection='swmm:'+namespace,key=key)


BUILDS = {
    'NONE':q.NoBuildup(),
    'POW':q.PowerBuildup(maximum=10,coefficient=2,exponent=1.2),
    'EXP':q.ExponentialBuildup(maximum=10,rate=.4,unused_parameter=.7),
    'SAT':q.SaturationBuildup(maximum=10,half_saturation_days=3,unused_parameter=.8),
    'EXT':q.ExternalBuildup(maximum=10,scale_factor=2,series=ref('timeseries','Accumulation')),
}
WASHES = {
    'NONE':q.NoWashoff(),
    'EXP':q.ExponentialWashoff(coefficient=1.1,exponent=1.2),
    'RC':q.RatingWashoff(coefficient=2,exponent=1.2),
    'EMC':q.EventMeanConcentration(concentration=3,unused_exponent=.7),
}


def quality_model(build='POW',wash='EXP',normalizer='AREA'):
    model=hydrology_model()
    model.update_options(end_date=date(2020,1,30),end_time=time(8),routing_step=timedelta(seconds=60),dry_days=5)
    for id,units in (('Q0','MG/L'),('Q1','UG/L')):
        model.pollutants.add(q.Pollutant(id=id,units=units,rainfall_concentration=2,groundwater_concentration=3,rdii_concentration=4,
            decay_rate=-.01,snow_only=False,dwf_concentration=1,initial_concentration=.5))
    model.landuses.add(q.LandUse(id='Land',sweep_interval=1,sweep_availability=.5,days_since_sweeping=.1))
    model.coverages.add(q.Coverage(subcatchment=ref('subcatchments','S'),landuse=ref('landuses','Land'),percent=100))
    for id in ('Q0','Q1'):
        model.buildup.add(q.Buildup(landuse=ref('landuses','Land'),pollutant=ref('pollutants',id),function=BUILDS[build],normalizer=normalizer if build!='NONE' else 'AREA'))
        model.washoff.add(q.Washoff(landuse=ref('landuses','Land'),pollutant=ref('pollutants',id),function=WASHES[wash],
            **({} if wash=='NONE' else dict(sweeping_removal=40,bmp_removal=10))))
    if build=='EXT':
        model.timeseries.add(InlineTimeSeries(id='Accumulation',points=(SeriesPoint(time=timedelta(),value=2),SeriesPoint(time=timedelta(hours=8),value=3))))
    return model


def quality_corpus():
    for build in BUILDS:
        for wash in WASHES:
            yield quality_model(build,wash)
    model=quality_model()
    model.loadings.add(q.InitialLoading(subcatchment=ref('subcatchments','S'),pollutant=ref('pollutants','Q0'),mass_per_area=4))
    model.inflows.add(FlowInflow(node=ref('nodes','J'),baseline=.1,scale_factor=1))
    model.inflows.add(ConcentrationInflow(node=ref('nodes','J'),constituent=ref('pollutants','Q0'),baseline=2,scale_factor=1))
    model.inflows.add(MassInflow(node=ref('nodes','J'),constituent=ref('pollutants','Q1'),baseline=3,mass_factor=126,scale_factor=1))
    model.dwf.add(DryWeatherConcentration(node=ref('nodes','J'),constituent=ref('pollutants','Q0'),baseline=4))
    yield model


class QualityTests(unittest.TestCase):
    def assert_quality_equal(self,a,b):
        for namespace in ('pollutants','landuses','coverages','loadings','buildup','washoff','inflows','dwf'):
            self.assertEqual(tuple(a.collection('swmm:'+namespace).items()),tuple(b.collection('swmm:'+namespace).items()),namespace)

    def test_every_formula_source_free_json_and_full_inp_roundtrip(self):
        for model in quality_corpus():
            with self.subTest(buildup=model.buildup[('Land','Q0')].function,washoff=model.washoff[('Land','Q0')].function):
                self.assertTrue(model.validate(for_run=True).is_valid,model.validate(for_run=True))
                source=model.to_document().text
                parsed=Model.from_document(InpDocument.from_text(source),strict=True)
                self.assertEqual(source,parsed.to_document().text)
                self.assert_quality_equal(model,parsed)
                self.assert_quality_equal(model,portable(model))
                self.assert_quality_equal(model,Model.from_document(parsed.to_document(normalize=True),strict=True))

    def test_pollutant_optional_tail_co_pollutant_and_native_ignored_slots(self):
        for tail in ('',' NO',' YES *',' NO * 0',' NO * 0 4',' NO * 0 4 5',' NO Q1 .2',' NO Q1 .2 4 5'):
            source=hydrology_model().to_document().text+'[POLLUTANTS]\nQ0 MG/L 1 2 3 -.1'+tail+'\nQ1 UG/L 0 0 0 0\n'
            model=Model.from_document(InpDocument.from_text(source),strict=True)
            self.assertEqual(model.to_document().text,source)
            self.assert_quality_equal(model,portable(model))
            self.assert_quality_equal(model,Model.from_document(model.to_document(normalize=True),strict=True))
        model=Model.from_document(InpDocument.from_text('[POLLUTANTS]\nQ0 MG/L 1 2 3 0 NO * garbage 4 5\n'),strict=True)
        self.assertIn('quality.ignored_co_pollutant',{d.code for d in model.validate().diagnostics})
        self.assertIsNone(model.pollutants['Q0'].co_fraction)
        model.pollutants.update('Q0',co_pollutant=ref('pollutants','*'))
        self.assertFalse(model.validate().is_valid)

    def test_multi_pair_repeated_assignments_preserve_and_edit_atomically(self):
        base=quality_model(); base.coverages.remove(('S','Land'))
        base.landuses.add(q.LandUse(id='Other'))
        source=base.to_document().text+'[COVERAGES]\nS Land 20 Other 30 ; shared\nS Land 70 ; final\n[LOADINGS]\nS Q0 2 Q1 3\nS Q0 4\n'
        model=Model.from_document(InpDocument.from_text(source),strict=True)
        self.assertEqual(source,model.to_document().text)
        self.assertEqual(model.coverages[('S','Land')].percent,70)
        self.assertEqual(model.loadings[('S','Q0')].mass_per_area,4)
        model.coverages.update(('S','Land'),percent=60)
        text=model.to_document().text
        self.assertIn('; shared',text)
        reread=Model.from_document(InpDocument.from_text(text),strict=True)
        self.assert_quality_equal(model,reread)
        model.coverages.remove(('S','Land'))
        reread=Model.from_document(model.to_document(),strict=True)
        self.assertEqual(tuple(reread.coverages), (('S','Other'),))
        self.assertEqual(reread.loadings[('S','Q1')].mass_per_area,3)

    def test_graph_renames_deletion_and_scenario_rollback(self):
        model=list(quality_corpus())[-1]
        model.pollutants.update('Q1',co_pollutant=ref('pollutants','Q0'),co_fraction=.2)
        model.pollutants.rename('Q0','Solids'); model.landuses.rename('Land','Urban');model.subcatchments.rename('S','Area');model.nodes.rename('J','Receiving')
        self.assertIn(('Receiving','POLLUTANT:Solids'),model.inflows)
        self.assertIn(('Urban','Solids'),model.buildup)
        self.assertIn(('Area','Urban'),model.coverages)
        self.assertIn(('Area','Solids'),model.loadings)
        self.assertEqual(model.pollutants['Q1'].co_pollutant.key,'Solids')
        with self.assertRaises(ValidationError): model.pollutants.remove('Solids')
        before=model.to_json_document().to_bytes()
        patch=ScenarioPatch(flow_units='CFS',operations=(RenameRecord(target=ref('landuses','Urban'),new_id='Changed'),
            SetFields(target=ref('coverages',('Area','Changed')),changes=(FieldChange(name='percent',value=101),))))
        with self.assertRaises(ValidationError): patch.apply(model)
        self.assertEqual(model.to_json_document().to_bytes(),before)
        self.assert_quality_equal(model,portable(model))

    def test_units_follow_formula_pollutant_mass_and_user_curb(self):
        area=.92903e-5/2.2956e-5; mass=1/2.203
        for build in BUILDS:
            for wash in WASHES:
                for normalizer in ('AREA','CURBLENGTH'):
                    model=quality_model(build,wash,normalizer)
                    model.loadings.add(q.InitialLoading(subcatchment=ref('subcatchments','S'),pollutant=ref('pollutants','Q0'),mass_per_area=4))
                    before=model.pollutants['Q0']; model.convert_units('CMS')
                    self.assertEqual(before,model.pollutants['Q0'])
                    self.assertEqual(model.subcatchments['S'].curb_length,hydrology_model().subcatchments['S'].curb_length)
                    if build!='NONE':
                        expected=10*mass/(area if normalizer=='AREA' else 1)
                        self.assertAlmostEqual(model.buildup[('Land','Q0')].function.maximum,expected)
                    if build=='EXT': self.assertAlmostEqual(model.timeseries['Accumulation'].points[0].value,2*mass/(area if normalizer=='AREA' else 1))
                    if wash=='EXP': self.assertAlmostEqual(model.washoff[('Land','Q0')].function.coefficient,1.1/25.4**1.2)
                    if wash=='RC': self.assertAlmostEqual(model.washoff[('Land','Q0')].function.coefficient,2/.02832**1.2)
                    self.assertAlmostEqual(model.loadings[('S','Q0')].mass_per_area,4*mass/area)
        model=quality_model('EXT','RC')
        for id in model.pollutants: model.reinterpret_pollutant_units(id,'#/L')
        model.convert_units('CMS')
        self.assertAlmostEqual(model.buildup[('Land','Q0')].function.maximum,10/area)
        self.assertAlmostEqual(model.timeseries['Accumulation'].points[0].value,2/area)

    def test_shared_external_buildup_dimensions_reject_incompatible_consumers(self):
        model=quality_model('EXT')
        model.buildup.update(('Land','Q1'),normalizer='CURBLENGTH')
        self.assertIn('resource.conflicting_dimensions',{d.code for d in model.validate().errors})
        with self.assertRaises(ValidationError): model.convert_units('CMS')

    def test_invalid_records_and_reserved_flow_do_not_export(self):
        cases=('[POLLUTANTS]\nFLOW MG/L 0 0 0 0\n','[POLLUTANTS]\nQ MG/L 0 -1 0 0\n',
            '[LANDUSES]\nL 1 .2\n','[LANDUSES]\nL\nL\n','[BUILDUP]\nL Q POW 2 1 .001 AREA\n',
            '[WASHOFF]\nL Q RC 1 11\n','[COVERAGES]\nS L 20 Other\n','[LOADINGS]\nS Q NaN\n')
        for source in cases:
            model=Model.from_document(InpDocument.from_text(source))
            self.assertFalse(model.validate().is_valid,source)
            self.assertEqual(model.document.text,source)
            with self.assertRaises(ValidationError): model.to_document()
        model=quality_model();model.pollutants.update('Q0',co_pollutant='bad')
        self.assertFalse(model.validate().is_valid)

    def test_quality_layout_adapters_use_actual_order_and_concentration_units(self):
        model=quality_model()
        source=model.to_document().text
        source=source.replace('Q0 MG/L','X MG/L').replace('Q1 UG/L','Q0 UG/L').replace('X MG/L','Q1 MG/L')
        model=Model.from_document(InpDocument.from_text(source),strict=True)
        for layout in (HotstartLayout.from_model(model),RunoffLayout.from_model(model)):
            self.assertEqual(tuple((p.id,p.units) for p in layout.pollutants),(('Q1','MG/L'),('Q0','UG/L')))
        self.assertEqual(HotstartLayout.from_model(model).landuses,('Land',))

    def test_concentration_mass_and_dwf_variants_and_run_requirements(self):
        model=quality_model()
        for tail,cls in (('',ConcentrationInflow),(' CONCEN ignored .5 2',ConcentrationInflow),(' MASS',MassInflow),(' MASS 126 .5 2',MassInflow)):
            source=model.to_document().text+'[INFLOWS]\nJ Q0 ""'+tail+'\n[DWF]\nJ Q1 3\n'
            parsed=Model.from_document(InpDocument.from_text(source),strict=True)
            self.assertIs(type(parsed.inflows[('J','POLLUTANT:Q0')]),cls)
            self.assert_quality_equal(parsed,portable(parsed))
            self.assert_quality_equal(parsed,Model.from_document(parsed.to_document(normalize=True),strict=True))
            errors={d.code for d in parsed.validate(for_run=True).errors}
            self.assertEqual('inflow.concentration_needs_flow' in errors,cls is ConcentrationInflow)
            parsed.inflows.add(FlowInflow(node=ref('nodes','J'),baseline=.1))
            self.assertTrue(parsed.validate(for_run=True).is_valid)
        model.inflows.add(ConcentrationInflow(node=ref('nodes','O'),constituent=ref('pollutants','Q0'),baseline=1))
        self.assertTrue(model.validate(for_run=True).is_valid)

    def test_pollutant_unit_conversion_updates_all_known_consumers_atomically(self):
        model=quality_model('POW','RC')
        model.pollutants.update('Q1',co_pollutant=ref('pollutants','Q0'),co_fraction=.2)
        model.pollutants.update('Q0',co_pollutant=ref('pollutants','Q1'),co_fraction=.3)
        model.timeseries.add(InlineTimeSeries(id='Concentration',points=(SeriesPoint(time=timedelta(),value=2),)))
        model.inflows.add(ConcentrationInflow(node=ref('nodes','J'),constituent=ref('pollutants','Q0'),series=ref('timeseries','Concentration'),baseline=3))
        model.inflows.add(MassInflow(node=ref('nodes','O'),constituent=ref('pollutants','Q0'),baseline=4,mass_factor=126))
        model.dwf.add(DryWeatherConcentration(node=ref('nodes','J'),constituent=ref('pollutants','Q0'),baseline=5))
        with self.assertRaises(ValidationError): model.pollutants.update('Q0',units='UG/L')
        model.convert_pollutant_units('Q0','UG/L')
        self.assertEqual(model.pollutants['Q0'].rainfall_concentration,2000)
        self.assertEqual(model.pollutants['Q0'].co_fraction,300)
        self.assertEqual(model.pollutants['Q1'].co_fraction,.0002)
        self.assertEqual(model.timeseries['Concentration'].points[0].value,2000)
        self.assertEqual(model.inflows[('J','POLLUTANT:Q0')].baseline,3000)
        self.assertEqual(model.inflows[('O','POLLUTANT:Q0')].mass_factor,126000)
        self.assertEqual(model.dwf[('J','POLLUTANT:Q0')].baseline,5000)
        self.assertEqual(model.washoff[('Land','Q0')].function.coefficient,2000)
        self.assertEqual(model.buildup[('Land','Q0')].function.maximum,10)
        model.convert_pollutant_units('Q0','MG/L')
        self.assertEqual(model.timeseries['Concentration'].points[0].value,2)
        model.reinterpret_pollutant_units('Q1','MG/L')
        model.inflows.add(ConcentrationInflow(node=ref('nodes','O'),constituent=ref('pollutants','Q1'),series=ref('timeseries','Concentration')))
        before=model.to_json_document().to_bytes()
        with self.assertRaises(ValidationError): model.convert_pollutant_units('Q0','UG/L')
        self.assertEqual(model.to_json_document().to_bytes(),before)
        with self.assertRaises(ValidationError): model.convert_pollutant_units('Q0','#/L')

    def test_native_prefixes_ignored_columns_and_defined_flow_prefix_pollutant(self):
        base=quality_model().to_document().text
        source=base+('[PATTERNS]\nDaily DAILY 1\n[INFLOWS]\n'
            'J FLOWER "" ignored ignored 1 .1 Daily trailing\n'
            'J Q0 "" CONCENTRATION ignored 1 2 Daily trailing\n'
            '[DWF]\nJ FLOWER .2 Daily "" "" "" ignored\n')
        parsed=Model.from_document(InpDocument.from_text(source),strict=True)
        self.assertIs(type(parsed.inflows[('J','FLOW')]),FlowInflow)
        self.assertEqual(parsed.dwf[('J','FLOW')].baseline,.2)
        self.assertIn('inflow.ignored_columns',{d.code for d in parsed.validate().diagnostics})
        self.assertTrue(parsed.validate(for_run=True).is_valid)
        self.assert_quality_equal(parsed,Model.from_document(parsed.to_document(normalize=True),strict=True))
        defined=source.replace('Q0 MG/L','FLOWER MG/L').replace('J Q0','J FLOWER').replace('J FLOWER "" ignored ignored 1 .1 Daily trailing\n','J FLOW "" FLOW 1 1 .1\n').replace('Land Q0','Land FLOWER')
        parsed=Model.from_document(InpDocument.from_text(defined),strict=True)
        self.assertIs(type(parsed.inflows[('J','POLLUTANT:FLOWER')]),ConcentrationInflow)
        self.assertIs(type(parsed.dwf[('J','POLLUTANT:FLOWER')]),DryWeatherConcentration)

    def test_overridden_buildup_source_dependencies_and_invalid_drafts(self):
        base=quality_model().to_document().text
        for series,code in (('Missing','quality.source_reference'),('Rain','quality.native_rainfall_series')):
            source=base+f'[BUILDUP]\nLand Q0 EXT 10 1 {series} AREA\nLand Q0 POW 10 2 1.2 AREA\n'
            model=Model.from_document(InpDocument.from_text(source),strict=True)
            self.assertIn(code,{d.code for d in model.validate(for_run=True).errors})
            self.assertTrue(model.validate(for_run=True,normalize=True).is_valid)
        model=quality_model('EXT')
        # Imported/draft invalid types must produce diagnostics, not a lookup
        # failure while inferring shared time-series dimensions.
        with model._store._permit_context_change('pollutant_units'):
            model.pollutants.update('Q0',units='invalid')
        self.assertFalse(model.validate().is_valid)

    def test_pollutant_conversion_rejects_external_data_without_mutation(self):
        from easysewer.model.resources import FileTimeSeries
        from easysewer.model.values import FileReference
        model=quality_model()
        model.timeseries.add(FileTimeSeries(id='Outside',file=FileReference(path='quality.dat')))
        model.inflows.add(ConcentrationInflow(node=ref('nodes','J'),constituent=ref('pollutants','Q0'),series=ref('timeseries','Outside')))
        before=model.to_json_document().to_bytes()
        with self.assertRaises(ValidationError): model.convert_pollutant_units('Q0','UG/L')
        self.assertEqual(before,model.to_json_document().to_bytes())


if __name__=='__main__':
    unittest.main()
