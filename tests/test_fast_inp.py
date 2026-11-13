"""Trusted INP loading preserves parameters without implicit validation."""

from dataclasses import fields, is_dataclass, replace
from datetime import timedelta
from enum import Enum
from pathlib import Path
import codecs
import struct
import tempfile
import unittest
from unittest.mock import patch

from easysewer import Model
from easysewer.io.inp import InpDocument
from easysewer.io.inp._fast_read import _values
from easysewer.io.inp.lexer import tokenize
from easysewer.model import FileReference, Ref
from easysewer.model._trusted import active
from easysewer.model.identity import require_immutable
from easysewer.validation import ValidationError

NETWORK = '[OPTIONS]\nFLOW_UNITS CMS\nFLOW_ROUTING DYNWAVE\nLINK_OFFSETS ELEVATION\nSTART_DATE 01/01/2020\nEND_DATE 01/01/2020\nEND_TIME 01:00:00\nTHREADS 4\n[JUNCTIONS]\nJ 10 5 0 0 0\n[OUTFALLS]\nO 9 FREE NO\n[CONDUITS]\nP J O 100.12345678901235 0.013 * 0 0 0\n[XSECTIONS]\nP CIRCULAR 1 0 0 0 1\n[LOSSES]\nP 0 0 0 NO 0\n[COORDINATES]\nJ 1.2345678901234567 2\nO 3 4\n[VERTICES]\nP 2 3\n[REPORT]\nSUBCATCHMENTS NONE\nNODES NONE\nLINKS NONE\n'

RESOURCES = '[OPTIONS]\nSTART_DATE 01/01/2020\nSTART_TIME 12:00\nTHREADS 4\n[CURVES]\nC SHAPE 0 0 0.5 1\nC 1 0\n[PATTERNS]\nDry HOURLY 1 1 1 1 1 1 1 1 1 1 1 1\nDry 1 1 1 1 1 1 1 1 1 1 1 1\n[TIMESERIES]\nA 0 -5 00:01 -4\nB 01/03/2020 0:00 10\nA 01/01/2020 12:05 -3 12:10 -2\n[TIMESERIES]\nb 1:00 11\na 12:15 -1\n'

CONTROLS = '[OPTIONS]\nFLOW_UNITS CMS\nFLOW_ROUTING DYNWAVE\nLINK_OFFSETS ELEVATION\nSTART_DATE 01/01/2020\nEND_DATE 01/01/2020\nEND_TIME 01:00:00\nTHREADS 4\n[JUNCTIONS]\nJ 10 5 0 0 0\n[OUTFALLS]\nO 9 FREE NO\n[CONDUITS]\nP J O 100.12345678901235 0.013 * 0 0 0\n[XSECTIONS]\nP CIRCULAR 1 0 0 0 1\n[LOSSES]\nP 0 0 0 NO 0\n[COORDINATES]\nJ 1.2345678901234567 2\nO 3 4\n[VERTICES]\nP 2 3\n[REPORT]\nSUBCATCHMENTS NONE\nNODES NONE\nLINKS NONE\n[CONTROLS]\nVARIABLE DepthValue = NODE J DEPTH\nVARIABLE RateValue = LINK P FLOW\nEXPRESSION AverageRate = (RateValue + RateValue) / 2\nRULE first\nIF DepthValue > 1\nOR NODE J DEPTH > NODE O DEPTH\nAND AverageRate >= 0.1\nTHEN CONDUIT P STATUS = CLOSED\nELSE CONDUIT P STATUS = OPEN\nPRIORITY 2\nRULE second\nIF SIMULATION TIME >= 00:00:20\nTHEN CONDUIT P STATUS = OPEN\n'

STORAGE = '[OPTIONS]\nTHREADS 4\n[STORAGE]\nS1 10 5 1 FUNCTIONAL 2 1.7 50\n[JUNCTIONS]\nJ 9 5\n[STORAGE]\nS2 10 5 1 TABULAR Area\n[CURVES]\nArea STORAGE 0 50\nArea 2 70\nArea 5 100\n'

