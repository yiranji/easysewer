"""Rain-gage/catchment field semantics and the exact grouped source grammar."""
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import time, timedelta
from pathlib import Path
import unittest
from unittest.mock import patch

from easysewer.io.inp import InpDocument
from easysewer.model import Model, Ref, Point
from easysewer.model import hydrology as h, network as n
from easysewer.model.resources import SeriesPoint
from test_hydrology_v2 import hydrology_model, INFILTRATION
from test_scenario_v2 import portable

GAGE = Ref(collection='swmm:raingages', key='R')
CATCHMENT = Ref(collection='swmm:subcatchments', key='S')
UNITS = ('CFS', 'GPM', 'MGD', 'CMS', 'LPS', 'MLD')


def load(text, **kwargs):
    return Model.from_document(InpDocument.from_text(text, source='D:/models/hydrology.inp'), strict=True, **kwargs)


def fixture(method='HORTON', units='CFS', routing='PERVIOUS', form='INTENSITY', impervious=30):
    model = hydrology_model(method, form=form)
    model.reinterpret_units(units)
    model.update_options(end_date=model.options.start_date, end_time=time(0, 20), report_step=timedelta(minutes=1),
                         routing_step=timedelta(seconds=5), wet_step=timedelta(seconds=30))
    model.raingages.update('R', interval=timedelta(minutes=1), position=Point(x=1, y=2))
    model.timeseries.update('Rain', points=tuple(SeriesPoint(time=timedelta(minutes=i), value=2 if 1 <= i < 10 else 0) for i in range(21)))
    row = model.subcatchments['S']
    parameters = row.infiltration.parameters
    if type(parameters) is h.Horton:
        parameters = replace(parameters, maximum_volume=None, drying_time=0)
    elif type(parameters) is h.CurveNumber:
        parameters = replace(parameters, curve_number={'OUTLET': 5, 'IMPERVIOUS': 120, 'PERVIOUS': 75}[routing])
    model.subcatchments.update('S', impervious_percent=impervious, subareas=replace(row.subareas, route_to=routing, routed_percent=None),
                              infiltration=replace(row.infiltration, method=None, parameters=parameters), polygon=(Point(x=0, y=0), Point(x=1, y=2)))
    model.update_options(infiltration=method)
    return model.to_document().text + '[MAP]\nUNITS METERS\n'


def queries(model):
    result = []
    def walk(owner, value, path=()):
        if is_dataclass(value):
            for f in fields(value):
                p = path + (f.name,)
                result.append(model.inspect_field(owner, p))
                result.append(model.field_provenance(owner, p))
                walk(owner, getattr(value, f.name), p)
        elif isinstance(value, tuple):
            for i, v in enumerate(value):
                walk(owner, v, path + (i,))
    for name in ('raingages', 'subcatchments'):
        for row in getattr(model, name).values():
            walk(Ref(collection='swmm:' + name, key=row.id), row)
    return tuple(result)


