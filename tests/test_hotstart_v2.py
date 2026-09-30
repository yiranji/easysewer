"""Independent binary fixtures, identity failures, and atomic state editing."""

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from easysewer.io.hotstart import (
    HotstartLayout, HotstartData, StateObject, StatePollutant, CatchmentLayout,
    InfiltrationState, NodeState, LinkState, read_header,
)
from easysewer.io.hotstart_manifest import HotstartManifest
from easysewer.io._hotstart_model import LayoutUnavailable
from easysewer.io.interface_inspection import inspect_interface
from easysewer.io.inp import InpDocument
from easysewer.model import Model
from easysewer.runtime import check_files
from easysewer.validation import ValidationError
from test_files_v2 import bind
from test_options_v2 import network
from test_hydrology_v2 import hydrology_model


def fixture(version=4, method='HORTON', pollutants=2, landuses=2):
    """Literal reader layout, deliberately independent of production sizing."""
    layout = HotstartLayout(flow_units='CMS',
        nodes=tuple(StateObject(id=f'N{i}', kind=kind) for i, kind in enumerate(('JUNCTION', 'STORAGE', 'OUTFALL', 'DIVIDER'))),
        links=tuple(StateObject(id=f'L{i}', kind=kind) for i, kind in enumerate(('CONDUIT', 'PUMP', 'ORIFICE', 'WEIR', 'OUTLET'))),
        subcatchments=(CatchmentLayout(id='S0', infiltration=method, groundwater='Aquifer', snowpack='Snow'),
                       CatchmentLayout(id='S1', infiltration=method)),
        pollutants=tuple(StatePollutant(id=f'Q{i}', units='MG/L' if i == 0 else 'UG/L') for i in range(pollutants)),
        landuses=tuple(f'Land{i}' for i in range(landuses)))
    counts = [4, 5, pollutants, 3]
    if version >= 2: counts.insert(0, 2)
    if version >= 3: counts.insert(1, landuses)
    parts = [b'SWMM5-HOTSTART'+(str(version).encode() if version != 1 else b''), struct.pack('<'+'i'*len(counts), *counts)]
    def doubles(values): parts.append(struct.pack('<'+'d'*len(values), *values))
    def floats(values): parts.append(struct.pack('<'+'f'*len(values), *values))
    if version >= 3:
        for index in range(2):
            doubles((.01, .02, .03, .4))
            doubles((.2, 2., 3., 1., 5., 6.))
            if index == 0:
                doubles((.3, -15., -.02, 12.))
                for surface in range(3): doubles((1.+surface, .1, .02, -8., .5))
            doubles(tuple(10.+i for i in range(pollutants)))
            doubles(tuple(20.+i for i in range(pollutants)))
            if pollutants:
                for landuse in range(landuses):
                    doubles(tuple(100.+10*landuse+i for i in range(pollutants)))
                    doubles((43831.+landuse,))
    if version == 2:
        floats((.3, -15., .4, -10.))
    for index in range(4):
        floats((.5+index, -.1))
        if version == 4 and index == 1: floats((60.,))
        floats(tuple(.6+i for i in range(pollutants)))
        if version <= 2: floats(tuple(42.+i for i in range(pollutants)))
    for index in range(5): floats((-1.-index, .25, .8, *(3.+i for i in range(pollutants))))
    return layout, b''.join(parts)


def network_state():
    model = network()
    layout = HotstartLayout.from_model(model)
    raw = b'SWMM5-HOTSTART4'+struct.pack('<6i', 0, 0, 2, 1, 0, 0)+struct.pack('<7f', 1., 0., 0., 0., .5, .1, 1.)
    return model, layout, raw


