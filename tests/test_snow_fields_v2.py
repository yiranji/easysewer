"""Snow input defaults, transfer applicability and monthly-assignment provenance."""
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import timedelta
import unittest

from easysewer.io.inp import InpDocument
from easysewer.model import Model, Ref
from easysewer.model import hydrology as h, climate as c
from easysewer.model.resources import Pattern, InlineTimeSeries, SeriesPoint
from easysewer.model.units import UnitContext
from test_hydrology_fields_v2 import fixture as hydrology_fixture, UNITS
from test_hydrology_v2 import snowpack
from test_scenario_v2 import portable

SNOW = Ref(collection='swmm:snowpacks', key='Snow')
ADJUST = Ref(collection='swmm:subcatchment_adjustments', key='S')


def load(text):
    return Model.from_document(InpDocument.from_text(text, source='snow-fields.inp'), strict=True)


def fixture(units='CFS', missing=0, mode='active', month=1):
    model = load(hydrology_fixture(units=units))
    model.update_options(start_date=model.options.start_date.replace(month=month, day=1),
                         end_date=model.options.end_date.replace(month=month, day=1),
                         report_start_date=model.options.report_start_date.replace(month=month, day=1))
    cold, warm, base = (25, 45, 32) if units in UNITS[:3] else (-4, 7, 0)
    model.timeseries.add(InlineTimeSeries(id='Air', points=tuple(SeriesPoint(time=timedelta(minutes=t), value=v)
                                                               for t, v in ((0, cold), (8, cold), (9, warm), (20, warm)))))
    model.update_climate(temperature=c.SeriesTemperature(series=Ref(collection='swmm:timeseries', key='Air')))
    recipient = Ref(collection='swmm:subcatchments', key='Recipient')
    removal = (None if mode == 'none' else h.SnowRemoval(threshold=.25, out_of_system=.1, to_impervious=.1,
        to_pervious=.1, immediate_melt=.1, to_subcatchment=.1, destination=recipient))
    snow = snowpack(initial=3, removal=removal)
    changes = {}
    for bit, name in enumerate(('plowable', 'impervious', 'pervious')):
        value = getattr(snow, name)
        changes[name] = None if missing & (1 << bit) else replace(value, base_temperature=base, initial_free_water=.8)
    model.snowpacks.add(replace(snow, **changes))
    model.subcatchments.update('S', snowpack=SNOW)
    model.subcatchments.add(replace(model.subcatchments['S'], id='Recipient', snowpack=None if mode == 'inactive' else SNOW))
    for name, values in (('Soil', (.5, 2)), ('Storage', (2, .5)), ('Roughness', (3, 1))):
        model.patterns.add(Pattern(id=name, kind='MONTHLY', factors=values))
    model.subcatchment_adjustments.add(c.SubcatchmentAdjustments(subcatchment=Ref(collection='swmm:subcatchments', key='S'),
        infiltration=Ref(collection='swmm:patterns', key='Soil'), depression_storage=Ref(collection='swmm:patterns', key='Storage'),
        pervious_roughness=Ref(collection='swmm:patterns', key='Roughness')))
    return model.to_document().text


def queries(model):
    result = []
    def walk(owner, value, path=()):
        if is_dataclass(value):
            for f in fields(value):
                p = path + (f.name,)
                result.append(model.inspect_field(owner, p))
                result.append(model.field_provenance(owner, p))
                walk(owner, getattr(value, f.name), p)
    for name in ('snowpacks', 'subcatchment_adjustments'):
        for key, row in getattr(model, name).items():
            walk(Ref(collection='swmm:' + name, key=key), row)
    return tuple(result)