class HydrologyFieldTests(unittest.TestCase):
    def test_five_infiltration_methods_six_units_all_nested_contracts(self):
        for method in INFILTRATION:
            for units in UNITS:
                with self.subTest(method=method, units=units):
                    model = load(fixture(method, units))
                    before = queries(model)
                    for info in before[::2]:
                        self.assertIn(info.semantics.effective.status, ('known', 'not_applicable'), info.path)
                        self.assertNotEqual(info.semantics.unit.status, 'unknown', info.path)
                        if all(type(v) is str for v in info.path):
                            self.assertIn(info.provenance.status, ('explicit', 'omitted', 'derived'), info.path)
                    self.assertEqual(queries(Model.from_json_document(model.to_json_document(), strict=True)), before)
                    info = model.inspect_field(CATCHMENT, ('infiltration', 'method'))
                    self.assertEqual(info.semantics.default.value, method)
                    self.assertEqual(info.semantics.effective.value, method)
                    self.assertIsNone(info.value)
                    self.assertEqual(info.provenance.status, 'omitted')
                    self.assertEqual(model.inspect_field(CATCHMENT, 'area').semantics.unit.value, 'acre' if units in UNITS[:3] else 'ha')
                    self.assertEqual(model.inspect_field(CATCHMENT, 'curb_length').semantics.unit.value, 'user length')
                    self.assertEqual(model.inspect_field(GAGE, ('position', 'x')).semantics.unit.value, 'm')
                    self.assertEqual(model.inspect_field(GAGE, 'interval').semantics.unit.value, 's')

    def test_native_clamps_and_horton_input_defaults(self):
        model = load(fixture())
        info = model.inspect_field(CATCHMENT, ('infiltration', 'parameters', 'maximum_volume'))
        self.assertIsNone(info.value); self.assertEqual(info.provenance.status, 'omitted')
        self.assertEqual(info.semantics.default.value, 0)
        self.assertEqual(info.semantics.effective.value, 0)
        self.assertEqual(model.inspect_field(CATCHMENT, ('infiltration', 'parameters', 'drying_time')).semantics.effective.value, 1e-6)
        for value in (-2, 10, 75, 99, 120):
            model = load(fixture('CURVE_NUMBER'))
            model.subcatchments.update('S', infiltration=h.Infiltration(parameters=h.CurveNumber(curve_number=value, drying_time=2)))
            self.assertEqual(model.inspect_field(CATCHMENT, ('infiltration', 'parameters', 'curve_number')).semantics.effective.value, min(99, max(10, value)))
        model = load(fixture(impervious=120))
        self.assertEqual(model.inspect_field(CATCHMENT, 'impervious_percent').semantics.effective.value, 100)
        self.assertEqual(model.inspect_field(CATCHMENT, 'impervious_percent').value, 120)

    def test_subarea_routing_applicability_and_percent_default(self):
        for routing in ('OUTLET', 'IMPERVIOUS', 'PERVIOUS'):
            for impervious in (0, 30, 100, 120):
                model = load(fixture(routing=routing, impervious=impervious))
                expected = routing if impervious == 30 else 'OUTLET'
                self.assertEqual(model.inspect_field(CATCHMENT, ('subareas', 'route_to')).semantics.effective.value, expected)
                info = model.inspect_field(CATCHMENT, ('subareas', 'routed_percent'))
                self.assertEqual(info.semantics.default.value, 100)
                self.assertEqual(info.semantics.effective.status, 'known' if expected != 'OUTLET' else 'not_applicable')
                self.assertIsNone(info.value)
                row = model.subcatchments['S']
                model.subcatchments.update('S', subareas=replace(row.subareas, routed_percent=0))
                if expected != 'OUTLET':
                    self.assertEqual(model.inspect_field(CATCHMENT, ('subareas', 'routed_percent')).semantics.effective.value, 0)

    def test_replacement_history_clears_optional_contributions_and_keeps_ignored_tokens(self):
        text = fixture().replace('S 3 0.2 4 0', 'S 3 0.2 4 0 5 HORTON')
        text += '[INFILTRATION]\nS 3 .2 4 2\n[SUBAREAS]\nS .01 .2 .05 .1 25 PERVIOUS 30\nS .02 .3 .1 .2 15 OUTLET\n'
        text += '[SYMBOLS]\nR 3 4 tail\n'
        model = load(text)
        info = model.field_provenance(CATCHMENT, ('infiltration', 'parameters', 'maximum_volume'))
        self.assertIsNone(info.value.value)
        self.assertEqual(len(info.declarations), 1)
        self.assertFalse(info.declarations[0].contributes)
        self.assertFalse(model.field_provenance(CATCHMENT, ('infiltration', 'method')).declarations[0].contributes)
        info = model.field_provenance(CATCHMENT, ('subareas', 'routed_percent'))
        self.assertIsNone(info.value.value)
        self.assertTrue(all(not d.contributes for d in info.declarations))
        self.assertEqual([d.contributes for d in model.field_provenance(GAGE, ('position', 'x')).declarations], [False, True])
        self.assertEqual(model.field_provenance(GAGE, 'position').declarations[-1].role, 'retained')
        model = load(fixture('CURVE_NUMBER').replace('S 75 0 2', 'S 75 999 2'))
        declarations = model.field_provenance(CATCHMENT, ('infiltration', 'parameters')).declarations
        self.assertEqual([t.raw for t in declarations[0].tokens], ['75', '2'])
        self.assertEqual([t.raw for t in declarations[1].tokens], ['999'])
        self.assertFalse(declarations[1].contributes)
        self.assertEqual(queries(Model.from_json_document(model.to_json_document(), strict=True)), queries(model))

    def test_rain_file_and_series_sources_intervals_stars_and_no_io(self):
        for tail, seconds in (('TIMESERIES Rain', 2), ('FILE missing.dat Station MM', 1), ('FILE missing.dat Station MM *', 1)):
            model = load(f'[RAINGAGES]\nR VOLUME .0005 1 {tail}\n[TIMESERIES]\nRain 0 0\n')
            info = model.inspect_field(GAGE, 'interval')
            self.assertEqual(info.value, timedelta(seconds=seconds))
            self.assertEqual(info.provenance.declarations[0].tokens[0].raw, '.0005')
            if tail.startswith('FILE'):
                with patch.object(Path, 'open', side_effect=AssertionError('No file I/O')):
                    self.assertEqual(info.semantics.effective.status, 'unknown')
                    self.assertEqual(model.inspect_field(GAGE, 'form').semantics.effective.status, 'unknown')
                    self.assertEqual(model.inspect_field(GAGE, 'source').semantics.effective.value, model.raingages['R'].source)
                    self.assertEqual(model.field_provenance(GAGE, ('source', 'file', 'flavor')).status, 'derived')
                    self.assertEqual(model.field_provenance(GAGE, ('source', 'start_date')).status, 'explicit' if tail.endswith('*') else 'omitted')
                    self.assertEqual(model.inspect_field(GAGE, ('source', 'units')).semantics.effective.value, 'MM')
            else:
                self.assertEqual(info.semantics.effective.value, timedelta(seconds=seconds))

    def test_missing_incompatible_and_ambiguous_contexts_are_not_known(self):
        model = load(fixture())
        model.update_options(infiltration='GREEN_AMPT')
        self.assertEqual(model.inspect_field(CATCHMENT, 'infiltration').semantics.effective.status, 'invalid')
        model.update_options(infiltration='HORTON')
        model.subcatchments.update('S', infiltration=None, subareas=None)
        for name in ('infiltration', 'subareas'):
            self.assertEqual(model.inspect_field(CATCHMENT, name).semantics.effective.status, 'invalid')
        model.subcatchments.update('S', area=0)
        for name in ('infiltration', 'subareas'):
            self.assertEqual(model.inspect_field(CATCHMENT, name).semantics.effective.status, 'not_applicable')
            self.assertEqual(model.inspect_field(CATCHMENT, name).semantics.default.status, 'not_applicable')
        model = load(fixture())
        row = model.subcatchments['S']
        model.subcatchments.add(replace(row, id='J'))
        self.assertEqual(model.inspect_field(CATCHMENT, 'outlet').semantics.effective.status, 'ambiguous')
        self.assertEqual(model.inspect_field(CATCHMENT, ('outlet', 'key')).semantics.effective.status, 'ambiguous')
        model.subcatchments.update('S', rain_gage=Ref(collection='swmm:raingages', key='Missing'))
        self.assertEqual(model.inspect_field(CATCHMENT, 'rain_gage').semantics.effective.status, 'invalid')
        model.raingages.update('R', source=h.SeriesRainfall(series=Ref(collection='swmm:timeseries', key='Missing')))
        self.assertEqual(model.inspect_field(GAGE, 'source').semantics.effective.status, 'invalid')

    def test_extension_parameters_sources_and_subareas_require_explicit_contracts(self):
        @dataclass(frozen=True, kw_only=True)
        class CustomHorton(h.Horton): pass
        @dataclass(frozen=True, kw_only=True)
        class CustomRainfall(h.SeriesRainfall): pass
        @dataclass(frozen=True, kw_only=True)
        class CustomSubareas(h.Subareas): pass
        model = load(fixture()); row = model.subcatchments['S']
        model.subcatchments.update('S', infiltration=h.Infiltration(parameters=CustomHorton(maximum_rate=3, minimum_rate=.2, decay=4, drying_time=2)),
                                  subareas=CustomSubareas(**{f.name: getattr(row.subareas, f.name) for f in fields(row.subareas)}))
        for name in ('infiltration', 'subareas'):
            self.assertEqual(model.inspect_field(CATCHMENT, name).semantics.effective.status, 'unknown')
        model.raingages.update('R', source=CustomRainfall(series=Ref(collection='swmm:timeseries', key='Rain')))
        self.assertEqual(model.inspect_field(GAGE, 'source').semantics.effective.status, 'unknown')

    def test_source_identity_unit_conversion_and_whole_group_rejection(self):
        model = load(fixture()); before = queries(model)
        with self.assertRaises(RuntimeError):
            with model.transaction():
                model.raingages.rename('R', 'Renamed'); raise RuntimeError
        self.assertEqual(queries(model), before)
        old = model.field_provenance(CATCHMENT, ('rain_gage', 'key'))
        model.raingages.rename('R', 'Renamed')
        self.assertEqual(model.field_provenance(CATCHMENT, ('rain_gage', 'key')), old)
        self.assertTrue(model.inspect_field(CATCHMENT, ('rain_gage', 'key')).changed)
        model.convert_units('CMS')
        self.assertEqual(model.inspect_field(CATCHMENT, ('infiltration', 'parameters', 'maximum_rate')).semantics.unit.value, 'mm/h')
        self.assertEqual(queries(Model.from_json_document(model.to_json_document(), strict=True)), queries(model))
        self.assertEqual(portable(model).field_provenance(CATCHMENT, 'area').status, 'untracked')
        for extra in ('[SUBAREAS]\nS bad .2 .05 .1 25 OUTLET\n', '[SUBCATCHMENTS]\nS R J 2 30 100 1 12\n'):
            invalid = Model.from_document(InpDocument.from_text(fixture() + extra))
            self.assertNotIn('S', invalid.subcatchments)
            self.assertFalse(invalid.validate().is_valid)


if __name__ == '__main__':
    unittest.main()
