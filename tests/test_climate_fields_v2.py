"""Climate field contracts and original assignment history."""
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import date, time
from pathlib import Path
import unittest

from easysewer.io.inp import InpDocument
from easysewer.model import Model, Ref
from easysewer.model import climate as c
from easysewer.model.resources import Pattern
from easysewer.model.values import FileReference
from test_snow_fields_v2 import fixture as snow_fixture, UNITS
from test_climate_v2 import add_series

OWNER = Ref(collection='swmm:climate', key='settings')


def load(source):
    return Model.from_document(InpDocument.from_text(source, source=str(Path.cwd()/'climate-fields.inp')), strict=True)


def fixture(units='CFS', evaporation='monthly', temperature='series', weather=None, file_units=None):
    model = load(snow_fixture(units))
    model.update_options(start_date=date(2020, 1, 31), start_time=time(23, 50),
        end_date=date(2020, 2, 1), end_time=time(0, 10), report_start_date=date(2020, 1, 31), report_start_time=time(23, 50))
    rate = .2 if units in UNITS[:3] else 5.08
    series = (add_series(model, 'Evap', ((0, rate), (1, rate * 2))) if evaporation == 'series'
              else Ref(collection='swmm:timeseries', key='Evap'))
    sources = dict(omitted=None, constant=c.ConstantEvaporation(rate=rate),
        monthly=c.MonthlyEvaporation(values=(rate, rate * 2, *(rate,) * 10)),
        series=c.SeriesEvaporation(series=series), file=c.FileEvaporation(), derived=c.TemperatureEvaporation())
    model.patterns.add(Pattern(id='Recovery', kind='MONTHLY', factors=(.8, .6)))
    model.update_climate(temperature={'none': None, 'series': model.climate.temperature, 'file': c.FileTemperature()}[temperature],
        file=c.ClimateFile(file=FileReference(path=str(weather)), units=file_units) if weather else None,
        wind=c.FileWind() if weather else None,
        evaporation=c.Evaporation(source=sources[evaporation], recovery_pattern=Ref(collection='swmm:patterns', key='Recovery')),
        adjustments=c.ClimateAdjustments(temperature=c.MonthlyTemperatureChanges(values=(2., -2., *(0.,) * 10)),
            evaporation=c.MonthlyEvaporation(values=(.01, .02, *(0.,) * 10)),
            rainfall=c.MonthlyFactors(values=(1., .5, *(1.,) * 10)),
            conductivity=c.MonthlyFactors(values=(0., -2., *(1.,) * 10))))
    return model.to_document().text


def queries(model):
    result = []
    def walk(value, path=()):
        if is_dataclass(value):
            for f in fields(value):
                p = path + (f.name,)
                result.extend((model.inspect_field(OWNER, p), model.field_provenance(OWNER, p)))
                walk(getattr(value, f.name), p)
        elif isinstance(value, tuple):
            for i, item in enumerate(value):
                p = path + (i,)
                result.extend((model.inspect_field(OWNER, p), model.field_provenance(OWNER, p)))
    walk(model.climate)
    return tuple(result)


