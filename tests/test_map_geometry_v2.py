"""Joint GUI geometry ownership, ordered vertices and mutation semantics."""
from dataclasses import replace
from datetime import time, timedelta
import unittest

from easysewer.io.inp import InpDocument
from easysewer.io.json import JsonDocument
from easysewer.model import Model, Point, Ref
from easysewer.model.resources import SeriesPoint
from easysewer.scenario import ScenarioPatch
from easysewer.validation import ValidationError
from test_hydrology_v2 import hydrology_model
from test_scenario_v2 import portable, fields

POINTS = (Point(x=-1, y=2), Point(x=3, y=4), Point(x=-1, y=2))


def geometry_model():
    model = hydrology_model()
    model.update_options(end_date=model.options.start_date, end_time=time(0, 15),
        routing_step=timedelta(seconds=5), report_step=timedelta(minutes=1),
        wet_step=timedelta(seconds=60), dry_step=timedelta(seconds=60))
    model.raingages.update('R', interval=timedelta(minutes=5), position=Point(x=1, y=2))
    model.timeseries.update('Rain', points=tuple(SeriesPoint(time=timedelta(minutes=m), value=v)
        for m, v in ((0, .6), (5, 1.2), (10, 0), (15, 0))))
    model.nodes.update('J', polygon=POINTS, position=Point(x=0, y=1))
    model.links.update('P', vertices=POINTS)
    model.subcatchments.update('S', polygon=tuple(reversed(POINTS)) + (Point(x=8, y=9),))
    return model


def geometry(model):
    return ({k: (v.position, getattr(v, 'polygon', ())) for k, v in model.nodes.items()},
            {k: v.vertices for k, v in model.links.items()},
            {k: v.polygon for k, v in model.subcatchments.items()},
            {k: v.position for k, v in model.raingages.items()})


