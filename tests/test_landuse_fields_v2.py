"""Land-use formulas, contextual units and exact multi-pair source history."""
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import timedelta
import unittest

from easysewer.model import Model, Ref
from easysewer.model import quality as q
from easysewer.model.resources import InlineTimeSeries, SeriesPoint
from easysewer.io.inp import InpDocument
from easysewer.validation import ValidationError
from test_hydrology_fields_v2 import fixture as hydrology_fixture, UNITS
from test_quality_v2 import BUILDS, WASHES

POLLUTANT_UNITS = ('MG/L', 'UG/L', '#/L')
NAMES = ('landuses', 'coverages', 'loadings', 'buildup', 'washoff')
def ref(name, key): return Ref(collection='swmm:' + name, key=key)
def load(text): return Model.from_document(InpDocument.from_text(text, source='landuse.inp'), strict=True)
LAND = ref('landuses', 'Land')
BUILD = ref('buildup', ('Land', 'Q0'))
WASH = ref('washoff', ('Land', 'Q0'))
LOAD = ref('loadings', ('S', 'Q0'))
COVER = ref('coverages', ('S', 'Land'))


def fixture(units='CFS', pu='MG/L', build='POW', wash='EXP', normalizer='AREA', mode='explicit'):
    m = load(hydrology_fixture(units=units))
    m.update_options(dry_days=5)
    m.subcatchments.update('S', curb_length=10)
    m.pollutants.add(q.Pollutant(id='Q0', units=pu, rainfall_concentration=2,
        groundwater_concentration=3, rdii_concentration=4, decay_rate=-.01))
    m.landuses.add(q.LandUse(id='Land', **({} if mode == 'defaults' else
        dict(sweep_interval=1, sweep_availability=.5, days_since_sweeping=.1))))
    m.coverages.add(q.Coverage(subcatchment=ref('subcatchments', 'S'), landuse=LAND, percent=100))
    m.loadings.add(q.InitialLoading(subcatchment=ref('subcatchments', 'S'), pollutant=ref('pollutants', 'Q0'),
        mass_per_area=0 if mode == 'defaults' else 4))
    m.buildup.add(q.Buildup(landuse=LAND, pollutant=ref('pollutants', 'Q0'), function=BUILDS[build],
        normalizer=normalizer if build != 'NONE' else 'AREA'))
    m.washoff.add(q.Washoff(landuse=LAND, pollutant=ref('pollutants', 'Q0'), function=WASHES[wash],
        **({} if wash == 'NONE' or mode == 'defaults' else dict(sweeping_removal=40, bmp_removal=10))))
    if build == 'EXT':
        m.timeseries.add(InlineTimeSeries(id='Accumulation', points=(SeriesPoint(time=timedelta(), value=2),
            SeriesPoint(time=timedelta(minutes=20), value=3))))
    return m.to_document().text


def queries(m):
    result = []
    def walk(owner, value, path=()):
        if is_dataclass(value):
            for f in fields(value):
                p = path + (f.name,)
                result.extend((m.inspect_field(owner, p), m.field_provenance(owner, p)))
                walk(owner, getattr(value, f.name), p)
    for namespace in NAMES:
        for key, row in m.collection('swmm:' + namespace).items(): walk(ref(namespace, key), row)
    return tuple(result)


