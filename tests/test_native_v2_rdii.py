"""RDII seasonal semantics and editable runoff caches against SWMM 5.2.4."""

from dataclasses import replace
from datetime import date, time, timedelta
import hashlib
from pathlib import Path
import struct
import tempfile
import unittest

from easysewer import get_native_capabilities
from easysewer.io.cache_manifest import CacheManifest
from easysewer.io.hotstart import StatePollutant
from easysewer.io.inp import InpDocument
from easysewer.io.interface_inspection import inspect_interface
from easysewer.io.runoff_cache import RunoffLayout, RunoffData, RdiiLayout, RdiiData
from easysewer.model import Model, Ref
from easysewer.runtime import check_files
from easysewer.scenario import ScenarioPatch, SetFields, FieldChange
from test_files_v2 import bind
from test_hydrology_v2 import hydrology_model
from test_native_v2_files import selected
import test_native_v2_hotstart as native_hotstart
import test_native_v2_project as native_project
from test_rdii_v2 import rdii_model, response
from test_scenario_v2 import portable


def seasonal():
    model = selected(rdii_model())
    model.update_options(routing_step=timedelta(seconds=60))
    return model


@unittest.skipUnless(get_native_capabilities()['swmm_solver'] and get_native_capabilities()['swmm_output'], 'Native solver/output unavailable')
class NativeRdiiTests(unittest.TestCase):
    def solve(self, root, name, source):
        return native_project.NativeProjectTests.solve(self, root, name, source, report_encoding='cp1252')

    def test_monthly_components_abstraction_legacy_and_source_free_json(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = selected(hydrology_model()); base.update_options(routing_step=timedelta(seconds=60))
            source = base.to_document().text + ('[HYDROGRAPHS]\nUH R\n'
                'UH ALL .15 1 2 .1 2 3 .05 4 4 .1 .2 .025\n'
                'UH JANUARY SHORTER .2 .5 1 .05 .1 .01\n'
                'UH FEBRUARY MEDIUM .18 1.5 2 .08 .15 .03\n[RDII]\nJ UH 2\n')
            model = Model.from_document(InpDocument.from_text(source), strict=True)
            expected = self.solve(root, 'literal', source)
            self.assertEqual(expected, self.solve(root, 'source', model.to_document().text))
            self.assertEqual(expected, self.solve(root, 'json', portable(model).to_document().text))
            self.assertGreater(max(expected['system'][7]), 0)
            changed = portable(model)
            group = changed.hydrographs['UH']
            changed.hydrographs.update('UH', responses=tuple(replace(r, maximum_abstraction=0., recovery_rate=0., initial_abstraction=0.) for r in group.responses))
            self.assertNotEqual(expected['series'][1], self.solve(root, 'no-abstraction', changed.to_document().text)['series'][1])
            # A later ALL row replaces SHORT in both months, leaving MEDIUM/LONG.
            changed = portable(model)
            changed.hydrographs.update('UH', responses=(*group.responses, response(fraction=.05)))
            all_literal = source.replace('[RDII]', 'UH ALL SHORT .05 1 2\n[RDII]')
            actual = self.solve(root, 'all-last', changed.to_document().text)
            self.assertEqual(actual, self.solve(root, 'all-literal', all_literal))
            self.assertNotEqual(expected['series'][1], actual['series'][1])

    def test_scenario_area_and_si_conversion_against_independent_inp(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); model = seasonal()
            baseline = self.solve(root, 'baseline', model.to_document().text)
            patch = ScenarioPatch(flow_units='CFS', operations=(SetFields(target=Ref(collection='swmm:rdii', key='J'), changes=(FieldChange(name='sewer_area', value=4.),)),))
            changed = ScenarioPatch.from_json_document(patch.to_json_document()).apply(model).model
            literal = model.to_document().text.replace('J UH 2', 'J UH 4')
            actual = self.solve(root, 'scenario', portable(changed).to_document().text)
            self.assertEqual(actual, self.solve(root, 'literal', literal))
            self.assertNotEqual(baseline['series'][1], actual['series'][1])
            changed.convert_units('CMS')
            base = selected(hydrology_model()); base.update_options(routing_step=timedelta(seconds=60)); base.convert_units('CMS')
            # All rainfall lengths and IA recovery depths use the documented 25.4,
            # while engine area conversion uses its two native UCF constants.
            expected = base.to_document().text + ('[HYDROGRAPHS]\nUH R\n'
                'UH ALL SHORT .15 1 2 2.54 5.08 .635\nUH ALL MEDIUM .1 2 3\n'
                'UH ALL LONG .05 4 4\nUH FEB SHORT .2 .5 1\n[RDII]\n'
                f'J UH {4*.92903e-5/2.2956e-5:.17g}\n')
            self.assertEqual(self.solve(root, 'si-literal', expected), self.solve(root, 'si-model', portable(changed).to_document().text))

    def test_native_rdii_bytes_identity_reuse_and_independent_flow_edit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); model = seasonal(); source = model.to_document().text
            # Native indices follow source section order, not collection order.
            doc = InpDocument.from_text(source)
            storage = next(s for s in doc.sections if s.name == 'STORAGE')
            block = storage.header.raw + ''.join(line.raw for line in storage.lines)
            source = source.replace(block, '') + block
            model = Model.from_document(InpDocument.from_text(source), strict=True)
            raw_path, copy_path, edit_path, oracle_path = (root/name for name in ('rdii.bin', 'copy.bin', 'edit.bin', 'oracle.bin'))
            self.solve(root, 'producer', source+f'[FILES]\nSAVE RDII "{raw_path}"\n')
            raw = raw_path.read_bytes(); data = RdiiData.from_bytes(raw); layout = RdiiLayout.from_model(model)
            self.assertEqual(layout.nodes, ('O', 'J'))
            data.validate_layout(layout); self.assertEqual(data.to_bytes(), raw)
            self.assertGreater(len(data.frames), 10)
            self.assertEqual(data.node_indices, (layout.nodes.index('J'),))
            self.assertEqual({f.time.month for f in data.frames}, {1, 2})
            data.write(copy_path)
            original = self.solve(root, 'reuse', source+f'[FILES]\nUSE RDII "{raw_path}"\n')
            self.assertEqual(original, self.solve(root, 'copied', source+f'[FILES]\nUSE RDII "{copy_path}"\n'))
            bind(model, 'RDII', 'USE', copy_path)
            manifest = CacheManifest.asserted(raw, layout=layout, producer_input_sha256=hashlib.sha256((root/'producer.inp').read_bytes()).hexdigest())
            self.assertTrue(check_files(model, interface_manifests={'RDII':manifest}).complete)
            edited = replace(data, frames=tuple(replace(f, flows=tuple(v*2 for v in f.flows)) for f in data.frames))
            edited.write(edit_path)
            oracle = bytearray(raw)
            # Independent fixed native header + one-node frame stride.
            for offset in range(10+8+4, len(oracle), 12):
                value, = struct.unpack_from('<f', oracle, offset+8)
                struct.pack_into('<f', oracle, offset+8, value*2)
            oracle_path.write_bytes(oracle); self.assertEqual(edit_path.read_bytes(), oracle)
            actual = self.solve(root, 'edited', source+f'[FILES]\nUSE RDII "{edit_path}"\n')
            self.assertEqual(actual, self.solve(root, 'oracle', source+f'[FILES]\nUSE RDII "{oracle_path}"\n'))
            self.assertNotEqual(original['series'][1], actual['series'][1])

    def test_binary_rdii_flows_keep_internal_cfs_in_si_projects(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); model = seasonal(); documents = []
            for units in ('CFS', 'CMS'):
                model.convert_units(units)
                path = root/(units+'.bin')
                self.solve(root, units, model.to_document().text+f'[FILES]\nSAVE RDII "{path}"\n')
                data = RdiiData.read(path); documents.append(data)
                self.assertEqual(dict(inspect_interface(path.read_bytes(), 'RDII', model).facts)['flow_units'], 'CFS')
            us, si = documents
            self.assertEqual((us.step, us.node_indices), (si.step, si.node_indices))
            self.assertEqual(tuple(f.native_time for f in us.frames), tuple(f.native_time for f in si.frames))
            self.assertGreater(max(f.flows[0] for f in us.frames), .01)
            for first, second in zip(us.frames, si.frames):
                # Unit conversions plus the final float32 save permit one small
                # absolute rounding difference; a CMS/CFS factor would fail.
                self.assertAlmostEqual(first.flows[0], second.flows[0], delta=1e-7)

    def test_overwritten_gage_still_drives_controls_after_normalization(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); model = seasonal()
            model.raingages.add(replace(model.raingages['R'], id='Earlier'))
            source = model.to_document().text.replace('UH R', 'UH Earlier\nUH R') + ('[CONTROLS]\nRULE RainControl\n'
                'IF GAGE Earlier INTENSITY > .1\nTHEN OUTLET P SETTING = .2\nELSE OUTLET P SETTING = 1\n')
            model = Model.from_document(InpDocument.from_text(source), strict=True)
            expected = self.solve(root, 'literal', source)
            self.assertEqual(expected, self.solve(root, 'rebuilt', portable(model).to_document(normalize=True).text))
            model.hydrographs.update('UH', prior_rain_gages=())
            removed = self.solve(root, 'removed', model.to_document().text)
            self.assertNotEqual(expected['series'][1], removed['series'][1])
            self.assertNotEqual(expected['system'], removed['system'])

    def test_zero_rdii_produces_valid_header_only_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); model = seasonal()
            model.hydrographs.update('UH', responses=(response(fraction=0),))
            source = model.to_document().text; cache = root/'dry.bin'; copy = root/'copy.bin'
            produced = self.solve(root, 'producer', source+f'[FILES]\nSAVE RDII "{cache}"\n')
            raw = cache.read_bytes(); self.assertEqual(len(raw), 10+8+4)
            data = RdiiData.from_bytes(raw); self.assertFalse(data.frames); data.write(copy)
            actual = self.solve(root, 'reuse', source+f'[FILES]\nUSE RDII "{copy}"\n')
            self.assertEqual(actual['series'], produced['series'])
            self.assertEqual(actual['system'], produced['system'])
            bind(model, 'RDII', 'USE', copy)
            manifest = CacheManifest.asserted(raw, layout=RdiiLayout.from_model(model))
            self.assertTrue(check_files(model, interface_manifests={'RDII':manifest}).complete)

    def test_native_runoff_all_values_quality_units_and_reuse(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for units in ('CFS', 'CMS'):
                for quality in (False, True):
                    with self.subTest(units=units, quality=quality):
                        model = selected(hydrology_model()); model.convert_units(units)
                        model.update_options(end_date=date(2020, 1, 30), end_time=time(6), routing_step=timedelta(seconds=60))
                        source = model.to_document().text
                        pollutants = ()
                        if quality:
                            source += ('[POLLUTANTS]\nQ0 MG/L 2 4 0 0 NO * 0 0 0\nQ1 UG/L 3 5 0 0 NO * 0 0 0\n'
                                '[AQUIFERS]\nAquifer .45 .1 .25 .2 10 15 .5 5 0 -10 2 .3\n'
                                '[GROUNDWATER]\nS Aquifer J 20 .001 1 0 1 0 0 *\n')
                            pollutants = (StatePollutant(id='Q0', units='MG/L'), StatePollutant(id='Q1', units='UG/L'))
                        layout = replace(RunoffLayout.from_model(model), pollutants=pollutants)
                        cache, copy = root/'runoff.bin', root/'copy.bin'
                        self.solve(root, 'producer', source+f'[FILES]\nSAVE RUNOFF "{cache}"\n')
                        raw = cache.read_bytes(); data = RunoffData.from_bytes(raw, layout=layout)
                        self.assertEqual(data.to_bytes(), raw); data.write(copy)
                        self.assertGreater(max(f.samples[0].runoff for f in data.frames), 0)
                        for i, frame in enumerate(data.frames):
                            offset = len(b'SWMM5-RUNOFF')+16+i*(4+4*(8+len(pollutants)))
                            self.assertEqual(frame.step_seconds, struct.unpack_from('<f', raw, offset)[0])
                            self.assertEqual(frame.samples[0].values, struct.unpack_from('<'+'f'*(8+len(pollutants)), raw, offset+4))
                        if quality:
                            self.assertGreater(max(sum(f.samples[0].quality) for f in data.frames), 0)
                            self.assertGreater(max(f.samples[0].soil_moisture for f in data.frames), .1)
                        expected = self.solve(root, 'reuse', source+f'[FILES]\nUSE RUNOFF "{cache}"\n')
                        q = native_hotstart.NativeHotstartTests.quality(self, root, 'reuse') if quality else None
                        actual = self.solve(root, 'rewritten', source+f'[FILES]\nUSE RUNOFF "{copy}"\n')
                        # Native RPT echoes the consumed runoff filename.
                        self.assertIn(str(copy), actual['report'])
                        actual['report'] = actual['report'].replace(str(copy), str(cache))
                        self.assertEqual(expected, actual)
                        if quality:
                            self.assertEqual(q, native_hotstart.NativeHotstartTests.quality(self, root, 'rewritten'))
                        parsed = Model.from_document(InpDocument.from_text(source), strict=True)
                        manifest = CacheManifest.asserted(raw, layout=layout)
                        inspection = inspect_interface(raw, 'RUNOFF', parsed, manifest=manifest)
                        self.assertTrue(inspection.report.is_valid, inspection.report)
                        self.assertEqual(inspection.status, 'validated')

    def test_runoff_flow_edit_matches_independent_bytes_and_changes_hydraulics(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); model = selected(hydrology_model())
            model.update_options(end_date=date(2020, 1, 30), end_time=time(6), routing_step=timedelta(seconds=60))
            source = model.to_document().text; cache = root/'runoff.bin'; edited_path = root/'edited.bin'; oracle_path = root/'oracle.bin'
            self.solve(root, 'producer', source+f'[FILES]\nSAVE RUNOFF "{cache}"\n')
            raw = cache.read_bytes(); data = RunoffData.from_bytes(raw, layout=RunoffLayout.from_model(model))
            changed = replace(data, frames=tuple(replace(f, samples=(replace(f.samples[0], runoff=f.samples[0].runoff*2),)) for f in data.frames))
            changed.write(edited_path)
            oracle = bytearray(raw)
            for offset in range(len(b'SWMM5-RUNOFF')+16, len(raw), 36):
                value, = struct.unpack_from('<f', raw, offset+4+16)
                struct.pack_into('<f', oracle, offset+4+16, value*2)
            oracle_path.write_bytes(oracle); self.assertEqual(changed.to_bytes(), oracle)
            expected = self.solve(root, 'oracle', source+f'[FILES]\nUSE RUNOFF "{oracle_path}"\n')
            actual = self.solve(root, 'edited', source+f'[FILES]\nUSE RUNOFF "{edited_path}"\n')
            self.assertIn(str(edited_path), actual['report'])
            actual['report'] = actual['report'].replace(str(edited_path), str(oracle_path))
            self.assertEqual(expected, actual)
            baseline = self.solve(root, 'reuse', source+f'[FILES]\nUSE RUNOFF "{cache}"\n')
            self.assertNotEqual(expected['series'][1], baseline['series'][1])


if __name__ == '__main__':
    unittest.main()