class MapGeometryTests(unittest.TestCase):
    def assert_roundtrip(self, model):
        for other in (Model.from_document(model.to_document(), strict=True),
                      Model.from_document(model.to_document(normalize=True), strict=True),
                      portable(model), Model.from_document(portable(model).to_document(), strict=True)):
            self.assertEqual(geometry(other), geometry(model))
            for name in ('nodes', 'links', 'subcatchments', 'raingages'):
                self.assertEqual(list(getattr(other, name)), list(getattr(model, name)))

    def test_empty_repeated_sections_declarations_after_geometry_and_ignored_tails(self):
        model = geometry_model()
        model.nodes.update('J', polygon=(), position=None)
        model.links.update('P', vertices=())
        model.raingages.update('R', position=None)
        model.subcatchments.update('S', polygon=())
        source = ('[COORDINATES]\n[VERTICES]\n[POLYGONS]\n[SYMBOLS]\n'
            '[POLYGONS]\nJ -1 2 tail\nS 7 8\n[COORDINATES]\nJ 1 2\n'
            '[VERTICES]\nP -1 2 trailing\n[SYMBOLS]\nR 1 2 extra\n' + model.to_document().text +
            '[POLYGONS]\nJ 3 4\nJ -1 2\nS 5 6 tail\n'
            '[COORDINATES]\nj 3 4 extra\n[VERTICES]\nP -1 2\n[SYMBOLS]\nr 5 6\n')
        source = source.replace('\n', '\r\n')
        parsed = Model.from_document(InpDocument.from_text(source, source='geometry.inp'), strict=True)
        self.assertEqual(parsed.to_document().text, source)
        self.assertEqual(parsed.nodes['J'].polygon, POINTS)
        self.assertEqual(parsed.nodes['J'].position, Point(x=3, y=4))
        self.assertEqual(parsed.links['P'].vertices, (POINTS[0],) * 2)
        self.assertEqual(parsed.raingages['R'].position, Point(x=5, y=6))
        self.assertFalse(parsed.support.opaque_records)
        codes = {d.code for d in parsed.validate().diagnostics}
        self.assertTrue({'geometry.repeated_assignment', 'geometry.ignored_columns'} <= codes)
        normalized = parsed.to_document(normalize=True)
        self.assertEqual(len(normalized.records('COORDINATES')), 1)
        self.assertEqual(len(normalized.records('SYMBOLS')), 1)
        self.assertEqual(len(normalized.records('POLYGONS')), 5)
        self.assert_roundtrip(parsed)
        parsed.nodes.update('J', position=Point(x=9, y=10))
        self.assert_roundtrip(parsed)

    def test_same_id_across_four_kinds_and_subcatchment_polygon_priority(self):
        model = geometry_model()
        model.nodes.update('J', polygon=())
        model.subcatchments.update('S', outlet=Ref(collection='swmm:nodes', key='O'))
        for name, old in (('nodes', 'J'), ('links', 'P'), ('raingages', 'R'), ('subcatchments', 'S')):
            getattr(model, name).rename(old, 'Shared')
        self.assert_roundtrip(model)
        parsed = Model.from_document(model.to_document(), strict=True)
        self.assertFalse(parsed.nodes['Shared'].polygon)
        self.assertTrue(parsed.subcatchments['Shared'].polygon)
        self.assertTrue(all(parsed.support.owners[r.number] == 'swmm:hydrology'
            for r in parsed.document.records('POLYGONS')))
        before = model.to_document().text
        with self.assertRaises(ValidationError):
            with model.transaction():
                model.nodes.update('shared', polygon=POINTS)
        self.assertEqual(model.to_document().text, before)
        model.nodes.rename('Shared', 'Storage')
        model.nodes.update('Storage', polygon=POINTS)
        self.assert_roundtrip(model)
        # A malformed declared subcatchment must not reroute its polygon to storage.
        invalid = Model.from_document(InpDocument.from_text(
            '[STORAGE]\nX 0 5 0 FUNCTIONAL 0 1 500\n'
            '[SUBCATCHMENTS]\nx invalid\n[POLYGONS]\nx 1 2\n'))
        self.assertFalse(invalid.validate().is_valid)
        self.assertFalse(invalid.nodes['X'].polygon)
        self.assertTrue(any(r.section == 'POLYGONS' for r in invalid.support.opaque_records))

    def test_rename_reorder_clear_delete_and_recreate_do_not_resurrect_geometry(self):
        model = Model.from_document(geometry_model().to_document(), strict=True)
        old = model.nodes['J']
        model.nodes.rename('J', 'Tank')
        model.nodes.move('Tank')
        model.links.rename('P', 'Route')
        model.links.update('Route', vertices=tuple(reversed(POINTS)) + (Point(x=7, y=8),))
        model.subcatchments.rename('S', 'Catch')
        model.raingages.rename('R', 'Gauge')
        self.assert_roundtrip(model)
        model.nodes.update('Tank', polygon=(), position=None)
        model.links.update('Route', vertices=())
        model.subcatchments.update('Catch', polygon=())
        model.raingages.update('Gauge', position=None)
        for section in ('POLYGONS', 'VERTICES', 'COORDINATES', 'SYMBOLS'):
            self.assertFalse(model.to_document().records(section))
        self.assert_roundtrip(model)
        model.nodes.remove('Tank', cascade=True)
        model.nodes.add(replace(old, polygon=(), position=None))
        self.assert_roundtrip(model)
        self.assertFalse(model.to_document().records('POLYGONS'))

    def test_scenario_six_flow_units_and_nonphysical_cache(self):
        from test_cache_reuse_v2 import snapshot, compare
        model = geometry_model()
        patch = ScenarioPatch(operations=(fields('nodes', 'J', polygon=tuple(reversed(POINTS))),
            fields('links', 'P', vertices=()), fields('raingages', 'R', position=None),
            fields('subcatchments', 'S', polygon=(Point(x=5, y=6),))))
        changed = ScenarioPatch.from_json_document(patch.to_json_document()).apply(model).model
        self.assertEqual(compare(snapshot(model), snapshot(changed), intent='require_match').status, 'matched')
        self.assert_roundtrip(changed)
        for unit in ('CFS', 'GPM', 'MGD', 'CMS', 'LPS', 'MLD'):
            converted = changed.copy()
            converted.convert_units(unit)
            self.assertEqual(geometry(converted), geometry(changed))
            self.assert_roundtrip(converted)

    def test_invalid_coordinates_unknown_owners_and_old_json_defaults(self):
        for section, row in (('COORDINATES', 'J nan 1'), ('VERTICES', 'P 1'),
                             ('POLYGONS', 'J 1 inf'), ('POLYGONS', 'O 1 2'),
                             ('POLYGONS', 'Missing 1 2'), ('SYMBOLS', 'R 1 nan')):
            source = geometry_model().to_document().text + f'[{section}]\n{row}\n'
            invalid = Model.from_document(InpDocument.from_text(source, source='bad-geometry.inp'))
            self.assertTrue(invalid.support.opaque_records)
            self.assertTrue(any(d.span and d.span.source == 'bad-geometry.inp' for d in invalid.validate().diagnostics))
            with self.assertRaises(ValidationError):
                invalid.nodes.rename('J', 'Other')
        model = geometry_model()
        model.nodes.update('J', polygon=())
        data = portable(model).to_json_document().data
        def strip(value):
            if isinstance(value, dict):
                if value.get('type') == 'swmm:network.storage':
                    value.pop('polygon', None)
                for child in value.values(): strip(child)
            elif isinstance(value, list):
                for child in value: strip(child)
        strip(data)
        restored = Model.from_json_document(JsonDocument.from_data(data), strict=True)
        self.assertEqual(restored.nodes['J'], model.nodes['J'])
        with self.assertRaises(ValidationError):
            with model.transaction():
                model.nodes.update('J', polygon=(Point(x=float('nan'), y=0),))
        self.assertFalse(model.nodes['J'].polygon)


if __name__ == '__main__':
    unittest.main()