HYDROLOGY = '[OPTIONS]\nFLOW_UNITS CMS\nFLOW_ROUTING DYNWAVE\nINFILTRATION CURVE_NUMBER\nLINK_OFFSETS ELEVATION\nSTART_DATE 01/01/2020\nEND_DATE 01/01/2020\nEND_TIME 01:00:00\nTHREADS 4\n[JUNCTIONS]\nJ 10 5 0 0 0\n[OUTFALLS]\nO 9 FREE NO\n[CONDUITS]\nP J O 100.12345678901235 0.013 * 0 0 0\n[XSECTIONS]\nP CIRCULAR 1 0 0 0 1\n[LOSSES]\nP 0 0 0 NO 0\n[COORDINATES]\nJ 1.2345678901234567 2\nO 3 4\n[VERTICES]\nP 2 3\n[REPORT]\nSUBCATCHMENTS NONE\nNODES NONE\nLINKS NONE\n[RAINGAGES]\nRG INTENSITY 0:05 1 TIMESERIES Rain\n[SUBCATCHMENTS]\nS RG J 1 10 100 0.5 0\n[SUBAREAS]\nS .01 .1 0 0 25 OUTLET\n[INFILTRATION]\nS 76 0 7\n[TIMESERIES]\nRain 0:00 0\nRain 0:05 5\n[POLYGONS]\nS 0 0\nS 1 1\n'

def typed(value):
    """Include floating-point bits, value types and all collection order."""
    if type(value) is float:
        return (float, struct.pack('>d', value))
    if isinstance(value, Enum):
        return (type(value), value.name, typed(value.value))
    if is_dataclass(value):
        return (type(value), tuple((field.name, typed(getattr(value, field.name)))
                                  for field in fields(value)))
    if isinstance(value, dict):
        return (type(value), tuple((typed(key), typed(item)) for key, item in value.items()))
    if isinstance(value, (tuple, list)):
        return (type(value), tuple(map(typed, value)))
    if isinstance(value, (set, frozenset)):
        return (type(value), frozenset(map(typed, value)))
    return (type(value), value)


def parameters(model):
    return typed((model._store._records, model.profile))


class FastInpTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)

    def source(self, text=NETWORK, *, name='input.inp', encoding='utf-8'):
        path = self.directory / name
        path.write_bytes(text.encode(encoding))
        return path

    def test_complete_parameters_match_checked_reader_and_export_reread(self):
        for name, text in (('network', NETWORK), ('resources', RESOURCES),
                           ('controls', CONTROLS), ('storage', STORAGE),
                           ('hydrology', HYDROLOGY)):
            with self.subTest(name=name):
                source = self.source(text, name=name + '.inp')
                expected = Model.from_document(InpDocument.read(source), strict=True)
                actual = Model.from_inp(source)
                self.assertEqual(parameters(actual), parameters(expected))
                before = parameters(actual)
                output = self.directory / (name + '-out.inp')
                actual.to_inp(output)
                self.assertEqual(parameters(actual), before)
                self.assertEqual(parameters(Model.from_inp(output)), before)
                self.assertEqual(parameters(Model.from_document(InpDocument.read(output))), before)

    def assert_parameter_roundtrip(self, text, name):
        source = self.source(text, name=name + '.inp')
        checked = Model.from_document(InpDocument.read(source), strict=True)
        fast = Model.from_inp(source)
        expected = parameters(checked)
        self.assertEqual(parameters(fast), expected)
        output = self.directory / (name + '-out.inp')
        fast.to_inp(output)
        self.assertEqual(parameters(fast), expected)
        self.assertEqual(parameters(Model.from_inp(output)), expected)
        self.assertEqual(parameters(Model.from_document(InpDocument.read(output), strict=True)), expected)
        return fast

    def test_lid_groundwater_rdii_quality_and_treatment_fast_roundtrip(self):
        from test_lid_v2 import lid_model
        from test_groundwater_v2 import groundwater_model
        from test_rdii_v2 import rdii_model
        from test_treatment_v2 import treatment_model
        # A few complete domain fixtures exercise the trusted branches without
        # repeating the exhaustive variant matrices in their dedicated suites.
        cases = (
            ('lid-pavement', lid_model('PP'), 'swmm:lid_controls'),
            ('lid-drain-mat', lid_model('GR'), 'swmm:lid_usage'),
            ('groundwater', groundwater_model(), 'swmm:gwf'),
            ('rdii', rdii_model(), 'swmm:hydrographs'),
            ('quality-treatment', treatment_model(), 'swmm:treatment'),
        )
        for name, fixture, collection in cases:
            with self.subTest(name=name):
                parsed = self.assert_parameter_roundtrip(fixture.to_document().text, name)
                self.assertTrue(parsed.collection(collection))
        self.assertTrue(parsed.pollutants)
        self.assertTrue(parsed.buildup)
        self.assertTrue(parsed.washoff)

    def test_climate_surface_transect_and_inflow_fast_roundtrip(self):
        from test_surface_v2 import INLETS, transect
        from test_inflows_v2 import inflow_model, add_series
        from easysewer.model.geometry import CrossSection, Irregular
        from easysewer.model.inflows import FlowInflow, DryWeatherFlow
        from easysewer.model.resources import Pattern
        climate = (
            '[TEMPERATURE]\nFILE "weather data.dat" * C10\nTIMESERIES Air\n'
            'WINDSPEED MONTHLY 1 2 3 4 5 6 7 8 9 10 11 12\n'
            'SNOWMELT -2 .5 .6 100 35 -15\n'
            'ADC IMPERVIOUS 0 .1 .2 .3 .4 .5 .6 .7 .8 .9\n'
            'ADC PERVIOUS 1 1 1 1 1 1 1 1 1 1\n'
            '[TIMESERIES]\nAir 0:00 -8\nAir 12:00 3\n'
            '[EVAPORATION]\nMONTHLY .1 .2 .3 .4 .5 .6 .7 .8 .9 1 1.1 1.2\nDRY_ONLY YES\n'
            '[ADJUSTMENTS]\nTEMPERATURE 1 2 3 4 5 6 7 8 9 10 11 12\n'
        )
        surface = NETWORK.replace('P CIRCULAR 1 0 0 0 1', 'P STREET Road')
        surface += '[STREETS]\nRoad 10 .5 2 .016 .05 1 1 3 4 .03\n'
        surface += INLETS + '[INLET_USAGE]\nP G J 2 20 3 .03 1 ON_SAG\n'
        channel = Model.from_document(InpDocument.from_text(NETWORK), strict=True)
        channel.transects.add(transect())
        channel.links.update('P', section=CrossSection(geometry=Irregular(
            transect=Ref(collection='swmm:transects', key='T'))))
        inflow = inflow_model()
        series = add_series(inflow)
        inflow.patterns.add(Pattern(id='M', kind='MONTHLY', factors=(2,)))
        monthly = Ref(collection='swmm:patterns', key='M')
        node = Ref(collection='swmm:nodes', key='J')
        inflow.inflows.add(FlowInflow(node=node, series=series, baseline=.1, pattern=monthly))
        inflow.dwf.add(DryWeatherFlow(node=node, baseline=.2, patterns=(None, monthly)))
        cases = (
            ('climate', climate, 'swmm:climate'),
            ('surface', surface, 'swmm:inlet_usage'),
            ('transect', channel.to_document().text, 'swmm:transects'),
            ('inflow', inflow.to_document().text, 'swmm:inflows'),
        )
        for name, text, collection in cases:
            with self.subTest(name=name):
                parsed = self.assert_parameter_roundtrip(text, name)
                self.assertTrue(parsed.collection(collection))

    def test_regulator_link_families_fast_roundtrip(self):
        from test_regulators_v2 import regulator_model
        for kind in ('PUMP3', 'SIDE', 'TRAPEZOIDAL', 'TABULAR/HEAD'):
            with self.subTest(kind=kind):
                fixture = regulator_model(kind)
                name = 'regulator-' + kind.lower().replace('/', '-')
                parsed = self.assert_parameter_roundtrip(fixture.to_document().text, name)
                self.assertEqual(type(parsed.links['P']), type(fixture.links['P']))

    def test_default_load_and_export_build_no_rich_document_or_validation(self):
        source = self.source()
        with patch.object(Model, 'validate', side_effect=AssertionError('Unexpected model validation')):
            with patch.object(InpDocument, 'read', side_effect=AssertionError('Unexpected lossless reader')):
                model = Model.from_inp(source)
            with patch.object(InpDocument, 'from_text', side_effect=AssertionError('Unexpected document reparse')):
                model.to_inp(self.directory / 'out.inp')
        self.assertIsNone(model.document)
        self.assertIsNone(model.support)
        self.assertEqual(model.source_path, str(source.resolve()))
        self.assertEqual(model.provenance(Ref(collection='swmm:nodes', key='J')).kind, 'untracked')
        self.assertFalse(active())

    def test_explicit_document_and_strict_loading_keep_source_contract(self):
        text = NETWORK + '; retained source comment\n'
        path = self.source(text)
        checked = Model.from_inp(path, strict=True)
        self.assertEqual(checked.document.text, text)
        self.assertEqual(checked.to_document().text, text)
        permissive = Model.from_document(InpDocument.from_text(text))
        self.assertIsNotNone(permissive.document)
        self.assertEqual(permissive.to_document().text, text)
        generated = Model.from_inp(path).to_document()
        self.assertIsNone(generated.source)
        self.assertNotIn('; retained source comment', generated.text)

    def test_unchecked_load_export_and_explicit_validation_are_separate(self):
        path = self.source(NETWORK.replace('J 10 5 0 0 0', 'J 10 -1 0 0 0'))
        model = Model.from_inp(path)
        self.assertEqual(model.nodes['J'].max_depth, -1)
        output = self.directory / 'negative.inp'
        model.to_inp(output)
        self.assertEqual(Model.from_inp(output).nodes['J'].max_depth, -1)
        self.assertFalse(model.validate().is_valid)
        with self.assertRaises(ValidationError):
            Model.from_inp(path, strict=True)
        with self.assertRaises(ValidationError):
            model.to_inp(self.directory / 'checked.inp', strict=True)
        self.assertFalse((self.directory / 'checked.inp').exists())

    def test_conversion_failure_restores_public_checks(self):
        path = self.source(NETWORK.replace('J 10 5', 'J not-a-number 5'))
        with self.assertRaises(ValueError):
            Model.from_inp(path)
        self.assertFalse(active())
        with self.assertRaises(ValueError):
            FileReference(path='')
        with self.assertRaises(TypeError):
            require_immutable([1])
        self.assertEqual(len(Model.from_inp(self.source()).nodes), 2)

    def test_float_precision_zero_and_omission_survive_edits(self):
        model = Model.from_inp(self.source())
        model.nodes.update('J', elevation=11.234567890123457)
        model.links.update('P', length=0.10000000000000002)
        output = self.directory / 'edited.inp'
        model.to_inp(output)
        self.assertEqual(parameters(Model.from_inp(output)), parameters(model))
        self.assertEqual(list(model.nodes), ['J', 'O'])
        self.assertEqual(model.links['P'].outlet_offset, 0)
        self.assertNotEqual(model.links['P'].inlet_offset, 0)

    def test_precision_limit_fails_without_silently_truncating_time(self):
        for duration in ('0.0000001', '0.00000000001'):
            with self.subTest(duration=duration):
                with self.assertRaisesRegex(ValueError, 'resolution'):
                    Model.from_inp(self.source(NETWORK + '[OPTIONS]\nROUTING_STEP ' + duration + '\n'))
        model = Model.from_inp(self.source(NETWORK + '[OPTIONS]\nROUTING_STEP 0.000001\n'))
        self.assertEqual(model.options.routing_step, timedelta(microseconds=1))
        model.to_inp(self.directory / 'microsecond.inp')
        self.assertEqual(Model.from_inp(self.directory / 'microsecond.inp').options.routing_step,
                         timedelta(microseconds=1))

    def test_integer_conversion_never_silently_truncates_barrel_count(self):
        for count in ('1.9', '-1.9', '1e-1'):
            with self.subTest(count=count):
                text = NETWORK.replace('P CIRCULAR 1 0 0 0 1',
                                       'P CIRCULAR 1 0 0 0 ' + count)
                with self.assertRaisesRegex(ValueError, 'Expected an integer'):
                    Model.from_inp(self.source(text))
                self.assertFalse(active())
        # Exact conversion remains independent of the later legality checks.
        for count in ('2.0', '2e0', '-2'):
            with self.subTest(count=count):
                text = NETWORK.replace('P CIRCULAR 1 0 0 0 1',
                                       'P CIRCULAR 1 0 0 0 ' + count)
                model = Model.from_inp(self.source(text))
                self.assertEqual(model.links['P'].section.barrels, int(float(count)))

    def test_quotes_backslashes_and_non_ascii_whitespace_match_lexer(self):
        for raw in ('A\u00a0B 1', '"space name"\tC:\\data\\file.dat 1', 'A\t\t1  2'):
            expected = tuple(token.value for token in tokenize(raw, offset=0, line=1,
                             source=None, section=None)[0])
            self.assertEqual(_values(raw), expected)
        source = self.source(NETWORK.replace('\nJ ', '\nJ\u00a0A ').replace('P J O', 'P J\u00a0A O'))
        self.assertEqual(parameters(Model.from_inp(source)),
                         parameters(Model.from_document(InpDocument.read(source))))

    def test_encoding_bom_and_explicit_legacy_encoding(self):
        text = NETWORK.replace('\nJ ', '\nJé ').replace('P J O', 'P Jé O')
        for encoding in ('utf-8-sig', 'cp1252'):
            with self.subTest(encoding=encoding):
                source = self.source(text, name=encoding + '.inp', encoding=encoding)
                model = Model.from_inp(source, encoding=encoding)
                output = self.directory / (encoding + '-out.inp')
                model.to_inp(output)
                self.assertEqual(parameters(Model.from_inp(output, encoding=encoding)), parameters(model))
                if encoding == 'utf-8-sig':
                    self.assertTrue(output.read_bytes().startswith(codecs.BOM_UTF8))

    def test_declared_file_rebasing_preserves_target_and_original_model(self):
        source = self.source(NETWORK + '[TIMESERIES]\nRain FILE "rain data.dat"\n')
        model = Model.from_inp(source)
        before = parameters(model)
        expected = model.timeseries['Rain'].file.resolve()
        output = self.directory / 'elsewhere' / 'out.inp'
        model.to_inp(output)
        loaded = Model.from_inp(output)
        self.assertEqual(loaded.timeseries['Rain'].file.resolve(), expected)
        self.assertEqual(parameters(model), before)
        self.assertEqual(model.source_path, str(source.resolve()))

    def test_opaque_rows_survive_and_guard_unknown_reference_edits(self):
        text = NETWORK + '[FILES]\nUSE RUNOFF previous.bin\nNO RUNOFF unused.bin\n[FUTURE]\nfirst J\nsecond P\n'
        model = Model.from_inp(self.source(text))
        model.nodes.update('J', elevation=12.0)
        output = self.directory / 'opaque.inp'
        model.to_inp(output)
        rendered = output.read_text()
        self.assertIn('NO RUNOFF unused.bin', rendered)
        self.assertLess(rendered.index('first J'), rendered.index('second P'))
        self.assertEqual(parameters(Model.from_inp(output)), parameters(model))
        with self.assertRaises(ValidationError):
            model.nodes.rename('J', 'Moved')
        with self.assertRaises(ValidationError):
            model.nodes.move('J')
        with self.assertRaises(NotImplementedError):
            model.to_inp(self.directory / 'other' / 'opaque.inp')
        model.to_inp(self.directory / 'other' / 'preserved.inp', path_policy='preserve')

    def test_unrepresentable_token_leaves_existing_export_untouched(self):
        model = Model.from_inp(self.source(NETWORK + '[TIMESERIES]\nRain FILE "rain data.dat"\n'))
        original = model.timeseries['Rain']
        model.timeseries.replace('Rain', replace(original, file=replace(original.file, path='unrepresentable;name.dat')))
        output = self.directory / 'existing.inp'
        output.write_bytes(b'unchanged')
        with self.assertRaisesRegex(ValueError, 'cannot represent'):
            model.to_inp(output)
        self.assertEqual(output.read_bytes(), b'unchanged')
        self.assertFalse(active())

    def test_missing_writer_fails_before_replacing_existing_export(self):
        from easysewer.model.network import Junction
        from easysewer.schema import EPA_SWMM_5_2_4
        from easysewer.schema.registry import RegistryError
        profile = replace(EPA_SWMM_5_2_4, key='test:unsupported')
        model = Model.from_inp(self.source(''), profile=profile)
        model.nodes.add(Junction(id='J', elevation=1.0))
        output = self.directory / 'existing.inp'
        output.write_bytes(b'unchanged')
        with self.assertRaisesRegex(RegistryError, 'no active INP writer'):
            model.to_inp(output)
        self.assertEqual(output.read_bytes(), b'unchanged')
        self.assertFalse(active())

    def test_custom_codec_uses_document_compatibility_path(self):
        from easysewer.io.inp.network import default_schema
        from easysewer.schema import FeatureDescriptor
        from test_model_extensions import SensorCodec
        schema = default_schema()
        schema.register(FeatureDescriptor(key='test:sensors', sections={'SENSORS'}), SensorCodec())
        source = self.source(NETWORK + '[SENSORS]\nS J 1.5\n')
        model = Model.from_inp(source, schema=schema)
        self.assertIsNotNone(model.document)
        self.assertEqual(model.collection('test:sensors')['S'].threshold, 1.5)
        output = self.directory / 'custom.inp'
        model.to_inp(output)
        reread = Model.from_inp(output, schema=schema)
        self.assertEqual(parameters(reread), parameters(model))


if __name__ == '__main__':
    unittest.main()