class SnowFieldTests(unittest.TestCase):
    def test_all_snow_surfaces_units_defaults_and_water_capacity(self):
        for units in UNITS:
            for missing in (0, 1, 2, 4, 7):
                model = load(fixture(units, missing))
                for name in ('plowable', 'impervious', 'pervious'):
                    info = model.inspect_field(SNOW, name)
                    self.assertEqual(info.semantics.default.value.base_temperature, UnitContext().convert(0, dimension='temperature', to=UnitContext(flow_units=units)))
                    if getattr(model.snowpacks['Snow'], name) is None:
                        self.assertEqual(info.provenance.status, 'omitted')
                        self.assertEqual(info.semantics.effective.value, info.semantics.default.value)
                    else:
                        water = model.inspect_field(SNOW, (name, 'initial_free_water'))
                        self.assertEqual(water.value, .8)
                        self.assertAlmostEqual(water.semantics.effective.value, .3)
                        self.assertEqual(water.semantics.unit.value, 'in' if units in UNITS[:3] else 'mm')
                        self.assertEqual(model.inspect_field(SNOW, (name, 'minimum_melt')).semantics.unit.value,
                                         'in/hour/F' if units in UNITS[:3] else 'mm/hour/C')
                before = queries(model)
                for info in before[::2]:
                    self.assertIn(info.semantics.effective.status, ('known', 'not_applicable'), info.path)
                    self.assertNotEqual(info.semantics.unit.status, 'unknown', info.path)
                self.assertEqual(queries(Model.from_json_document(model.to_json_document(), strict=True)), before)

    def test_removal_defaults_short_syntax_and_zero_transfer(self):
        model = load(fixture(mode='none'))
        info = model.inspect_field(SNOW, 'removal')
        self.assertIsNone(info.semantics.default.value)
        self.assertIsNone(info.semantics.effective.value)
        source = fixture(mode='none') + '[SNOWPACKS]\nSnow REMOVAL .2 .1 .2 .3 .1\n'
        model = load(source)
        info = model.inspect_field(SNOW, ('removal', 'to_subcatchment'))
        self.assertEqual(info.provenance.status, 'omitted')
        self.assertIsNone(info.value)
        self.assertEqual(info.semantics.default.value, 0)
        self.assertEqual(info.semantics.effective.value, 0)
        self.assertFalse(model.validate(for_run=True).is_valid)
        self.assertTrue(model.validate(for_run=True, normalize=True).is_valid)
        self.assertEqual(model.inspect_field(SNOW, ('removal', 'destination')).semantics.effective.status, 'not_applicable')
        model = load(fixture())
        model.snowpacks.update('Snow', removal=replace(model.snowpacks['Snow'].removal, to_subcatchment=0))
        self.assertEqual(model.inspect_field(SNOW, ('removal', 'destination', 'key')).semantics.effective.status, 'not_applicable')

    def test_inactive_invalid_missing_and_extension_transfer_destinations(self):
        model = load(fixture(mode='inactive'))
        for path in (('removal', 'destination'), ('removal', 'destination', 'key'), ('removal', 'to_subcatchment')):
            self.assertEqual(model.inspect_field(SNOW, path).semantics.effective.status, 'not_applicable')
        model = load(fixture()); row = model.snowpacks['Snow']
        model.snowpacks.update('Snow', removal=replace(row.removal, destination=None))
        self.assertEqual(model.inspect_field(SNOW, 'removal').semantics.effective.status, 'invalid')
        model.snowpacks.update('Snow', removal=replace(row.removal, destination=Ref(collection='swmm:subcatchments', key='missing'), to_subcatchment=0))
        self.assertEqual(model.inspect_field(SNOW, 'removal').semantics.effective.status, 'invalid')
        model.snowpacks.update('Snow', removal=row.removal)
        model.subcatchments.update('Recipient', impervious_percent=100)
        self.assertEqual(model.inspect_field(SNOW, ('removal', 'destination')).semantics.effective.status, 'not_applicable')
        model.subcatchments.update('Recipient', impervious_percent=30, snowpack=Ref(collection='swmm:snowpacks', key='missing'))
        self.assertEqual(model.inspect_field(SNOW, ('removal', 'destination')).semantics.effective.status, 'invalid')

    def test_physical_constraints_and_extension_surfaces_do_not_acquire_builtin_facts(self):
        model = load(fixture()); snow = model.snowpacks['Snow']
        for value in (replace(snow.pervious, minimum_melt=-1), replace(snow.pervious, minimum_melt=1),
                      replace(snow.pervious, full_cover_depth=-1), replace(snow.pervious, initial_snow=-1)):
            model.snowpacks.update('Snow', pervious=value)
            self.assertEqual(model.inspect_field(SNOW, 'pervious').semantics.effective.status, 'invalid')
        model.snowpacks.update('Snow', pervious=snow.pervious)
        for changes in (dict(threshold=-1), dict(out_of_system=-1), dict(out_of_system=.605)):
            model.snowpacks.update('Snow', removal=replace(snow.removal, **changes))
            self.assertEqual(model.inspect_field(SNOW, 'removal').semantics.effective.status, 'invalid')
        @dataclass(frozen=True, kw_only=True)
        class ExtendedSurface(h.DepletableSnow): pass
        @dataclass(frozen=True, kw_only=True)
        class ExtendedRemoval(h.SnowRemoval): pass
        model.snowpacks.update('Snow', pervious=ExtendedSurface(**{f.name: getattr(snow.pervious, f.name) for f in fields(snow.pervious)}),
                              removal=ExtendedRemoval(**{f.name: getattr(snow.removal, f.name) for f in fields(snow.removal)}))
        self.assertEqual(model.inspect_field(SNOW, 'pervious').semantics.effective.status, 'unknown')
        self.assertEqual(model.inspect_field(SNOW, 'removal').semantics.effective.status, 'unknown')
        @dataclass(frozen=True, kw_only=True)
        class ExtendedPack(h.Snowpack): pass
        model.snowpacks.add(ExtendedPack(id='Receiving', plowable=snow.plowable, impervious=snow.impervious, pervious=snow.pervious))
        model.subcatchments.update('Recipient', snowpack=Ref(collection='swmm:snowpacks', key='Receiving'))
        model.snowpacks.update('Snow', removal=snow.removal)
        self.assertEqual(model.inspect_field(SNOW, ('removal', 'destination')).semantics.effective.status, 'unknown')

    def test_repeated_groups_last_assignments_and_source_positions(self):
        source = fixture() + '[SNOWPACKS]\nsnow PERVIOUS .001 .004 31 .2 4 .9 5\nSnow REMOVAL .3 .1 .2 .3 .1\n'
        model = load(source)
        self.assertEqual([d.contributes for d in model.field_provenance(SNOW, ('pervious', 'base_temperature')).declarations], [False, True])
        self.assertEqual(model.field_provenance(SNOW, ('pervious', 'base_temperature')).declarations[-1].tokens[0].raw, '31')
        info = model.field_provenance(SNOW, ('removal', 'to_subcatchment'))
        self.assertIsNone(info.value.value)
        self.assertTrue(all(not d.contributes for d in info.declarations))
        self.assertEqual(model.field_provenance(SNOW, ('removal', 'destination')).value.value, None)
        self.assertEqual(model.to_document().text, source)
        source += '[ADJUSTMENTS]\nINFIL s Storage\n'
        model = load(source)
        self.assertEqual(model.inspect_field(ADJUST, 'infiltration').semantics.effective.value.key, 'Storage')
        self.assertEqual([d.contributes for d in model.field_provenance(ADJUST, ('infiltration', 'key')).declarations], [False, True])
        self.assertEqual(model.field_provenance(ADJUST, ('subcatchment', 'key')).declarations[0].tokens[0].raw, 'S')
        self.assertEqual(sum(d.contributes for d in model.field_provenance(ADJUST, ('subcatchment', 'key')).declarations), 1)
        self.assertEqual(queries(Model.from_json_document(model.to_json_document(), strict=True)), queries(model))

    def test_local_assignments_defaults_pattern_kind_and_missing_targets(self):
        model = load(fixture()); model.subcatchment_adjustments.update('S', infiltration=None, depression_storage=None)
        model = load(model.to_document().text)
        for name in ('infiltration', 'depression_storage'):
            info = model.inspect_field(ADJUST, name)
            self.assertEqual(info.provenance.status, 'omitted')
            self.assertIsNone(info.semantics.default.value)
            self.assertIsNone(info.semantics.effective.value)
        model.patterns.update('Roughness', kind='DAILY')
        self.assertEqual(model.inspect_field(ADJUST, 'pervious_roughness').semantics.effective.status, 'invalid')
        model.subcatchment_adjustments.update('S', pervious_roughness=Ref(collection='swmm:patterns', key='missing'))
        self.assertEqual(model.inspect_field(ADJUST, ('pervious_roughness', 'key')).semantics.effective.status, 'invalid')
        @dataclass(frozen=True, kw_only=True)
        class ExtendedPattern(Pattern): pass
        model = load(fixture())
        model.patterns.replace('Soil', ExtendedPattern(id='Soil', kind='MONTHLY', factors=(1.,)))
        self.assertEqual(model.inspect_field(ADJUST, 'infiltration').semantics.effective.status, 'unknown')

    def test_identity_rollback_json_portable_and_units(self):
        model = load(fixture()); before = queries(model)
        with self.assertRaises(RuntimeError):
            with model.transaction():
                model.subcatchments.rename('S', 'Renamed'); model.patterns.rename('Soil', 'P'); raise RuntimeError
        self.assertEqual(queries(model), before)
        source = model.field_provenance(ADJUST, ('infiltration', 'key'))
        model.patterns.rename('Soil', 'Permeability')
        self.assertEqual(model.field_provenance(ADJUST, ('infiltration', 'key')), source)
        model.subcatchments.rename('S', 'Catchment')
        owner = Ref(collection='swmm:subcatchment_adjustments', key='Catchment')
        self.assertEqual(model.field_provenance(owner, ('subcatchment', 'key')).value.value, 'S')
        self.assertTrue(model.inspect_field(owner, ('subcatchment', 'key')).changed)
        model.convert_units('CMS')
        self.assertEqual(model.inspect_field(SNOW, ('plowable', 'base_temperature')).semantics.unit.value, 'C')
        self.assertEqual(queries(Model.from_json_document(model.to_json_document(), strict=True)), queries(model))
        self.assertEqual(portable(model).field_provenance(SNOW, 'plowable').status, 'untracked')

    def test_bad_groups_remain_unclaimed(self):
        for suffix, name in (('[SNOWPACKS]\nSnow PERVIOUS invalid\n', 'snowpacks'),
                             ('[ADJUSTMENTS]\nINFIL S Missing TooMany\n', 'subcatchment_adjustments')):
            model = Model.from_document(InpDocument.from_text(fixture() + suffix))
            self.assertNotIn('Snow' if name == 'snowpacks' else 'S', getattr(model, name))
            self.assertFalse(model.validate().is_valid)


if __name__ == '__main__':
    unittest.main()
