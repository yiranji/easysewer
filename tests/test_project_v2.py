"""Report selection fidelity and lossless project prose/metadata boundaries."""

from pathlib import Path
import tempfile
import unittest

from easysewer.io.inp import InpDocument
from easysewer.model import Model, Point, Ref
from easysewer.model.network import Junction
from easysewer.model.project import MapExtent
from easysewer.model.report import ReportSelection
from easysewer.validation import ValidationError
from test_options_v2 import network
from test_scenario_v2 import portable


def selected(collection, *ids, mode="SELECTED"):
    return ReportSelection(mode=mode, members=tuple(Ref(collection="swmm:" + collection, key=i) for i in ids))


def load(text):
    return Model.from_document(InpDocument.from_text(text), strict=True)


class ReportTests(unittest.TestCase):
    def test_defaults_and_explicit_switches_remain_distinct(self):
        m = Model()
        self.assertIsNone(m.report.continuity)
        self.assertTrue(m.effective_report.settings.continuity)
        self.assertFalse(m.effective_report.settings.averages)
        self.assertEqual(m.to_document().text, "")
        m.update_report(disabled=True, input=False, continuity=False, flow_stats=False,
                        controls=True, averages=True, nodes=ReportSelection(mode="ALL"))
        self.assertEqual(load(portable(m).to_document().text).report, m.report)
        m.update_report(averages=None)
        self.assertNotIn("AVERAGES", m.to_document().text)

    def test_cumulative_modes_repeated_sections_aliases_and_native_order(self):
        source = network().to_document().text + "[REPORT]\nNODE O\n[REPORT]\nNODES J j\nNODES NONE\nINPUT YESplease ignored\n"
        m = load(source)
        self.assertEqual(m.to_document().text, source)
        self.assertEqual(m.report.nodes, selected("nodes", "O", "J", mode="NONE"))
        self.assertEqual(tuple(r.key for r in m.effective_report.nodes), ("J", "O"))
        self.assertIn("report.sticky_members", {d.code for d in m.validate().diagnostics})
        self.assertEqual(load(portable(m).to_document().text).report, m.report)
        m.nodes.rename("J", "Upstream")
        m.links.rename("P", "Pipe")
        self.assertEqual(load(m.to_document().text).report.nodes.members[1].key, "Upstream")
        with self.assertRaises(ValidationError):
            m.nodes.remove("O")

    def test_all_then_list_and_ignored_selector_tail(self):
        m = load(network().to_document().text + "[REPORT]\nNODES ALL Missing\nNODES J\nLINKS NONE Missing\n")
        self.assertEqual(m.report.nodes, selected("nodes", "J"))
        self.assertEqual(m.effective_report.links, ())
        self.assertEqual(load(m.to_document(normalize=True).text).report, m.report)
        self.assertIn("report.ignored_tail", {d.code for d in m.validate().diagnostics})

    def test_long_lists_are_chunked_and_reserved_ids_do_not_become_selectors(self):
        m = Model()
        names = (*("N" + str(i) for i in range(39)), "ALL", "NONE", *("J" + str(i) for i in range(60)))
        for name in names:
            m.nodes.add(Junction(id=name, elevation=0))
        m.update_report(nodes=selected("nodes", *names))
        doc = m.to_document()
        self.assertGreater(len(doc.records("REPORT")), 2)
        self.assertTrue(all(len(row.values) <= 40 for row in doc.records("REPORT")))
        self.assertTrue(all(row.values[1] not in ("ALL", "NONE") for row in doc.records("REPORT")))
        self.assertEqual(load(doc.text).report, m.report)

    def test_import_long_list_models_only_native_first_39_members(self):
        names = tuple("N" + str(i) for i in range(45))
        source = "[JUNCTIONS]\n" + "".join(n + " 0\n" for n in names) + "[REPORT]\nNODES " + " ".join(names) + "\n"
        m = load(source)
        self.assertEqual(m.report.nodes, selected("nodes", *names[:39]))
        self.assertEqual(m.to_document().text, source)
        self.assertIn("report.native_token_limit", {d.code for d in m.validate().diagnostics})
        self.assertEqual(load(m.to_document(normalize=True).text).report, m.report)

    def test_invalid_fields_preserve_source_without_partial_claim(self):
        for invalid in ("INPUT maybe", "NODES", "AVERAGES 1"):
            source = network().to_document().text + "[REPORT]\nINPUT YES\n" + invalid + "\n"
            m = Model.from_document(InpDocument.from_text(source))
            self.assertFalse(m.validate().is_valid)
            self.assertEqual(m.document.text, source)
            with self.assertRaises(ValidationError):
                m.to_document()
        for selection in (selected("nodes", "J", "j"), selected("nodes"), selected("nodes", "ALL"), selected("links", "P")):
            with self.assertRaises(ValidationError):
                network().update_report(nodes=selection)

    def test_manual_lid_and_node_stats_are_not_silently_reinterpreted(self):
        m = load(network().to_document().text + '[REPORT]\nLID S Bio "detail.txt"\n')
        self.assertEqual(m.support.opaque_records[0].values[0], "LID")
        self.assertIn("report.native_lid_keyword", {d.code for d in m.validate(for_run=True).errors})
        source = network().to_document().text + "[REPORT]\nNODESTATS YES\n"
        m = Model.from_document(InpDocument.from_text(source))
        self.assertEqual(m.report.nodes, selected("nodes", "YES"))
        self.assertFalse(m.validate().is_valid)


