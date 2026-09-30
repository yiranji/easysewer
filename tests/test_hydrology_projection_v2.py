"""Hydrology field edits preserve source layout and native identity ordering."""
from collections import Counter
from dataclasses import replace
import unittest
from unittest.mock import patch

from easysewer.io.inp import InpDocument
from easysewer.model import Model, Point
from easysewer.schema.registry import RegistryError


SOURCE = '''; original header
[OPTIONS]
INFILTRATION HORTON
[RAINGAGES]
R VOLUME 0:05 1 TIMESERIES Rain
Q VOLUME 0:05 1 TIMESERIES Rain
[TIMESERIES]
Rain 0 0
[JUNCTIONS]
J 0
[STORAGE]
T 0 10 0 FUNCTIONAL 1 1 1
[SUBCATCHMENTS]
A R J 15.6 100 1000 .5 0 ; area
B Q J 20 50 900 .7 0
[SUBAREAS]
A .01 .1 .05 .05 0 OUTLET
B .02 .1 .05 .05 0 OUTLET
[INFILTRATION]
A 3 .5 4 7 0
B 3 .5 4 7 0
[Polygons]
A 1 2
A 3 4
B 5 6
T 7 8 ; another feature owns this polygon
'''


class HydrologyProjectionTests(unittest.TestCase):
    def model(self, text=SOURCE):
        return Model.from_document(InpDocument.from_bytes(text.replace('\n', '\r\n').encode('utf-8-sig')), strict=True)

    def test_area_edit_changes_only_its_source_line_and_survives_json(self):
        model = self.model(); original = model.document.to_bytes()
        model.subcatchments.update('A', area=19.5)
        output = model.to_document()
        expected = original.replace(b'A R J 15.6 100 1000 .5 0 ; area', b'A R J 19.5 100.0 1000.0 0.5 0.0 ; area')
        self.assertEqual(output.to_bytes(), expected)
        self.assertEqual(model.document.to_bytes(), original)
        restored = Model.from_json_document(model.to_json_document(), strict=True)
        self.assertEqual(restored.to_document().to_bytes(), expected)
        self.assertEqual(Model.from_document(output, strict=True).subcatchments['A'].area, 19.5)

    def test_numeric_edits_leave_other_rows_headers_and_opaque_content_unchanged(self):
        changes = (
            (lambda m: m.raingages.update('R', snow_factor=2), 'R VOLUME'),
            (lambda m: m.subcatchments.update('A', subareas=replace(m.subcatchments['A'].subareas, pervious_roughness=.2)), 'A .01 .1'),
            (lambda m: m.subcatchments.update('A', infiltration=replace(m.subcatchments['A'].infiltration,
                parameters=replace(m.subcatchments['A'].infiltration.parameters, maximum_rate=5))), 'A 3 .5'),
            (lambda m: m.subcatchments.update('A', polygon=(Point(x=9, y=2), *m.subcatchments['A'].polygon[1:])), 'A 1 2'),
        )
        for change, prefix in changes:
            with self.subTest(prefix=prefix):
                model = self.model(SOURCE+'[FUTURE]\nunknown exact tokens ; keep\n')
                before = model.document.text.splitlines(); change(model)
                output = model.to_document(); after = output.text.splitlines()
                self.assertEqual(len(after), len(before))
                changed = [(a, b) for a, b in zip(before, after) if a != b]
                self.assertEqual(len(changed), 1); self.assertTrue(changed[0][0].startswith(prefix))
                reread = Model.from_document(output, strict=True)
                self.assertEqual(reread.subcatchments['A'], model.subcatchments['A'])
                self.assertEqual(reread.raingages['R'], model.raingages['R'])
                self.assertIn('unknown exact tokens ; keep', output.text)
                self.assertEqual(Counter(s.name for s in output.sections), Counter(s.name for s in model.document.sections))

    def test_identity_reorder_and_rename_still_rebuild_in_collection_order(self):
        model = self.model()
        model.subcatchments.move('B', before='A'); model.raingages.move('Q', before='R')
        model.raingages.rename('R', 'RainGauge')
        output = model.to_document()
        self.assertEqual([r.values[0] for r in output.records('SUBCATCHMENTS')], ['B', 'A'])
        self.assertEqual([r.values[0] for r in output.records('RAINGAGES')], ['Q', 'RainGauge'])
        reread = Model.from_document(output, strict=True)
        self.assertEqual(list(reread.subcatchments), ['B', 'A'])
        self.assertEqual(list(reread.raingages), ['Q', 'RainGauge'])
        self.assertEqual(reread.subcatchments['A'].rain_gage.key, 'RainGauge')
        self.assertEqual(reread.nodes['T'].polygon, model.nodes['T'].polygon)

    def test_source_dependent_rewrite_requires_an_explicit_boolean(self):
        from easysewer.io.inp.hydrology import HydrologyCodec
        model = self.model(); model.subcatchments.update('A', area=19.5)
        with patch.object(HydrologyCodec, 'requires_atomic_write', return_value='yes'):
            with self.assertRaisesRegex(RegistryError, 'must be boolean'):
                model.to_document()
