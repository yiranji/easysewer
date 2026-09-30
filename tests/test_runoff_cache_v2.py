"""Independent RUNOFF/RDII binary, exact identities and empty dry caches."""

from dataclasses import replace
from datetime import datetime, timedelta
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from easysewer.io.cache_manifest import CacheManifest
from easysewer.io.hotstart import StatePollutant
from easysewer.io.inp import InpDocument
from easysewer.io.interface_inspection import inspect_interface
from easysewer.io.runoff_cache import RunoffLayout, RunoffData, RdiiLayout, RdiiBindingIdentity, RdiiData, RdiiFrame
from easysewer.model import Model
from easysewer.runtime import check_files
from easysewer.validation import ValidationError
from test_files_v2 import bind
from test_rdii_v2 import rdii_model


def runoff_fixture(pollutants=2):
    layout = RunoffLayout(flow_units='CFS', subcatchments=('S', 'T'),
        pollutants=tuple(StatePollutant(id=f'Q{i}', units='MG/L') for i in range(pollutants)))
    parts = [b'SWMM5-RUNOFF', struct.pack('<4i', 2, pollutants, 0, 2)]
    for frame in range(2):
        parts.append(struct.pack('<f', 60.5+frame))
        for index in range(2):
            parts.append(struct.pack('<'+'f'*(8+pollutants), *(100*frame+10*index+i+.125 for i in range(8+pollutants))))
    return layout, b''.join(parts)


def rdii_fixture(*, empty=False):
    layout = RdiiLayout(nodes=('A', 'B'), bindings=(RdiiBindingIdentity(node='A', hydrograph='H'), RdiiBindingIdentity(node='B', hydrograph='G')))
    raw = b'SWMM5-RDII'+struct.pack('<4i', 60, 2, 0, 1)
    if not empty:
        raw += struct.pack('<d2f', 43831., .1, .2)+struct.pack('<d2f', 43831.+120/86400, .3, .4)
    return layout, raw