class ClimateFieldTests(unittest.TestCase):
    def test_defaults_and_monthly_items_six_units(self):
        for units in UNITS:
            model = load(fixture(units))
            for info in queries(model)[::2]:
                self.assertEqual(info.semantics.effective.status, 'known', info.path)
                self.assertNotEqual(info.semantics.unit.status, 'unknown', info.path)
            info = model.inspect_field(OWNER, 'temperature')
            self.assertIsInstance(info.semantics.default.value, c.ConstantTemperature)
            self.assertAlmostEqual(info.semantics.default.value.value, 70 if units in UNITS[:3] else (70-32)*5/9)
            self.assertEqual(model.inspect_field(OWNER, ('evaporation', 'dry_only')).semantics.effective.value, False)
            self.assertEqual(model.inspect_field(OWNER, ('adjustments', 'conductivity', 'values', 1)).semantics.effective.value, 1)
            self.assertEqual(model.inspect_field(OWNER, ('adjustments', 'temperature', 'values', 1)).semantics.unit.value,
                             'delta F' if units in UNITS[:3] else 'delta C')
            self.assertEqual(model.inspect_field(OWNER, ('evaporation', 'source', 'values', 0)).semantics.unit.value,
                             'in/day' if units in UNITS[:3] else 'mm/day')
            self.assertEqual(model.inspect_field(OWNER, ('evaporation', 'source', 'values', 0)).provenance.status, 'untracked_path')
            self.assertEqual(model.field_provenance(OWNER, ('evaporation', 'source', 'values', 0)).status, 'explicit')
            self.assertEqual(queries(Model.from_json_document(model.to_json_document(), strict=True)), queries(model))

    def test_all_modes_file_selectors_and_no_io(self):
        for units in UNITS:
            for mode in ('omitted', 'constant', 'monthly', 'series', 'file', 'derived'):
                model = load(fixture(units, mode, 'file', Path.cwd()/'missing weather.dat'))
                for info in queries(model)[::2]:
                    self.assertEqual(info.semantics.effective.status, 'known', (mode, info.path))
                self.assertEqual(model.inspect_field(OWNER, ('file', 'units')).semantics.effective.value, 'F' if units in UNITS[:3] else 'C')
                self.assertIn('GHCND', model.inspect_field(OWNER, ('file', 'units')).semantics.effective.reason)
                self.assertEqual(model.inspect_field(OWNER, ('file', 'start_date')).semantics.effective.value, date(2020, 1, 31))
        for units in ('F', 'C', 'C10'):
            model = load(fixture(weather=Path.cwd()/'absent.dat', file_units=units))
            self.assertEqual(model.inspect_field(OWNER, ('file', 'units')).semantics.effective.value, units)

    def test_assignment_history_independent_file_mode_and_optional_tails(self):
        text = fixture(weather=Path.cwd()/'original.dat', file_units='C10')
        text += '[TEMPERATURE]\nFILE second.dat 01/01/2020 F\nTIMESERIES Air\nFILE last.dat\nTIMESERIES Air\n'
        model = load(text)
        self.assertEqual(model.to_document().text, text)
        history = model.field_provenance(OWNER, 'temperature').declarations
        self.assertEqual(sum(x.contributes for x in history), 1)
        self.assertEqual(history[-1].tokens[0].raw, 'TIMESERIES')
        for name in ('start_date', 'units'):
            info = model.field_provenance(OWNER, ('file', name))
            self.assertIsNone(info.value.value)
            self.assertTrue(info.declarations)
            self.assertTrue(all(not x.contributes for x in info.declarations))
        self.assertEqual(model.field_provenance(OWNER, ('file', 'file', 'path')).declarations[-1].tokens[0].raw, 'last.dat')
        model = load(text + '[TEMPERATURE]\nFILE last.dat * C\n')
        self.assertIsInstance(model.climate.temperature, c.FileTemperature)
        self.assertEqual(model.field_provenance(OWNER, ('file', 'start_date')).declarations[-1].role, 'marker')
        self.assertEqual(queries(Model.from_json_document(model.to_json_document(), strict=True)), queries(model))

    def test_repeated_sources_months_and_bare_file_normalization(self):
        text = fixture(weather=Path.cwd()/'w.dat', temperature='file')
        text += '[EVAPORATION]\nFILE '+' '.join(['.8']*12)+'\nDRY_ONLY YES\nFILE\n'
        model = load(text)
        self.assertFalse(model.validate(for_run=True).is_valid)
        self.assertTrue(model.validate(for_run=True, normalize=True).is_valid)
        fact = model.inspect_field(OWNER, ('evaporation', 'source', 'pan_coefficients'))
        self.assertEqual(fact.semantics.effective.value.values, (1.,)*12)
        self.assertTrue(all(not d.contributes for d in fact.provenance.declarations))
        self.assertTrue(model.inspect_field(OWNER, ('evaporation', 'dry_only')).semantics.effective.value)
        model = load(fixture()+'[ADJUSTMENTS]\nCONDUCTIVITY '+' '.join(['2']*12)+'\n')
        info = model.field_provenance(OWNER, ('adjustments', 'conductivity', 'values', 1))
        self.assertEqual([d.contributes for d in info.declarations], [False, True])
        self.assertEqual(info.declarations[-1].tokens[0].raw, '2')

    def test_physical_references_and_extensions(self):
        for change in (dict(wind=c.MonthlyWindSpeeds(values=(-1.,)*12)),
                       dict(evaporation=c.Evaporation(source=c.ConstantEvaporation(rate=-1))),
                       dict(temperature=c.FileTemperature()),
                       dict(evaporation=c.Evaporation(source=c.FileEvaporation())),
                       dict(temperature=c.SeriesTemperature(series=Ref(collection='swmm:timeseries', key='missing')))):
            model = load(fixture()); model.update_climate(**change)
            self.assertEqual(model.inspect_field(OWNER, next(iter(change))).semantics.effective.status, 'invalid')
        model = load(fixture()); model.patterns.update('Recovery', kind='DAILY')
        self.assertEqual(model.inspect_field(OWNER, 'evaporation').semantics.effective.status, 'invalid')
        @dataclass(frozen=True, kw_only=True)
        class ExtendedMonthly(c.MonthlyFactors): pass
        model = load(fixture()); model.update_climate(adjustments=c.ClimateAdjustments(rainfall=ExtendedMonthly(values=(1.,)*12)))
        self.assertEqual(model.inspect_field(OWNER, 'adjustments').semantics.effective.status, 'unknown')
        @dataclass(frozen=True, kw_only=True)
        class ExtendedPattern(Pattern): pass
        model = load(fixture()); model.patterns.replace('Recovery', ExtendedPattern(id='Recovery', kind='MONTHLY', factors=(1.,)))
        self.assertEqual(model.inspect_field(OWNER, 'evaporation').semantics.effective.status, 'unknown')

    def test_explicit_snow_wind_depletion_units_tokens_and_boundaries(self):
        for units in UNITS:
            model = load(fixture(units))
            model.update_climate(wind=c.MonthlyWindSpeeds(values=tuple(float(i) for i in range(12))),
                snowmelt=c.Snowmelt(snowfall_temperature=0, antecedent_weight=.5, negative_melt_ratio=.6,
                    elevation=100, latitude=35, solar_time_correction=-15),
                impervious_depletion=c.ArealDepletion(fractions=(.5,)*10),
                pervious_depletion=c.ArealDepletion(fractions=(1.,)*10))
            model = load(model.to_document().text)
            for info in queries(model)[::2]:
                self.assertEqual(info.semantics.effective.status, 'known', info.path)
            self.assertEqual(model.inspect_field(OWNER, ('wind', 'values', 11)).semantics.unit.value,
                             'mile/h' if units in UNITS[:3] else 'km/h')
            self.assertEqual(model.inspect_field(OWNER, ('snowmelt', 'snowfall_temperature')).semantics.unit.value,
                             'F' if units in UNITS[:3] else 'C')
            self.assertEqual(model.inspect_field(OWNER, ('snowmelt', 'elevation')).semantics.unit.value,
                             'ft' if units in UNITS[:3] else 'm')
            self.assertEqual(model.inspect_field(OWNER, ('snowmelt', 'solar_time_correction')).semantics.unit.value, 'minute')
            self.assertEqual(float(model.field_provenance(OWNER, ('wind', 'values', 11)).declarations[0].tokens[0].raw), 11)
            self.assertEqual(model.field_provenance(OWNER, ('impervious_depletion', 'fractions', 9)).declarations[0].tokens[0].raw, '0.5')
            self.assertEqual(model.inspect_field(OWNER, ('pervious_depletion', 'fractions', 9)).semantics.unit.value, '1')
        model = load(fixture(weather=Path.cwd()/'weather.dat', temperature='series'))
        model.update_climate(evaporation=c.Evaporation(source=c.TemperatureEvaporation()))
        self.assertEqual(model.inspect_field(OWNER, 'evaporation').semantics.effective.status, 'invalid')
        model.update_climate(temperature=c.FileTemperature(), evaporation=c.Evaporation(
            source=c.FileEvaporation(pan_coefficients=c.MonthlyFactors(values=(-1.,)*12))))
        self.assertEqual(model.inspect_field(OWNER, 'evaporation').semantics.effective.status, 'invalid')

    def test_unit_conversion_rename_and_rollback_preserve_original_sources(self):
        model = load(fixture()); before = queries(model)
        with self.assertRaises(RuntimeError):
            with model.transaction():
                model.timeseries.rename('Air', 'T'); model.patterns.rename('Recovery', 'P'); raise RuntimeError
        self.assertEqual(queries(model), before)
        model.timeseries.rename('Air', 'Temperature')
        self.assertTrue(model.inspect_field(OWNER, ('temperature', 'series', 'key')).changed)
        self.assertEqual(model.field_provenance(OWNER, ('temperature', 'series', 'key')).value.value, 'Air')
        model.convert_units('CMS')
        self.assertAlmostEqual(model.inspect_field(OWNER, ('adjustments', 'temperature', 'values', 0)).semantics.effective.value, 2*5/9)
        self.assertEqual(queries(Model.from_json_document(model.to_json_document(), strict=True)), queries(model))

    def test_malformed_group_unclaimed_and_bad_month_index_rejected(self):
        model = Model.from_document(InpDocument.from_text(fixture()+'[TEMPERATURE]\nSNOWMELT invalid\n'))
        self.assertFalse(model.collection('swmm:climate'))
        model = load(fixture())
        with self.assertRaises(KeyError):
            model.inspect_field(OWNER, ('adjustments', 'rainfall', 'values', 12))


if __name__ == '__main__': unittest.main()