class ProjectTests(unittest.TestCase):
    def test_title_raw_lines_comments_bom_repeated_sections_and_json(self):
        lines = (' 中文 title "unterminated; literal', ';comment', '', '{"ordinary":"JSON"}')
        text = '[TITLE]\r\n' + '\r\n'.join(lines[:3]) + '\r\n[TITLE]\r\n' + lines[3] + '\r\n'
        original = text.encode('utf-8-sig')
        m = Model.from_document(InpDocument.from_bytes(original), strict=True)
        self.assertEqual(m.title.lines, lines)
        self.assertFalse(m.metadata)
        self.assertEqual(m.to_document().to_bytes(), original)
        self.assertEqual(Model.from_json_document(m.to_json_document(), strict=True).to_document().to_bytes(), original)
        m.update_title(lines=(*lines, 'last line'))
        doc = m.to_document()
        self.assertEqual(len(doc.find_sections('TITLE')), 1)
        self.assertEqual(doc.text.count(';comment'), 1)
        self.assertEqual(load(doc.text).title, m.title)

    def test_annotation_chunks_json_references_and_units_are_informational(self):
        m = network()
        m.update_title(lines=('Project title', '; note'))
        value = {'name': '暴雨' * 700, 'id': 'J', 'number': 2.5, 'nested': [None, True, {'x': 3}]}
        m.set_annotation('user:labels', value)
        value['number'] = 99
        self.assertEqual(m.metadata['user:labels'].value['number'], 2.5)
        m.metadata['user:labels'].value['number'] = 88
        original = m.metadata['user:labels']
        doc = m.to_document()
        self.assertGreater(doc.text.count(';@easysewer.annotation/1'), 2)
        self.assertTrue(all(len(line.content) < 1023 for line in doc.lines))
        restored = load(doc.text)
        self.assertEqual(restored.metadata['user:labels'], original)
        self.assertEqual(restored.title, m.title)
        self.assertEqual(portable(restored).metadata['user:labels'], original)
        restored.set_annotation('user:second', {'x': 2})
        restored = load(restored.to_document().text)
        restored.metadata.move('user:second', before='user:labels')
        restored = load(restored.to_document().text)
        self.assertEqual(tuple(restored.metadata), ('user:second', 'user:labels'))
        restored.metadata.remove('user:second')
        restored.nodes.rename('J', 'Moved')
        restored.convert_units('CMS')
        self.assertEqual(restored.metadata['user:labels'], original)
        restored.metadata.rename('user:labels', 'user:event')
        self.assertEqual(load(restored.to_document().text).metadata['user:event'].value, original.value)
        restored.metadata.remove('user:event')
        self.assertNotIn(';@easysewer.annotation/', restored.to_document().text)

    def test_unknown_invalid_duplicate_and_reserved_annotation_markers(self):
        source = '[TITLE]\n;@easysewer.annotation/2 future opaque\nordinary\n'
        m = load(source)
        m.update_title(lines=(*m.title.lines, 'more'))
        self.assertIn(';@easysewer.annotation/2 future opaque', m.to_document().text)
        known = Model(); known.set_annotation('user:labels', {'x': 3})
        marker = known.to_document().lines[1].content
        for text in (marker + '\n' + marker, marker.replace('1/1', '1/2'), marker.replace(' 1/1 ', ' 2/2 '),
                     marker.replace('1/1', '1/99999999999999999999999999'), marker.replace('1/1', '1/' + '9'*5000),
                     ';@easysewer.annotation/1 invalid'):
            bad = Model.from_document(InpDocument.from_text('[TITLE]\n' + text + '\n'))
            self.assertFalse(bad.validate().is_valid)
            self.assertEqual(bad.document.text, '[TITLE]\n' + text + '\n')
        m = Model(); m.update_title(lines=(marker,))
        with self.assertRaises(ValidationError):
            m.to_document()

    def test_title_syntax_and_native_limits_are_separate(self):
        for line in ('[REPORT]', 'one\ntwo', 'nul\x00'):
            with self.assertRaises(ValidationError):
                Model().update_title(lines=(line,))
        m = network(); m.update_title(lines=('one', ';ignored', '', 'two', 'three', 'four'))
        self.assertTrue(m.validate().is_valid)
        self.assertIn('project.native_title_limit', {d.code for d in m.validate(for_run=True).diagnostics})
        m.update_title(lines=('long ' * 250,))
        self.assertTrue(m.validate().is_valid)
        self.assertIn('project.native_line_limit', {d.code for d in m.validate(for_run=True).errors})

    def test_map_units_are_independent_and_backdrop_rebases(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); old = root / 'old'; old.mkdir()
            source = network().to_document().text + '[MAP]\nDIMENSIONS 0 1 100 200\nUNITS Degrees\n[BACKDROP]\nFILE "a picture.png"\nDIMENSIONS -1 -2 101 202\n'
            path = old / 'model.inp'; path.write_text(source, encoding='utf-8')
            m = Model.from_inp(path, strict=True)
            original = m.map
            expected_backdrop = (old / 'a picture.png').resolve()
            self.assertEqual(m.backdrop.file.resolve(), expected_backdrop)
            m.convert_units('CMS')
            self.assertEqual(m.map, original)
            m.to_inp(root / 'new' / 'model.inp')
            n = Model.from_inp(root / 'new' / 'model.inp', strict=True)
            self.assertEqual(n.backdrop.file.resolve(), expected_backdrop)
            self.assertEqual(n.map, original)
            self.assertEqual(portable(n).backdrop.file.resolve(), expected_backdrop)
            for units in ('FEET', 'METERS', 'DEGREES', 'NONE'):
                n.update_map(units=units)
                self.assertEqual(load(n.to_document().text).map.units, units)
        with self.assertRaises(ValidationError):
            Model().update_map(extent=MapExtent(lower_left=Point(x=1, y=0), upper_right=Point(x=0, y=2)))

    def test_unknown_map_settings_preserve_source_and_guard_changes(self):
        source = '[MAP]\nUNITS FEET\nFUTURE opaque path\n'
        m = load(source); m.update_map(units='NONE')
        self.assertIn('FUTURE opaque path', m.to_document().text)
        with self.assertRaises(ValidationError):
            m.convert_units('CMS')
        for row in ('DIMENSIONS 1 2 0 3', 'UNITS UNKNOWN', 'FILE "bad;quote"'):
            bad = Model.from_document(InpDocument.from_text('[BACKDROP]\n' + row + '\n' if row.startswith('FILE') else '[MAP]\n' + row + '\n'))
            self.assertFalse(bad.validate().is_valid)




if __name__ == '__main__':
    unittest.main()
