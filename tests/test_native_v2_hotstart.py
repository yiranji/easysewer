"""Real SWMM-produced state, independent byte oracles, and native reuse."""

from ctypes import byref
from dataclasses import replace
from datetime import date, time, timedelta
import hashlib
from pathlib import Path
import struct
import tempfile
import unittest

from easysewer import get_native_capabilities
from easysewer.io.hotstart import HotstartData, HotstartLayout, StatePollutant, LegacyGroundwaterState
from easysewer.io.hotstart_manifest import HotstartManifest
from easysewer.io.inp import InpDocument
from easysewer.io.interface_inspection import inspect_interface
from easysewer.model import Model, Ref
from easysewer.runtime import check_files
from test_files_v2 import bind
from test_hydrology_v2 import hydrology_model, snowpack, INFILTRATION
from test_native_v2_files import selected
import test_native_v2_project as native_project
from test_options_v2 import network


@unittest.skipUnless(get_native_capabilities()['swmm_solver'] and get_native_capabilities()['swmm_output'], 'Native solver/output unavailable')
class NativeHotstartTests(unittest.TestCase):
    def solve(self, root, name, source):
        return native_project.NativeProjectTests.solve(self, root, name, source, report_encoding='cp1252')

    def quality(self, root, name, *, subcatchments=1):
        from easysewer.runtime._output_api import SWMMOutputAPI
        reader = SWMMOutputAPI()
        try:
            reader.open(str(root/(name+'.out'))); periods = reader.get_times(1)
            return tuple(tuple(tuple(tuple(method(i, a, 0, periods)) for a in range(first, first+2)) for i in range(count))
                for method, count, first in ((reader.get_subcatch_series, subcatchments, 8), (reader.get_node_series, 2, 6), (reader.get_link_series, 1, 5)))
        finally:
            self.assertEqual(reader.lib.SMO_close(byref(reader.handle)), 0)

    def test_five_infiltration_methods_two_unit_systems_exact_state_and_reuse(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for method in INFILTRATION:
                for units in ('CFS', 'CMS'):
                    with self.subTest(method=method, units=units):
                        model = selected(hydrology_model(method)); model.reinterpret_units(units)
                        model.update_options(end_date=date(2020, 1, 30), end_time=time(6), routing_step=timedelta(seconds=60))
                        source = model.to_document().text; state = root/'source.hsf'; rewritten = root/'rewrite.hsf'
                        produced = self.solve(root, 'producer', source+f'[FILES]\nSAVE HOTSTART "{state}"\n')
                        self.assertGreater(max(produced['series'][0][0][4]), 0)
                        layout = HotstartLayout.from_model(model)
                        raw = state.read_bytes(); document = HotstartData.from_bytes(raw, layout=layout)
                        self.assertEqual(tuple(n.id for n in layout.nodes), produced['ids'][1])
                        self.assertEqual(document.to_bytes(), raw)
                        # One catchment, one storage node, one outfall and link.
                        self.assertEqual(len(raw), 15+24+80+4*(3+2+3))
                        self.assertEqual(document.subcatchments[0].ponded_depths, struct.unpack_from('<3d', raw, 39))
                        self.assertEqual(document.subcatchments[0].infiltration.values, struct.unpack_from('<6d', raw, 39+32))
                        document.write(rewritten)
                        expected = self.solve(root, 'literal', source+f'[FILES]\nUSE HOTSTART "{state}"\n')
                        actual = self.solve(root, 'rewritten', source+f'[FILES]\nUSE HOTSTART "{rewritten}"\n')
                        self.assertEqual(expected, actual)
                        bind(model, 'HOTSTART', 'USE', rewritten)
                        manifest = HotstartManifest.asserted(raw, layout=layout, producer_input_sha256=hashlib.sha256((root/'producer.inp').read_bytes()).hexdigest())
                        self.assertTrue(check_files(model, interface_manifests={'HOTSTART': manifest}).complete)

    def test_all_optional_states_two_pollutants_landuses_groundwater_and_snow(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = selected(hydrology_model())
            model.update_options(end_date=date(2020, 1, 30), end_time=time(6), routing_step=timedelta(seconds=60))
            model.snowpacks.add(snowpack(initial=3))
            model.subcatchments.update('S', snowpack=Ref(collection='swmm:snowpacks', key='Snow'))
            layout = HotstartLayout.from_model(model)
            layout = replace(layout, subcatchments=(replace(layout.subcatchments[0], groundwater='Aquifer'),),
                             pollutants=(StatePollutant(id='Q0', units='MG/L'), StatePollutant(id='Q1', units='UG/L')), landuses=('Land0', 'Land1'))
            source = model.to_document().text + ('[AQUIFERS]\nAquifer .45 .1 .25 .2 10 15 .5 5 0 -10 2 .3\n'
                '[GROUNDWATER]\nS Aquifer J 20 .001 1 0 1 0 0 *\n'
                '[POLLUTANTS]\nQ0 MG/L 2 4 0 0 NO * 0 0 0\nQ1 UG/L 3 5 0 0 NO * 0 0 0\n'
                '[LANDUSES]\nLand0 0 0 0\nLand1 0 0 0\n[COVERAGES]\nS Land0 50 Land1 50\n'
                '[BUILDUP]\nLand0 Q0 POW 10 1 1 AREA\nLand0 Q1 POW 15 2 1 AREA\n'
                'Land1 Q0 POW 20 3 1 AREA\nLand1 Q1 POW 25 4 1 AREA\n'
                '[WASHOFF]\nLand0 Q0 EXP 1 1 0 0\nLand0 Q1 EXP 1 1 0 0\n'
                'Land1 Q0 EXP 1 1 0 0\nLand1 Q1 EXP 1 1 0 0\n[LOADINGS]\nS Q0 2 Q1 3\n')
            state = root/'full.hsf'; rewritten = root/'full-copy.hsf'
            self.solve(root, 'producer', source+f'[FILES]\nSAVE HOTSTART "{state}"\n')
            raw = state.read_bytes(); document = HotstartData.from_bytes(raw, layout=layout)
            self.assertEqual(len(raw), 15+24+8*(10+4+15+4+6)+4*(5+4+5))
            self.assertEqual(document.to_bytes(), raw)
            catchment = document.subcatchments[0]
            self.assertGreater(catchment.groundwater.moisture, .1)
            self.assertGreater(catchment.groundwater.flow, 0)
            self.assertTrue(any(s.snow_depth > 0 for s in catchment.snow))
            self.assertEqual((catchment.groundwater.moisture, catchment.groundwater.water_table,
                              catchment.groundwater.flow, catchment.groundwater.maximum_infiltration_volume), struct.unpack_from('<4d', raw, 39+80))
            # np=2, two land uses: check all buildup and sweep slots independently.
            for i, landuse in enumerate(catchment.landuses):
                self.assertEqual((*landuse.buildup, landuse.last_swept), struct.unpack_from('<3d', raw, 39+8*(10+4+15+4)+i*24))
            self.assertGreater(sum(catchment.runoff_quality), 0)
            document.write(rewritten)
            original = self.solve(root, 'literal', source+f'[FILES]\nUSE HOTSTART "{state}"\n')
            quality_original = self.quality(root, 'literal')
            actual = self.solve(root, 'rewritten', source+f'[FILES]\nUSE HOTSTART "{rewritten}"\n')
            self.assertEqual(actual, original)
            self.assertEqual(self.quality(root, 'rewritten'), quality_original)
            self.assertTrue(any(v > 0 for obj in quality_original[1] for pollutant in obj for v in pollutant))
            owned = Model.from_document(InpDocument.from_text(source), strict=True)
            manifest = HotstartManifest.asserted(raw, layout=layout)
            checked = inspect_interface(raw, 'HOTSTART', owned, manifest=manifest)
            self.assertEqual(checked.status, 'validated')
            self.assertTrue(checked.report.is_valid)
            # Legacy formats still consume quality and their backward-compatible
            # slots, while omitting newer runoff and storage-residence fields.
            for version in (1, 2, 3):
                nodes = tuple(replace(n, residence_time=None, legacy_quality=(0., 0.) if version <= 2 else ()) for n in document.nodes)
                legacy = (LegacyGroundwaterState(id='S', moisture=catchment.groundwater.moisture,
                           water_table=catchment.groundwater.water_table),) if version == 2 else ()
                converted = replace(document, version=version, nodes=nodes,
                                    subcatchments=document.subcatchments if version == 3 else (), legacy_groundwater=legacy)
                converted.write(rewritten)
                result = self.solve(root, 'legacy', source+f'[FILES]\nUSE HOTSTART "{rewritten}"\n')
                self.assertGreater(result['periods'], 0)
                self.assertEqual(HotstartData.read(rewritten, layout=layout), converted)

    def test_legacy_versions_match_independent_headers_and_native_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); state = root/'state.hsf'; current = root/'current.hsf'
            model = selected(network()); source = model.to_document().text
            self.solve(root, 'producer', source+f'[FILES]\nSAVE HOTSTART "{state}"\n')
            raw = state.read_bytes(); layout = HotstartLayout.from_model(model)
            document = HotstartData.from_bytes(raw, layout=layout)
            baseline = self.solve(root, 'baseline', source+f'[FILES]\nUSE HOTSTART "{state}"\n')
            for version, counts in ((1, (2, 1, 0, 0)), (2, (0, 2, 1, 0, 0)), (3, (0, 0, 2, 1, 0, 0)), (4, (0, 0, 2, 1, 0, 0))):
                converted = replace(document, version=version)
                literal = b'SWMM5-HOTSTART'+(str(version).encode() if version != 1 else b'')+struct.pack('<'+'i'*len(counts), *counts)+raw[39:]
                self.assertEqual(converted.to_bytes(), literal)
                converted.write(current)
                self.assertEqual(self.solve(root, 'reuse', source+f'[FILES]\nUSE HOTSTART "{current}"\n'), baseline)

    def test_edit_changes_native_history_and_matches_independent_binary_patch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); state = root/'state.hsf'; edited = root/'edit.hsf'; oracle = root/'oracle.hsf'
            model = selected(network()); source = model.to_document().text
            self.solve(root, 'producer', source+f'[FILES]\nSAVE HOTSTART "{state}"\n')
            raw = state.read_bytes(); layout = HotstartLayout.from_model(model)
            document = HotstartData.from_bytes(raw, layout=layout)
            replace(document, nodes=(replace(document.nodes[0], depth=2.), document.nodes[1])).write(edited)
            independent = bytearray(raw); struct.pack_into('<f', independent, 39, 2.); oracle.write_bytes(independent)
            self.assertEqual(edited.read_bytes(), oracle.read_bytes())
            original = self.solve(root, 'original', source+f'[FILES]\nUSE HOTSTART "{state}"\n')
            changed = self.solve(root, 'changed', source+f'[FILES]\nUSE HOTSTART "{edited}"\n')
            self.assertNotEqual(original['series'], changed['series'])
            self.assertEqual(changed, self.solve(root, 'oracle', source+f'[FILES]\nUSE HOTSTART "{oracle}"\n'))
            manifest = HotstartManifest.asserted(raw, layout=layout)
            self.assertFalse(inspect_interface(edited.read_bytes(), 'HOTSTART', model, manifest=manifest).report.is_valid)
            self.assertEqual(state.read_bytes(), raw)

    def test_repeated_sections_and_subtype_order_use_actual_native_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); state = root/'state.hsf'
            model = selected(hydrology_model()); model.update_options(routing_step=timedelta(seconds=60))
            document = model.to_document(); source = document.text
            storage = next(s for s in document.sections if s.name == 'STORAGE')
            block = storage.header.raw + ''.join(line.raw for line in storage.lines)
            source = source.replace(block, '') + block
            parsed = Model.from_document(InpDocument.from_text(source), strict=True)
            produced = self.solve(root, 'producer', source+f'[FILES]\nSAVE HOTSTART "{state}"\n')
            layout = HotstartLayout.from_model(parsed)
            self.assertEqual(tuple(n.id for n in layout.nodes), ('O', 'J'))
            self.assertEqual(tuple(n.id for n in layout.nodes), produced['ids'][1])
            raw = state.read_bytes(); state_data = HotstartData.from_bytes(raw, layout=layout)
            self.assertIsNone(state_data.nodes[0].residence_time)
            self.assertIsNotNone(state_data.nodes[1].residence_time)
            self.assertEqual(state_data.to_bytes(), raw)
            bind(parsed, 'HOTSTART', 'USE', state)
            manifest = HotstartManifest.asserted(raw, layout=layout)
            self.assertTrue(check_files(parsed, interface_manifests={'HOTSTART': manifest}).complete)
            normalized = Model.from_document(parsed.to_document(normalize=True), strict=True)
            if tuple(n.id for n in HotstartLayout.from_model(normalized).nodes) != tuple(n.id for n in layout.nodes):
                self.assertFalse(check_files(normalized, interface_manifests={'HOTSTART': manifest}).report.is_valid)


if __name__ == '__main__':
    unittest.main()