class LandUseFieldTests(unittest.TestCase):
    def test_all_formula_dimensions_and_json(self):
        for units in UNITS:
            for pu in POLLUTANT_UNITS:
                for normalizer in ('AREA', 'CURBLENGTH'):
                    for build in BUILDS:
                        for wash in WASHES:
                            with self.subTest(units=units, pu=pu, normalizer=normalizer, build=build, wash=wash):
                                m = load(fixture(units, pu, build, wash, normalizer))
                                before = queries(m)
                                for info in before[::2]:
                                    self.assertIn(info.semantics.effective.status, ('known', 'not_applicable'), info.path)
                                    self.assertNotEqual(info.semantics.unit.status, 'unknown', info.path)
                                    self.assertIn(info.provenance.status, ('explicit', 'derived', 'omitted'), info.path)
                                self.assertEqual(queries(Model.from_json_document(m.to_json_document(), strict=True)), before)
                                mass = 'count' if pu == '#/L' else 'lb' if units in UNITS[:3] else 'kg'
                                area = 'acre' if units in UNITS[:3] else 'ha'
                                self.assertEqual(m.inspect_field(LOAD, 'mass_per_area').semantics.unit.value, mass + '/' + area)
                                if build != 'NONE':
                                    per = area if normalizer == 'AREA' else 'user curb'
                                    self.assertEqual(m.inspect_field(BUILD, ('function', 'maximum')).semantics.unit.value, mass + '/' + per)
                                if build == 'SAT':
                                    self.assertEqual(m.inspect_field(BUILD, ('function', 'half_saturation_days')).semantics.unit.value, 'day')
                                if wash == 'EMC':
                                    self.assertEqual(m.inspect_field(WASH, ('function', 'concentration')).semantics.unit.value,
                                        {'MG/L': 'mg/L', 'UG/L': 'ug/L', '#/L': 'count/L'}[pu])

    def test_defaults_zero_and_formula_specific_coefficients(self):
        m = load(fixture(mode='defaults'))
        for name in ('sweep_interval', 'sweep_availability', 'days_since_sweeping'):
            fact = m.inspect_field(LAND, name)
            self.assertIsNone(fact.value); self.assertEqual(fact.semantics.effective.value, 0)
            self.assertEqual(fact.provenance.status, 'omitted')
        self.assertEqual(m.inspect_field(WASH, 'bmp_removal').semantics.effective.value, 0)
        self.assertEqual(m.inspect_field(WASH, 'sweeping_removal').semantics.unit.value, '%')
        self.assertEqual(m.inspect_field(LAND, 'sweep_availability').semantics.unit.value, '1')
        self.assertEqual(m.inspect_field(LOAD, 'mass_per_area').semantics.effective.value, 0)
        self.assertIn('positive', m.inspect_field(LOAD, 'mass_per_area').semantics.effective.reason)
        self.assertEqual(m.inspect_field(BUILD, ('function', 'coefficient')).semantics.unit.value, 'lb/acre/day^1.2')
        self.assertEqual(m.inspect_field(WASH, ('function', 'coefficient')).semantics.unit.value, '1/h/(in/h)^1.2')
        m.washoff.update(WASH.key, function=q.RatingWashoff(coefficient=2, exponent=0))
        self.assertEqual(m.inspect_field(WASH, ('function', 'coefficient')).semantics.unit.value, 'mg/s/(CFS)^0')
        m.landuses.update('Land', sweep_interval=0)
        self.assertEqual(m.inspect_field(LAND, 'sweep_interval').semantics.effective.value, 0)

    def test_repeated_pairs_on_same_and_separate_rows(self):
        m = load(fixture()); m.landuses.add(q.LandUse(id='Other')); m.coverages.remove(COVER.key); m.loadings.remove(LOAD.key)
        source = m.to_document().text + '[COVERAGES]\nS Land 20 Other 30 Land 40\nS Land 70\n[LOADINGS]\nS Q0 2 Q0 3\nS Q0 4\n'
        m = load(source)
        self.assertEqual(m.to_document().text, source)
        self.assertEqual([d.contributes for d in m.field_provenance(COVER, 'percent').declarations], [False, False, True])
        self.assertEqual([d.contributes for d in m.field_provenance(ref('coverages', ('S', 'Other')), 'percent').declarations], [True])
        self.assertEqual([d.contributes for d in m.field_provenance(LOAD, 'mass_per_area').declarations], [False, False, True])
        self.assertEqual(m.field_provenance(COVER, 'percent').declarations[1].tokens[0].value, '40')
        self.assertEqual(queries(Model.from_json_document(m.to_json_document(), strict=True)), queries(m))

    def test_history_variant_switch_and_ignored_slots(self):
        source = fixture(build='SAT', wash='EMC') + '[BUILDUP]\nLand Q0 EXP 10 .4 .7 AREA\nLand Q0 SAT 12 .8 3 CURBLENGTH\n[WASHOFF]\nLand Q0 NONE ignored anything\n'
        m = load(source)
        self.assertEqual(m.inspect_field(BUILD, ('function', 'half_saturation_days')).semantics.effective.value, 3)
        old = m.field_provenance(BUILD, ('function', 'unused_parameter'))
        self.assertEqual([d.tokens[0].value for d in old.declarations], ['0.8', '.7', '.8'])
        self.assertEqual([d.contributes for d in old.declarations], [False, False, True])
        self.assertEqual(m.inspect_field(BUILD, ('function', 'unused_parameter')).semantics.effective.status, 'not_applicable')
        self.assertEqual(m.inspect_field(WASH, 'bmp_removal').semantics.effective.status, 'not_applicable')
        self.assertTrue(all(not d.contributes for d in m.field_provenance(WASH, 'bmp_removal').declarations))
        self.assertTrue(any(d.role == 'retained' and not d.contributes for d in m.field_provenance(WASH, 'function').declarations))
        m = load(fixture(build='NONE', wash='EMC'))
        self.assertEqual(m.inspect_field(BUILD, 'normalizer').semantics.effective.status, 'not_applicable')
        self.assertEqual(m.inspect_field(WASH, ('function', 'unused_exponent')).semantics.effective.status, 'not_applicable')

    def test_reference_extensions_shared_dimensions_and_invalid_coverage(self):
        m = load(fixture(build='EXT')); m.landuses.add(q.LandUse(id='Other'))
        m.buildup.add(replace(m.buildup[BUILD.key], landuse=ref('landuses', 'Other'), normalizer='CURBLENGTH'))
        self.assertEqual(m.inspect_field(BUILD, 'function').semantics.effective.status, 'ambiguous')
        m = load(fixture()); row = m.coverages[COVER.key]; m.coverages.remove(COVER.key)
        m.coverages.add(replace(row, landuse=ref('landuses', 'Missing')))
        self.assertEqual(m.inspect_field(ref('coverages', ('S', 'Missing')), 'percent').semantics.effective.status, 'invalid')
        m = load(fixture()); m.landuses.add(q.LandUse(id='Other'))
        m.coverages.add(q.Coverage(subcatchment=ref('subcatchments', 'S'), landuse=ref('landuses', 'Other'), percent=1))
        self.assertEqual(m.inspect_field(COVER, 'percent').semantics.effective.status, 'invalid')
        @dataclass(frozen=True, kw_only=True)
        class Extension(q.PowerBuildup): pass
        m = load(fixture()); m.buildup.update(BUILD.key, function=Extension(maximum=10, coefficient=2, exponent=1.2))
        self.assertEqual(m.inspect_field(BUILD, 'function').semantics.effective.status, 'unknown')
        @dataclass(frozen=True, kw_only=True)
        class LandExtension(q.LandUse): pass
        m = load(fixture()); m.landuses.replace('Land', LandExtension(id='Land'))
        self.assertEqual(m.inspect_field(BUILD, 'function').semantics.effective.status, 'unknown')

    def test_rename_rollback_unit_conversion_and_original_sources(self):
        m = load(fixture(wash='RC')); before = queries(m)
        with self.assertRaises(RuntimeError):
            with m.transaction():
                m.landuses.rename('Land', 'Urban'); m.pollutants.rename('Q0', 'Solids'); raise RuntimeError
        self.assertEqual(queries(m), before)
        m.landuses.rename('Land', 'Urban'); m.pollutants.rename('Q0', 'Solids'); m.subcatchments.rename('S', 'Area')
        owner = ref('buildup', ('Urban', 'Solids'))
        self.assertEqual(m.field_provenance(owner, ('landuse', 'key')).value.value, 'Land')
        with self.assertRaises(ValidationError): m.landuses.remove('Urban')
        m.convert_units('CMS'); m.convert_pollutant_units('Solids', 'UG/L')
        self.assertEqual(m.inspect_field(owner, ('function', 'maximum')).semantics.unit.value, 'kg/ha')
        info = m.inspect_field(ref('washoff', ('Urban', 'Solids')), ('function', 'coefficient'))
        self.assertEqual(info.semantics.unit.value, 'ug/s/(CMS)^1.2'); self.assertTrue(info.changed)
        self.assertEqual(queries(Model.from_json_document(m.to_json_document(), strict=True)), queries(m))

    def test_malformed_group_is_unclaimed(self):
        source = '[LANDUSES]\nL\n[COVERAGES]\nS L 10\nS L bad\n'
        m = Model.from_document(InpDocument.from_text(source))
        self.assertFalse(m.validate().is_valid)
        self.assertEqual(len(m.coverages), 0)
        self.assertEqual(m.document.text, source)


if __name__ == '__main__': unittest.main()