class RunoffCacheTests(unittest.TestCase):
    def test_runoff_all_fields_quality_and_float32_edit_exact_bytes(self):
        for pollutants in (0, 1, 2):
            layout, raw = runoff_fixture(pollutants)
            data = RunoffData.from_bytes(raw, layout=layout)
            self.assertEqual(data.to_bytes(), raw)
            self.assertEqual(data.duration_seconds, 122.)
            self.assertEqual(data.frames[1].samples[1].groundwater_elevation, 116.125)
            self.assertEqual(data.frames[1].samples[1].quality, tuple(118.125+i for i in range(pollutants)))
            first = data.frames[0]
            edited = replace(data, frames=(replace(first, samples=(replace(first.samples[0], runoff=.3), first.samples[1])), data.frames[1]))
            oracle = bytearray(raw); struct.pack_into('<f', oracle, len(b'SWMM5-RUNOFF')+16+4+4*4, .3)
            self.assertEqual(edited.to_bytes(), oracle)
            self.assertEqual(RunoffData.from_bytes(edited.to_bytes(), layout=layout), edited)

    def test_runoff_truncation_trailing_counts_units_and_nonfinite_values(self):
        layout, raw = runoff_fixture()
        for size in range(len(raw)):
            with self.assertRaises(ValueError): RunoffData.from_bytes(raw[:size], layout=layout)
        with self.assertRaises(ValueError): RunoffData.from_bytes(raw+b'X', layout=layout)
        with self.assertRaises(ValueError): RunoffData.from_bytes(raw, layout=replace(layout, flow_units='CMS'))
        for offset in (len(b'SWMM5-RUNOFF')+16, len(raw)-4):
            for value in (float('nan'), float('inf')):
                bad = bytearray(raw); struct.pack_into('<f', bad, offset, value)
                with self.assertRaises(ValueError): RunoffData.from_bytes(bytes(bad), layout=layout)
        data = RunoffData.from_bytes(raw, layout=layout)
        with self.assertRaises(ValueError): replace(data.frames[0], step_seconds=0)
        with self.assertRaises(ValueError): replace(data, layout=replace(layout, subcatchments=('T', 'S')))
        with self.assertRaises(TypeError): RunoffData.from_bytes(bytearray(raw), layout=layout)

    def test_positive_steps_must_advance_the_native_double_clock(self):
        layout, raw = runoff_fixture()
        data = RunoffData.from_bytes(raw, layout=layout)
        with self.assertRaisesRegex(ValueError, 'advance'):
            replace(data, frames=(data.frames[0], replace(data.frames[1], step_seconds=1e-40)))
        tiny = replace(data.frames[0], step_seconds=1e-40)
        # Tiny values alone are representable; no arbitrary minimum is imposed.
        self.assertEqual(len(replace(data, frames=(tiny, tiny)).frames), 2)

    def test_rdii_sparse_and_dry_data_exact_clock_indices_and_flow(self):
        for empty in (False, True):
            layout, raw = rdii_fixture(empty=empty)
            data = RdiiData.from_bytes(raw); data.validate_layout(layout)
            self.assertEqual(data.to_bytes(), raw)
            self.assertEqual(data.node_indices, (0, 1))
            self.assertEqual(data.step, timedelta(seconds=60))
            if not empty:
                self.assertEqual(data.frames[0].time, datetime(2020, 1, 1))
                changed = replace(data, frames=(replace(data.frames[0], flows=(1., 2.)), data.frames[1]))
                oracle = bytearray(raw); struct.pack_into('<2f', oracle, len(b'SWMM5-RDII')+16+8, 1., 2.)
                self.assertEqual(changed.to_bytes(), oracle)
                self.assertEqual(RdiiData.from_bytes(changed.to_bytes()), changed)

    def test_rdii_rejects_partial_frames_overlap_duplicate_and_unbound_indices(self):
        layout, raw = rdii_fixture()
        header = len(b'SWMM5-RDII')+16
        for size in range(len(raw)):
            if size >= header and (size-header) % 16 == 0:
                continue  # The format has no frame count; complete prefixes are valid.
            with self.assertRaises(ValueError): RdiiData.from_bytes(raw[:size])
        data = RdiiData.from_bytes(raw)
        for indices in ((0, 0), (-1, 1), (False, 1)):
            with self.assertRaises(ValueError): replace(data, node_indices=indices)
        with self.assertRaises(ValueError): replace(data, frames=tuple(reversed(data.frames)))
        with self.assertRaises(ValueError): replace(data, frames=(data.frames[0], replace(data.frames[1], native_time=43831.+30/86400)))
        with self.assertRaises(ValueError): data.validate_layout(replace(layout, bindings=layout.bindings[:1]))
        with self.assertRaises(ValueError): replace(data, node_indices=(0, 99)).validate_layout(layout)
        for value in (float('inf'), -1e10, 1e20):
            with self.assertRaises(ValueError): RdiiFrame(native_time=value, flows=(1., 2.))

    def test_manifests_cover_exact_bytes_order_constituents_and_bindings(self):
        for layout, raw in (runoff_fixture(), rdii_fixture(), rdii_fixture(empty=True)):
            manifest = CacheManifest.asserted(raw, layout=layout, engine_sha256='a'*64, producer_input_sha256='b'*64)
            self.assertEqual(CacheManifest.from_bytes(manifest.to_bytes()), manifest)
            self.assertEqual(manifest.verify(raw, layout=layout).to_bytes(), raw)
            with self.assertRaises(ValueError): manifest.verify(raw+b'X', layout=layout)
            if type(layout) is RunoffLayout:
                wrong = (replace(layout, subcatchments=('T','S')), replace(layout, pollutants=tuple(reversed(layout.pollutants))),
                         replace(layout, pollutants=(replace(layout.pollutants[0], units='UG/L'), layout.pollutants[1])))
                case = replace(layout, subcatchments=('s','t'))
            else:
                wrong = (replace(layout, nodes=('B','A')), replace(layout, bindings=(replace(layout.bindings[0], hydrograph='Changed'), layout.bindings[1])))
                case = replace(layout, nodes=('a','b'), bindings=tuple(replace(v, node=v.node.lower(), hydrograph=v.hydrograph.lower()) for v in reversed(layout.bindings)))
            self.assertEqual(manifest.verify(raw, layout=case).to_bytes(), raw)
            for target in wrong:
                with self.assertRaises(ValueError): manifest.verify(raw, layout=target)
            value = json.loads(manifest.to_bytes()); value['kind'] = 'FUTURE'
            with self.assertRaises(ValueError): CacheManifest.from_bytes(json.dumps(value).encode())
            duplicate = manifest.to_bytes().replace(b'"schema":', b'"sha256":"c", "schema":', 1)
            with self.assertRaises(ValidationError): CacheManifest.from_bytes(duplicate)

    def test_preflight_proves_current_rdii_binding_and_preserves_unknown_scope(self):
        model = rdii_model(); layout = RdiiLayout.from_model(model)
        index = layout.nodes.index('J')
        raw = b'SWMM5-RDII'+struct.pack('<3i', 60, 1, index)+struct.pack('<df', 43860., .2)
        manifest = CacheManifest.asserted(raw, layout=layout)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'rdii.bin'; path.write_bytes(raw); bind(model, 'RDII', 'USE', path)
            self.assertFalse(check_files(model).complete)
            self.assertTrue(check_files(model, interface_manifests={'RDII':manifest}).complete)
            missing = model.copy(); missing.files.remove(('RDII','USE')); missing.rdii.remove('J')
            self.assertFalse(inspect_interface(raw, 'RDII', missing).report.is_valid)
            opaque = Model.from_document(InpDocument.from_text(model.to_document().text+'[FUTURE]\nNEW STATE\n'), strict=True)
            checked = check_files(opaque, interface_manifests={'RDII':manifest})
            self.assertFalse(checked.complete)
            self.assertEqual(checked.checks[0].inspection.status, 'partial')
            self.assertTrue(checked.report.is_valid)
            self.assertFalse(inspect_interface(b'SWMM5 text', 'RDII', model, manifest=manifest).report.is_valid)

    def test_runoff_preflight_needs_positional_evidence(self):
        model = rdii_model(); model.subcatchments.add(replace(model.subcatchments['S'], id='T'))
        layout, raw = runoff_fixture(0)
        self.assertEqual(RunoffLayout.from_model(model), layout)
        manifest = CacheManifest.asserted(raw, layout=layout)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'runoff.bin'; path.write_bytes(raw); bind(model, 'RUNOFF', 'USE', path)
            self.assertFalse(check_files(model).complete)
            self.assertTrue(check_files(model, interface_manifests={'RUNOFF':manifest}).complete)
            wrong = replace(manifest, layout=replace(layout, subcatchments=('T','S')))
            self.assertFalse(check_files(model, interface_manifests={'RUNOFF':wrong}).report.is_valid)

    def test_atomic_writers_and_explicit_reads(self):
        rlayout, rraw = runoff_fixture(); dlayout, draw = rdii_fixture()
        documents = (RunoffData.from_bytes(rraw, layout=rlayout), RdiiData.from_bytes(draw), CacheManifest.asserted(draw, layout=dlayout))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'artifact'; path.write_bytes(b'old')
            for document in documents:
                with patch('easysewer.io._atomic.os.replace', side_effect=OSError('injected')):
                    with self.assertRaises(OSError): document.write(path)
                self.assertEqual(path.read_bytes(), b'old')
                self.assertEqual(list(Path(directory).iterdir()), [path])
            for document in documents:
                document.write(path)
                kwargs = {'layout':rlayout} if type(document) is RunoffData else {}
                self.assertEqual(type(document).read(path, **kwargs), document)


if __name__ == '__main__':
    unittest.main()
