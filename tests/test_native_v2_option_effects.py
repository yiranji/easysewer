"""Active OPTIONS switches: physical evidence and exact native round trips."""
import hashlib, re, tempfile, unittest
from pathlib import Path

from easysewer import get_native_capabilities
from easysewer.io.output import OutputReader
from easysewer.model import Model, Ref
from test_option_effects_v2 import SWITCHES, UNITS, fixture, infiltration_fixture, load
from test_native_v2_regulator_fields import FAMILIES, library
from test_native_v2_title_report_gates import solve

EVIDENCE = []


def observe(test, lib, root, source):
    result = solve(test, lib, root, source)
    with OutputReader(root/'model.out') as reader:
        result['pollutants'] = reader.metadata.names('swmm:pollutants')
        result['series'] = {v.key: reader.series(None, v.key).values
                            for v in reader.available_variables('swmm:system')}
        if result['pollutants']:
            result['concentration'] = reader.series(Ref(collection='swmm:nodes', key='J'),
                'swmm:concentration', pollutant=Ref(collection='swmm:pollutants', key='Q0')).values
    match = re.search(rb'% of Time in Steady State\s+:\s+(\S+)', result['report'])
    result['steady_percent'] = float(match[1]) if match else None
    return result


def physical_effect(test, field, off, on):
    a, b = off['series'], on['series']
    if field in ('ignore_rainfall', 'ignore_groundwater', 'ignore_rdii', 'ignore_routing'):
        variable = {'ignore_rainfall': 'rainfall', 'ignore_groundwater': 'groundwater_inflow',
                    'ignore_rdii': 'rdii_inflow', 'ignore_routing': 'outflow'}[field]
        test.assertGreater(max(a['swmm:'+variable]), 0)
        test.assertEqual(set(b['swmm:'+variable]), {0.0})
        if field == 'ignore_rainfall': test.assertEqual(set(b['swmm:runoff']), {0.0})
        if field == 'ignore_routing':
            test.assertEqual(a['swmm:runoff'], b['swmm:runoff'])
            test.assertGreater(max(b['swmm:runoff']), 0)
    elif field == 'ignore_snowmelt':
        test.assertGreater(max(a['swmm:snow_depth']), min(a['swmm:snow_depth']))
        test.assertEqual(len(set(b['swmm:snow_depth'])), 1)
        test.assertGreater(min(b['swmm:snow_depth']), 0)
        test.assertGreater(sum(a['swmm:runoff']), sum(b['swmm:runoff']))
    elif field == 'ignore_quality':
        test.assertEqual(off['pollutants'], ('Q0', 'Q1'))
        test.assertGreater(max(off['concentration']), 0)
        test.assertEqual(on['pollutants'], ())
        test.assertEqual(a, b)  # Quality removal must not change hydraulics here.
    elif field == 'allow_ponding':
        test.assertGreater(max(b['swmm:storage']), max(a['swmm:storage']))
        test.assertGreater(sum(a['swmm:flooding']), sum(b['swmm:flooding']))
    elif field == 'skip_steady_state':
        test.assertEqual(off['steady_percent'], 0)
        test.assertGreater(on['steady_percent'], 0)
        test.assertLessEqual(on['steady_percent'], 100)
    else: test.fail('No physical assertion for '+field)


def digest(result):
    return dict(out_sha256=hashlib.sha256(result['out']).hexdigest(),
        report_sha256=hashlib.sha256(result['report']).hexdigest(),
        steady_percent=result['steady_percent'], pollutants=result['pollutants'],
        system={key:dict(min=min(values), max=max(values), sum=sum(values))
                for key,values in result['series'].items()})


@unittest.skipUnless(get_native_capabilities()['swmm_solver'] and get_native_capabilities()['flexible_ponding'],
                     'Both native engines required')
