"""Construct every F domain together without decoding those INP sections."""
from pathlib import Path
from datetime import date, time
import hashlib
import tempfile
import unittest

from easysewer import get_native_capabilities
from easysewer.model import Model, Point
from easysewer.model.project import MapLabel, ProfilePlot, ObjectTag, MapExtent
from easysewer.scenario import ScenarioPatch
from easysewer.utils import probe_library_path
from test_events_v2 import period
from test_f_domain_v2 import f_model, state, edit_patch, target
from test_map_geometry_v2 import geometry_model
from test_native_v2_runner_checkpoint import reports
from test_native_v2_standard_io import direct_library, execute

EVIDENCE = []


def create_model():
    model = geometry_model()
    model.update_options(start_date=date(2020, 1, 1), end_date=date(2020, 1, 1),
                         report_start_date=date(2020, 1, 1), start_time=time(0))
    model.set_annotation('user:f-domain', {'id': 'J', 'note': '独立元数据', 'scale': 2.5})
    model.update_events(periods=(period(360, 720), period(60, 240), period(60, 240)))
    for collection, key, text in (('raingages', 'R', 'rain'), ('subcatchments', 'S', 'catchment'),
                                  ('nodes', 'J', 'storage'), ('links', 'P', 'pipe')):
        model.tags.add(ObjectTag(target=target(collection, key), text=text))
    anchored = MapLabel(position=Point(x=1, y=2), text='same label', anchor=target('nodes', 'J'))
    model.update_labels(entries=(anchored, anchored, MapLabel(position=Point(x=3, y=4), text='free')))
    model.profiles.add(ProfilePlot(name='Main profile', links=(target('links', 'P'),) * 2))
    model.update_map(extent=MapExtent(lower_left=Point(x=-10, y=-10), upper_right=Point(x=100, y=100)),
                     units='FEET', units_precedence='BACKDROP')
    model.update_backdrop(clear_file=True, units='METERS',
        extent=MapExtent(lower_left=Point(x=10, y=10), upper_right=Point(x=0, y=0)),
        legacy_offset=Point(x=-1, y=2), legacy_scaling=Point(x=1, y=-1))
    return model


@unittest.skipUnless(get_native_capabilities()['swmm_solver'] and get_native_capabilities()['flexible_ponding'],
                    'Standard/custom native solvers unavailable')
class NativeFCreationTests(unittest.TestCase):
    def test_typed_creation_edit_reimport_and_both_solvers(self):
        created, imported = create_model(), f_model()
        self.assertEqual(state(created), state(imported))
        patch = ScenarioPatch.from_json_document(edit_patch(created).to_json_document())
        changed = patch.apply(created).model
        expected = patch.apply(imported).model
        actual = Model.from_document(changed.to_document(), strict=True)
        self.assertEqual(state(actual), state(expected))
        for family, name, symbol in (
            ('standard', 'swmm5', 'swmm_getEasySewerStandardFixes'),
            ('custom', 'flexible_ponding', 'swmm_getEasySewerNativeIOFixes')):
            lib, library = direct_library(probe_library_path(name), revision_symbol=symbol)
            with self.subTest(family=family), tempfile.TemporaryDirectory() as directory:
                results = []
                for index, model in enumerate((expected, actual)):
                    root = Path(directory)/str(index)
                    root.mkdir()
                    self.assertTrue(all(code == 0 for code in execute(lib, root, model.to_document().text)))
                    results.append(((root/'model.out').read_bytes(), reports((root/'model.rpt').read_bytes())))
                self.assertEqual(results[0], results[1])
                EVIDENCE.append(dict(family=family, kind='typed-creation',
                    out_sha256=hashlib.sha256(results[1][0]).hexdigest(),
                    report_sha256=hashlib.sha256(results[1][1]).hexdigest(),
                    library_sha256=hashlib.sha256(library.read_bytes()).hexdigest()))


if __name__ == '__main__':
    unittest.main()