class HotstartTests(unittest.TestCase):
    def test_all_versions_methods_optional_blocks_and_exact_bytes(self):
        for version in range(1, 5):
            for method in ('HORTON', 'MODIFIED_HORTON', 'GREEN_AMPT', 'MODIFIED_GREEN_AMPT', 'CURVE_NUMBER'):
                for pollutants, landuses in ((0, 2), (1, 0), (2, 2)):
                    with self.subTest(version=version, method=method, pollutants=pollutants, landuses=landuses):
                        layout, raw = fixture(version, method, pollutants, landuses)
                        data = HotstartData.from_bytes(raw, layout=layout)
                        self.assertEqual(data.to_bytes(), raw)
                        self.assertEqual(layout.byte_length(version), len(raw))
                        self.assertEqual(data.links[-1].flow, -5.)
                        self.assertEqual(data.nodes[1].residence_time, 60. if version == 4 else None)
                        if version >= 3:
                            catchment = data.subcatchments[0]
                            self.assertEqual(catchment.groundwater.water_table, -15.)
                            self.assertEqual(catchment.snow[2].snow_depth, 3.)
                            self.assertEqual(catchment.snow[0].antecedent_temperature, -8.)
                            self.assertEqual(catchment.runoff_quality, tuple(10.+i for i in range(pollutants)))
                            if pollutants and landuses:
                                self.assertEqual(catchment.landuses[1].buildup, tuple(110.+i for i in range(pollutants)))
                                self.assertEqual(catchment.landuses[1].last_swept, 43832.)
                        if version == 2:
                            self.assertEqual(data.legacy_groundwater[1].water_table, -10.)
                        if version <= 2:
                            self.assertEqual(data.nodes[0].legacy_quality, tuple(42.+i for i in range(pollutants)))

    def test_every_truncation_trailing_nonfinite_counts_and_wrong_layout(self):
        layout, raw = fixture()
        for size in range(len(raw)):
            with self.assertRaises(ValueError): HotstartData.from_bytes(raw[:size], layout=layout)
        for suffix in (b'\0', raw):
            with self.assertRaises(ValueError): HotstartData.from_bytes(raw+suffix, layout=layout)
        for offset, code in ((39, 'd'), (len(raw)-4, 'f')):
            for value in (float('nan'), float('inf'), -float('inf')):
                bad = bytearray(raw); struct.pack_into('<'+code, bad, offset, value)
                with self.assertRaises(ValueError): HotstartData.from_bytes(bytes(bad), layout=layout)
        for field in range(6):
            bad = bytearray(raw); struct.pack_into('<i', bad, 15+field*4, -1)
            with self.assertRaises(ValueError): HotstartData.from_bytes(bytes(bad), layout=layout)
        wrong = replace(layout, nodes=tuple(replace(n, kind='JUNCTION') for n in layout.nodes))
        with self.assertRaises(ValueError): HotstartData.from_bytes(raw, layout=wrong)
        for wrong in (replace(layout, flow_units='CFS'), replace(layout, landuses=()),
                      replace(layout, subcatchments=(replace(layout.subcatchments[0], snowpack=None), layout.subcatchments[1]))):
            with self.assertRaises(ValueError): HotstartData.from_bytes(raw, layout=wrong)
        with self.assertRaises(TypeError): HotstartData.from_bytes(bytearray(raw), layout=layout)

    def test_edit_named_fields_and_float32_policy_preserves_unused_values(self):
        layout, raw = fixture(method='GREEN_AMPT')
        data = HotstartData.from_bytes(raw, layout=layout)
        first = data.subcatchments[0]
        infiltration = first.infiltration.with_values(F=123., reserved_5=-0.)
        self.assertEqual(infiltration.named_values['F'], 123.)
        edited = replace(data, subcatchments=(replace(first, infiltration=infiltration), *data.subcatchments[1:]),
                         nodes=(replace(data.nodes[0], depth=.1), *data.nodes[1:]))
        independent = bytearray(raw)
        struct.pack_into('<d', independent, 39+4*8+8, 123.)
        struct.pack_into('<d', independent, 39+4*8+5*8, -0.)
        # Runoff blocks: catchment 0 (10+4+15+4+6), catchment 1 (10+4+6).
        struct.pack_into('<f', independent, 39+8*(39+20), .1)
        self.assertEqual(edited.to_bytes(), independent)
        self.assertEqual(edited.nodes[0].depth, struct.unpack('<f', struct.pack('<f', .1))[0])
        self.assertEqual(HotstartData.from_bytes(edited.to_bytes(), layout=layout), edited)
        self.assertEqual(data.to_bytes(), raw)
        with self.assertRaises(ValueError): infiltration.with_values(unknown=3)
        with self.assertRaises(ValueError): infiltration.with_values(Sat=2)

    def test_immutable_shapes_ids_types_and_version_requirements(self):
        layout, raw = fixture(); data = HotstartData.from_bytes(raw, layout=layout)
        for mutation in (lambda: replace(data, version=True), lambda: replace(data, version=5),
                         lambda: replace(data, nodes=list(data.nodes)), lambda: replace(data, nodes=tuple(reversed(data.nodes))),
                         lambda: replace(data, nodes=(replace(data.nodes[0], residence_time=0), *data.nodes[1:])),
                         lambda: replace(data, links=(replace(data.links[0], quality=()), *data.links[1:])),
                         lambda: replace(data, subcatchments=(replace(data.subcatchments[0], groundwater=None), data.subcatchments[1])),
                         lambda: replace(layout, nodes=layout.nodes+(replace(layout.nodes[0], id='n0'),)),
                         lambda: replace(layout, nodes=layout.links), lambda: replace(data.nodes[0], depth=True),
                         lambda: replace(data.nodes[0], depth=1e100), lambda: replace(data.links[0], flow=float('nan'))):
            with self.assertRaises((ValueError, TypeError)): mutation()
        with self.assertRaises(ValueError): replace(data, version=3)

    def test_serialized_native_order_instead_of_collection_order(self):
        model = hydrology_model()
        source = model.to_document().text
        # A model imported in this order stores nodes in codec collection order;
        # the native scanner instead sees the source order across sections.
        doc = InpDocument.from_text(source)
        storage = next(s for s in doc.sections if s.name == 'STORAGE')
        storage_text = storage.header.raw + ''.join(line.raw for line in storage.lines)
        source = source.replace(storage_text, '') + storage_text
        model = Model.from_document(InpDocument.from_text(source), strict=True)
        layout = HotstartLayout.from_model(model)
        self.assertEqual(tuple(n.id for n in layout.nodes), ('O', 'J'))
        self.assertEqual(tuple(n.kind for n in layout.nodes), ('OUTFALL', 'STORAGE'))
        normalized = model.to_document(normalize=True)
        expected = tuple(line.values[0] for line in normalized.lines if line.kind == 'data' and line.section in ('STORAGE', 'OUTFALLS'))
        self.assertEqual(tuple(n.id for n in HotstartLayout.from_model(model, normalize=True).nodes), expected)

    def test_opaque_dependencies_stay_explicit_even_with_manifest(self):
        model, layout, raw = network_state()
        opaque = Model.from_document(InpDocument.from_text(model.to_document().text+'[FUTURE]\nNEW STATE\n'), strict=True)
        with self.assertRaises(LayoutUnavailable): HotstartLayout.from_model(opaque)
        self.assertEqual(inspect_interface(raw, 'HOTSTART', opaque).status, 'header_only')
        manifest = HotstartManifest.asserted(raw, layout=layout)
        checked = inspect_interface(raw, 'HOTSTART', opaque, manifest=manifest)
        self.assertEqual(checked.status, 'partial')
        self.assertIn('files.hotstart_layout_pending', {d.code for d in checked.report.diagnostics})

    def test_manifest_checks_exact_bytes_case_and_full_layout_identity(self):
        layout, raw = fixture()
        manifest = HotstartManifest.asserted(raw, layout=layout, engine_sha256='a'*64, producer_input_sha256='b'*64)
        self.assertEqual(manifest.sha256, hashlib.sha256(raw).hexdigest())
        self.assertEqual(HotstartManifest.from_bytes(manifest.to_bytes()), manifest)
        self.assertEqual(manifest.verify(raw, layout=layout).to_bytes(), raw)
        case = replace(layout, nodes=tuple(replace(n, id=n.id.lower()) for n in layout.nodes))
        self.assertEqual(manifest.verify(raw, layout=case).nodes[0].id, 'n0')
        mutations = (replace(layout, nodes=tuple(reversed(layout.nodes))),
                     replace(layout, links=(replace(layout.links[0], kind='OUTLET'), *layout.links[1:])),
                     replace(layout, subcatchments=(replace(layout.subcatchments[0], infiltration='CURVE_NUMBER'), layout.subcatchments[1])),
                     replace(layout, subcatchments=(replace(layout.subcatchments[0], groundwater='Different'), layout.subcatchments[1])),
                     replace(layout, pollutants=tuple(reversed(layout.pollutants))), replace(layout, landuses=tuple(reversed(layout.landuses))))
        for wrong in mutations:
            with self.assertRaises(ValueError): manifest.verify(raw, layout=wrong)
        edited = bytearray(raw); struct.pack_into('<d', edited, 39, .25)
        with self.assertRaises(ValueError): manifest.verify(bytes(edited), layout=layout)
        for value in ('A'*64, '0'*63, 'not a hash', None):
            with self.assertRaises(ValueError): replace(manifest, sha256=value)

    def test_manifest_schema_is_strict_and_non_executable(self):
        layout, raw = fixture(); manifest = HotstartManifest.asserted(raw, layout=layout)
        original = json.loads(manifest.to_bytes())
        for change in ({'schema': 'future/2'}, {'unexpected': 1}, {'layout': {'__class__': 'os.system'}}):
            value = dict(original); value.update(change)
            with self.assertRaises((ValueError, TypeError)): HotstartManifest.from_bytes(json.dumps(value).encode())
        duplicate = manifest.to_bytes().replace(b'"schema":', b'"sha256":"c", "schema":', 1)
        with self.assertRaises(ValidationError): HotstartManifest.from_bytes(duplicate)
        with self.assertRaises(TypeError): HotstartManifest.from_bytes(bytearray(manifest.to_bytes()))

    def test_preflight_distinguishes_structure_assertion_and_unused_evidence(self):
        model, layout, raw = network_state()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'state.hsf'; path.write_bytes(raw)
            bind(model, 'HOTSTART', 'USE', path)
            basic = check_files(model)
            self.assertTrue(basic.report.is_valid); self.assertFalse(basic.complete)
            self.assertEqual(basic.checks[0].inspection.status, 'state_checked')
            manifest = HotstartManifest.asserted(raw, layout=layout)
            verified = check_files(model, interface_manifests={'HOTSTART': manifest})
            self.assertTrue(verified.complete, verified.report)
            self.assertEqual(verified.checks[0].inspection.status, 'validated')
            self.assertFalse(check_files(model, inspect_data=False, interface_manifests={'HOTSTART': manifest}).complete)
            self.assertFalse(check_files(model, max_bytes=1, interface_manifests={'HOTSTART': manifest}).complete)
            wrong = replace(manifest, layout=replace(layout, nodes=tuple(reversed(layout.nodes))))
            self.assertFalse(check_files(model, interface_manifests={'HOTSTART': wrong}).report.is_valid)
            path.write_bytes(raw[:-1])
            self.assertFalse(check_files(model).report.is_valid)
            with self.assertRaises(TypeError): check_files(model, interface_manifests={'RUNOFF': manifest})
            self.assertIn('files.unused_manifest', {d.code for d in check_files(network(), interface_manifests={'HOTSTART': manifest}).report.diagnostics})

    def test_atomic_state_and_manifest_writes_keep_existing_files_on_failure(self):
        layout, raw = fixture(); data = HotstartData.from_bytes(raw, layout=layout)
        manifest = HotstartManifest.asserted(raw, layout=layout)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'artifact'; path.write_bytes(b'existing')
            for document in (data, manifest):
                with patch('easysewer.io._atomic.os.replace', side_effect=OSError('injected')):
                    with self.assertRaises(OSError): document.write(path)
                self.assertEqual(path.read_bytes(), b'existing')
                self.assertEqual(list(Path(directory).iterdir()), [path])
            data.write(path)
            self.assertEqual(HotstartData.read(path, layout=layout), data)
            manifest.write(path)
            self.assertEqual(HotstartManifest.read(path), manifest)

    def test_empty_state_signed_zero_and_ambiguous_legacy_signature(self):
        empty = HotstartLayout(flow_units='CFS')
        for version in (1, 2, 3, 4):
            data = HotstartData(layout=empty, nodes=(), links=(), version=version)
            self.assertEqual(HotstartData.from_bytes(data.to_bytes(), layout=empty), data)
        for count in (50, 51, 52, 306):
            layout = replace(empty, nodes=tuple(StateObject(id=f'N{i}', kind='JUNCTION') for i in range(count)))
            nodes = tuple(NodeState(id=n.id, depth=0., lateral_inflow=0.) for n in layout.nodes)
            with self.assertRaises(ValueError): HotstartData(layout=layout, nodes=nodes, links=(), version=1)
        model, layout, raw = network_state()
        data = HotstartData.from_bytes(raw, layout=layout)
        edited = replace(data, nodes=(replace(data.nodes[0], depth=-0.), data.nodes[1]))
        self.assertEqual(edited.to_bytes()[39:43], struct.pack('<f', -0.))
        self.assertEqual(HotstartData.from_bytes(edited.to_bytes(), layout=layout).to_bytes(), edited.to_bytes())

    def test_supplied_manifest_cannot_be_silently_bypassed_by_custom_inspector(self):
        model, layout, raw = network_state()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'state.hsf'; path.write_bytes(raw); bind(model, 'HOTSTART', 'USE', path)
            manifest = HotstartManifest.asserted(raw, layout=layout)
            use, = model.file_uses()
            called = []
            def unexpected(*args, **kwargs):
                called.append(True)
                return inspect_interface(raw, 'HOTSTART', model, manifest=manifest)
            checked = check_files(model, interface_manifests={'HOTSTART': manifest}, inspectors={use.format: unexpected})
            self.assertFalse(checked.report.is_valid)
            self.assertFalse(called)
            self.assertIn('files.unused_manifest', {d.code for d in checked.report.diagnostics})


if __name__ == '__main__':
    unittest.main()