class NativeOptionEffectTests(unittest.TestCase):
    def test_global_infiltration_five_methods_produce_distinct_active_runoff(self):
        methods = ('HORTON', 'MODIFIED_HORTON', 'GREEN_AMPT', 'MODIFIED_GREEN_AMPT', 'CURVE_NUMBER')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for family, name, symbol in FAMILIES:
                lib, _ = library(name, symbol)
                for units in UNITS:
                    results = {}
                    for method in methods:
                        with self.subTest(family=family, units=units, method=method):
                            m = infiltration_fixture(method, units)
                            source = m.to_document().text+'[OPTIONS]\nINFILTRATION '+method+'\n'
                            expected = observe(self, lib, root, source)
                            parsed = load(source)
                            self.assertIsNone(parsed.subcatchments['S'].infiltration.method)
                            self.assertEqual(parsed.options.infiltration, method)
                            restored = Model.from_json_document(parsed.to_json_document(), strict=True)
                            self.assertEqual(observe(self, lib, root, restored.to_document(normalize=True).text), expected)
                            self.assertEqual(observe(self, lib, root, m.to_document().text), expected)
                            results[method] = expected['series']['swmm:runoff']
                            self.assertGreater(max(results[method]), 0)
                            EVIDENCE.append(dict(kind='infiltration-method', family=family, units=units,
                                method=method, result=digest(expected)))
                    self.assertEqual(len(set(results.values())), 5)

    def test_routing_aliases_inertial_modes_and_surcharge_change_actual_hydraulics(self):
        alternatives = (
            ('flow_routing', ('STEADY', 'KINWAVE', 'DYNWAVE')),
            ('inertial_damping', ('NONE', 'PARTIAL', 'FULL')),
            ('surcharge_method', ('EXTRAN', 'SLOT')),
        )
        aliases = {'NF':'STEADY', 'KW':'KINWAVE', 'EKW':'KINWAVE', 'XKINWAVE':'KINWAVE', 'DW':'DYNWAVE'}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for family, name, symbol in FAMILIES:
                lib, _ = library(name, symbol)
                for units in UNITS:
                    for field, values in alternatives:
                        baseline = fixture('allow_ponding', units)
                        source = baseline.to_document().text
                        results = {}
                        for value in values:
                            with self.subTest(family=family, units=units, field=field, value=value):
                                literal = source+'[OPTIONS]\n'+field.upper()+' '+value+'\n'
                                expected = observe(self, lib, root, literal)
                                model = baseline.copy(); model.update_options(**{field:value})
                                restored = Model.from_json_document(model.to_json_document(), strict=True)
                                self.assertEqual(observe(self, lib, root, restored.to_document(normalize=True).text), expected)
                                self.assertEqual(observe(self, lib, root, load(literal).to_document(normalize=True).text), expected)
                                results[value] = expected
                                EVIDENCE.append(dict(kind='hydraulic-choice', family=family, units=units,
                                    field=field, value=value, result=digest(expected)))
                        self.assertEqual(len({r['series']['swmm:outflow'] for r in results.values()}), len(values))
                        if field == 'flow_routing':
                            for alias, canonical in aliases.items():
                                literal = source+'[OPTIONS]\nFLOW_ROUTING '+alias+'\n'
                                self.assertEqual(observe(self, lib, root, literal), results[canonical])
                                self.assertEqual(observe(self, lib, root, load(literal).to_document(normalize=True).text), results[canonical])
                                EVIDENCE.append(dict(kind='routing-alias', family=family, units=units,
                                    alias=alias, canonical=canonical, result=digest(results[canonical])))

    def test_zero_adjustments_and_zero_steady_tolerances_have_distinct_native_effects(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for family, name, symbol in FAMILIES:
                lib, _ = library(name, symbol)
                for units in UNITS:
                    m = fixture('allow_ponding', units); m.update_options(variable_step=.75)
                    length = 1 if units in UNITS[:3] else .3048
                    for field, keyword, effective in (
                        ('max_trials', 'MAX_TRIALS', 8),
                        ('minimum_step', 'MINIMUM_STEP', .001),
                        ('head_tolerance', 'HEAD_TOLERANCE', .005*length),
                        ('min_surface_area', 'MIN_SURFAREA', 12.566*length**2)):
                        with self.subTest(family=family, units=units, field=field):
                            source = m.to_document().text
                            zero = observe(self, lib, root, source+'[OPTIONS]\n'+keyword+' 0\n')
                            adjusted = observe(self, lib, root, source+'[OPTIONS]\n'+keyword+' '+format(effective, '.17g')+'\n')
                            self.assertEqual(zero, adjusted)
                            parsed = load(source+'[OPTIONS]\n'+keyword+' 0\n')
                            actual = getattr(parsed.effective_options.values, field)
                            if field == 'minimum_step': actual = actual.total_seconds()
                            self.assertAlmostEqual(actual, effective, places=12)
                            restored = Model.from_json_document(parsed.to_json_document(), strict=True)
                            self.assertEqual(observe(self, lib, root, restored.to_document(normalize=True).text), zero)
                            EVIDENCE.append(dict(kind='zero-adjustment', family=family, units=units,
                                field=field, effective=effective, result=digest(zero)))
                    for field in ('sys_flow_tol', 'lat_flow_tol'):
                        with self.subTest(family=family, units=units, field=field):
                            m = fixture('skip_steady_state', units)
                            m.update_options(skip_steady_state=True)
                            if field == 'lat_flow_tol':
                                from dataclasses import replace
                                points = m.timeseries['Q'].points
                                m.timeseries.update('Q', points=(*points[:-1], replace(points[-1], value=points[-1].value*1.05)))
                            source = m.to_document().text
                            default = observe(self, lib, root, source)
                            self.assertGreater(default['steady_percent'], 0)
                            for value in (0, -1):
                                text = source+'[OPTIONS]\n'+field.upper()+' '+str(value)+'\n'
                                result = observe(self, lib, root, text)
                                self.assertEqual(result['steady_percent'], 0)
                                parsed = load(text)
                                self.assertEqual(getattr(parsed.effective_options.values, field), value)
                                self.assertNotIn(field, parsed.effective_options.defaults_used)
                                restored = Model.from_json_document(parsed.to_json_document(), strict=True)
                                self.assertEqual(observe(self, lib, root, restored.to_document(normalize=True).text), result)
                                EVIDENCE.append(dict(kind='steady-tolerance', family=family, units=units,
                                    field=field, value=value, default_percent=default['steady_percent'], result=digest(result)))

    def test_ignore_rainfall_disables_rdii_and_routing_none_obeys_assignment_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for family, name, symbol in FAMILIES:
                lib, _ = library(name, symbol)
                for units in UNITS:
                    source = fixture('ignore_rdii', units).to_document().text
                    rain = observe(self, lib, root, source)
                    self.assertGreater(max(rain['series']['swmm:rdii_inflow']), 0)
                    tail = '[OPTIONS]\nIGNORE_RDII NO\nIGNORE_RAINFALL YES\n'
                    stopped = observe(self, lib, root, source+tail)
                    self.assertEqual(set(stopped['series']['swmm:rdii_inflow']), {0.0})
                    self.assertEqual(set(stopped['series']['swmm:rainfall']), {0.0})
                    m = load(source+tail)
                    self.assertFalse(m.options.ignore_rdii)
                    self.assertTrue(m.options.ignore_rainfall)
                    self.assertEqual(observe(self, lib, root, m.to_document(normalize=True).text), stopped)
                    EVIDENCE.append(dict(kind='rainfall-rdii', family=family, units=units, result=digest(stopped)))
                    source = fixture('ignore_routing', units).to_document().text
                    for lines, ignored in (
                        ('FLOW_ROUTING NONE\nIGNORE_ROUTING NO\n', False),
                        ('IGNORE_ROUTING NO\nFLOW_ROUTING NONE\n', True),
                        ('IGNORE_ROUTING YES\nFLOW_ROUTING DYNWAVE\n', True),
                        ('FLOW_ROUTING NONE\nFLOW_ROUTING DYNWAVE\nIGNORE_ROUTING NO\n', False)):
                        with self.subTest(family=family, units=units, lines=lines):
                            expected = observe(self, lib, root, source+'[OPTIONS]\n'+lines)
                            m = load(source+'[OPTIONS]\n'+lines)
                            self.assertEqual(m.options.ignore_routing, ignored)
                            self.assertEqual(observe(self, lib, root, m.to_document(normalize=True).text), expected)
                            values = expected['series']['swmm:outflow']
                            self.assertEqual(max(values) == 0, ignored)
                            self.assertGreater(max(expected['series']['swmm:runoff']), 0)
                            EVIDENCE.append(dict(kind='routing-assignment-order', family=family, units=units,
                                lines=lines, ignored=ignored, result=digest(expected)))

    def test_eight_active_switches_six_units_both_engines_and_complete_lifecycle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for family, name, symbol in FAMILIES:
                lib, path = library(name, symbol)
                for field in SWITCHES:
                    for units in UNITS:
                        with self.subTest(family=family, field=field, units=units):
                            m = fixture(field, units)
                            source = m.to_document().text
                            keyword = field.upper()
                            off = observe(self, lib, root, source+'[OPTIONS]\n'+keyword+' NO\n')
                            on = observe(self, lib, root, source+'[OPTIONS]\n'+keyword+' YES\n')
                            physical_effect(self, field, off, on)
                            for enabled, expected in ((True, on), (False, off)):
                                m.update_options(**{field: enabled})
                                self.assertEqual(observe(self, lib, root, m.to_document().text), expected)
                                restored = Model.from_json_document(m.to_json_document(), strict=True)
                                self.assertEqual(observe(self, lib, root, restored.to_document(normalize=True).text), expected)
                            repeated = source+'[OPTIONS]\n'+keyword+' YES\n[OPTIONS]\n'+keyword+' NO\n'
                            self.assertEqual(observe(self, lib, root, repeated), off)
                            self.assertEqual(observe(self, lib, root, load(repeated).to_document(normalize=True).text), off)
                            m.update_options(**{field: None})
                            self.assertEqual(observe(self, lib, root, m.to_document().text), off)
                            EVIDENCE.append(dict(kind='active-switch', family=family, field=field, units=units,
                                off=digest(off), on=digest(on), physical_assertion=True,
                                library_sha256=hashlib.sha256(Path(path).read_bytes()).hexdigest()))

    def test_compatibility_and_slope_weighting_are_inert_in_active_models(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for family, name, symbol in FAMILIES:
                lib, _ = library(name, symbol)
                for units in UNITS:
                    for domain in ('ignore_quality', 'allow_ponding'):
                        m = fixture(domain, units)
                        source = m.to_document().text
                        expected = observe(self, lib, root, source)
                        for field, keyword, token, value in (
                            ('compatibility', 'COMPATIBILITY', '3', 3),
                            ('compatibility', 'COMPATIBILITY', '4', 4),
                            ('compatibility', 'COMPATIBILITY', '5', 5),
                            ('slope_weighting', 'SLOPE_WEIGHTING', 'YES', True),
                            ('slope_weighting', 'SLOPE_WEIGHTING', 'NO', False)):
                            with self.subTest(family=family, units=units, domain=domain, field=field, value=value):
                                literal = source+'[OPTIONS]\n'+keyword+' '+token+'\n'
                                self.assertEqual(observe(self, lib, root, literal), expected)
                                m.update_options(**{field: value})
                                restored = Model.from_json_document(m.to_json_document(), strict=True)
                                self.assertEqual(observe(self, lib, root, restored.to_document(normalize=True).text), expected)
                                m.update_options(**{field: None})
                                self.assertEqual(observe(self, lib, root, m.to_document().text), expected)
                                EVIDENCE.append(dict(kind='deprecated-option', family=family, units=units,
                                    domain=domain, field=field, value=value, result=digest(expected)))


if __name__ == '__main__': unittest.main()
