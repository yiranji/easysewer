"""REPORT and project metadata checked against fixed SWMM 5.2.4 OUT/RPT files."""

from ctypes import byref
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
import struct
import tempfile
import unittest

from easysewer import get_native_capabilities
from easysewer.io.inp import InpDocument
from easysewer.model import Model, Ref
from easysewer.model.report import ReportSelection
from test_controls_v2 import controlled
from test_options_v2 import network
from test_project_v2 import selected
from test_scenario_v2 import portable


@unittest.skipUnless(get_native_capabilities()['swmm_solver'] and get_native_capabilities()['swmm_output'], 'Native solver/output unavailable')
class NativeProjectTests(unittest.TestCase):
    def solve(self, directory, name, source, *, allow_error=False, report_encoding='utf-8'):
        from easysewer.runtime._solver_api import SWMMSolverAPI
        from easysewer.runtime._output_api import SWMMOutputAPI
        base = Path(directory) / name
        inp, rpt, out = (base.with_suffix(s) for s in ('.inp', '.rpt', '.out'))
        inp.write_text(source, encoding='utf-8')
        solver = SWMMSolverAPI()
        if solver.get_version() != 52004:
            self.skipTest('Oracles require SWMM 5.2.4')
        started = False
        try:
            error = solver.open(str(inp), str(rpt), str(out))
            if not error:
                error = solver.start(1)
                started = not error
            if not error:
                for _ in range(20000):
                    error, elapsed = solver.step()
                    if error or not elapsed:
                        break
                else:
                    self.fail('Simulation did not finish')
            if started:
                ended = solver.end(); started = False
                error = error or ended
            if not error:
                error = solver.report()
        finally:
            if started:
                solver.end()
            solver.close()
        text = rpt.read_text(encoding=report_encoding, errors='strict')
        text = '\n'.join(line for line in text.splitlines() if not line.strip().startswith(
            ('Analysis begun on:', 'Analysis ended on:', 'Total elapsed time:')))
        if error:
            if allow_error:
                return {'error': error, 'report': text}
            self.fail(f'SWMM error {error}: {text}')
        # Independent header/ID layout from v5.2.4 output.c, not model indices.
        data = out.read_bytes()
        magic, version, units, ns, nn, nl, np = struct.unpack_from('<7i', data)
        self.assertEqual(version, 52004)
        offset, ids = 28, []
        for count in (ns, nn, nl, np):
            group = []
            for _ in range(count):
                length, = struct.unpack_from('<i', data, offset); offset += 4
                group.append(data[offset:offset+length].decode('utf-8')); offset += length
            ids.append(tuple(group))
        reader = SWMMOutputAPI()  # Explicit close avoids the old context manager's second init.
        try:
            reader.open(str(out)); periods = reader.get_times(1)
            series = tuple(tuple(tuple(tuple(method(i, a, 0, periods)) for a in range(attrs)) for i in range(count))
                for method, count, attrs in ((reader.get_subcatch_series, ns, 8), (reader.get_node_series, nn, 6), (reader.get_link_series, nl, 5)))
            system = tuple(tuple(reader.get_system_series(a, 0, periods)) for a in range(15))
        finally:
            self.assertEqual(reader.lib.SMO_close(byref(reader.handle)), 0)
        return {'ids': tuple(ids), 'series': series, 'system': system, 'report': text, 'periods': periods}

    def test_selection_history_matches_native_out_ids_and_rpt_modes(self):
        cases = (('NODES ALL\nNODES J', ('J',), True), ('NODES O\nNODES J', ('J', 'O'), True),
                 ('NODES J\nNODES NONE', ('J',), False), ('NODES J\nNODES ALL\nNODES NONE', ('J',), False),
                 ('NODES NONE', (), False), ('NODES J\nNODES ALL', ('J', 'O'), True))
        with tempfile.TemporaryDirectory() as directory:
            base = network(); base.update_options(report_step=timedelta(seconds=60))
            for directives, expected, detail in cases:
                with self.subTest(directives=directives):
                    source = base.to_document().text + '[REPORT]\n' + directives + '\nLINKS P\n'
                    model = Model.from_document(InpDocument.from_text(source), strict=True)
                    original = self.solve(directory, 'original', source)
                    rebuilt = self.solve(directory, 'rebuilt', portable(model).to_document().text)
                    self.assertEqual(original, rebuilt)
                    self.assertEqual(original['ids'][1], expected)
                    self.assertEqual(tuple(r.key for r in model.effective_report.nodes), expected)
                    self.assertEqual('<<< Node J >>>' in original['report'], detail)

    def test_switches_change_report_tables_without_changing_out(self):
        with tempfile.TemporaryDirectory() as directory:
            m = controlled('RULE R\nIF SIMULATION TIME >= 00:00:07\nTHEN CONDUIT P STATUS = CLOSED\nELSE CONDUIT P STATUS = OPEN\n')
            m.update_options(report_step=timedelta(seconds=60))
            m.update_report(nodes=ReportSelection(mode='ALL'), links=ReportSelection(mode='ALL'),
                            input=True, continuity=True, flow_stats=True, controls=True)
            yes = self.solve(directory, 'yes', m.to_document().text)
            for phrase in ('Flow Routing Continuity', 'Routing Time Step Summary', 'setting changed', 'Node Summary'):
                self.assertIn(phrase, yes['report'])
            m.update_report(input=False, continuity=False, flow_stats=False, controls=False)
            no = self.solve(directory, 'no', portable(m).to_document().text)
            self.assertEqual(yes['series'], no['series'])
            self.assertEqual(yes['system'], no['system'])
            for phrase in ('Flow Routing Continuity', 'Routing Time Step Summary', 'setting changed', 'Node Summary'):
                self.assertNotIn(phrase, no['report'])
            self.assertIn('Node Depth Summary', no['report'])
            m.update_report(disabled=True)
            disabled = self.solve(directory, 'disabled', m.to_document().text)
            self.assertEqual(disabled['series'], yes['series'])
            self.assertNotIn('Analysis Options', disabled['report'])
            self.assertNotIn('Node Depth Summary', disabled['report'])
            # Explicit swmm_report() still emits detailed time series in 5.2.4.
            self.assertIn('<<< Node J >>>', disabled['report'])

    def test_averages_matches_independent_declaration_and_changes_values(self):
        with tempfile.TemporaryDirectory() as directory:
            m = network(); m.update_options(report_step=timedelta(seconds=60))
            m.update_report(nodes=ReportSelection(mode='ALL'), links=ReportSelection(mode='ALL'))
            instant = self.solve(directory, 'instant', m.to_document().text)
            raw = m.to_document().text + '[REPORT]\nAVERAGES YES\n'
            oracle = self.solve(directory, 'oracle', raw)
            m.update_report(averages=True)
            actual = self.solve(directory, 'averaged', portable(m).to_document().text)
            self.assertEqual(oracle, actual)
            self.assertNotEqual(instant['series'][1], actual['series'][1])
            self.assertNotEqual(instant['series'][2], actual['series'][2])

    def test_long_selection_lists_keep_every_requested_native_id(self):
        with tempfile.TemporaryDirectory() as directory:
            m = network(); m.update_options(report_step=timedelta(seconds=60))
            names = (*('N' + str(i) for i in range(39)), 'ALL', 'NONE', 'Last')
            node, link = m.nodes['J'], m.links['P']
            for i, name in enumerate(names):
                m.nodes.add(replace(node, id=name))
                outlet = 'O' + str(i)
                m.nodes.add(replace(m.nodes['O'], id=outlet))
                m.links.add(replace(link, id='P' + str(i), inlet=Ref(collection='swmm:nodes', key=name),
                                    outlet=Ref(collection='swmm:nodes', key=outlet)))
            m.update_report(nodes=selected('nodes', *names))
            result = self.solve(directory, 'chunked', m.to_document().text)
            self.assertEqual(result['ids'][1], names)
            fresh = m.copy(); fresh.collection('swmm:report').remove('settings')
            original = fresh.to_document().text + '[REPORT]\nNODES ' + ' '.join(names) + '\n'
            parsed = Model.from_document(InpDocument.from_text(original), strict=True)
            native = self.solve(directory, 'truncated', original)
            normalized = self.solve(directory, 'normalized', portable(parsed).to_document().text)
            self.assertEqual(native['ids'][1], names[:39])
            self.assertEqual(native, normalized)

    def test_title_annotations_and_map_have_no_hydraulic_effect(self):
        with tempfile.TemporaryDirectory() as directory:
            m = network(); m.update_options(report_step=timedelta(seconds=60))
            m.update_report(nodes=ReportSelection(mode='ALL'), links=ReportSelection(mode='ALL'))
            source = m.to_document().text + '[TITLE]\nFirst "quote; retained\n;comment\nSecond\nThird\nFourth\n[MAP]\nUNITS NONE\nDIMENSIONS 0 0 100 100\n[BACKDROP]\nFILE "missing image.png"\n'
            original = self.solve(directory, 'original', source)
            m = Model.from_document(InpDocument.from_text(source), strict=True)
            m.set_annotation('user:labels', {'event': 'Storm' * 500, 'return_period': 10})
            actual = self.solve(directory, 'metadata', portable(m).to_document().text)
            self.assertEqual(actual, original)
            self.assertIn('First "quote; retained', actual['report'])
            self.assertIn('Third', actual['report'])
            self.assertNotIn('Fourth', actual['report'])
            self.assertNotIn('easysewer.annotation', actual['report'])

    def test_unsupported_lid_and_node_stats_match_native_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            m = network()
            for row in ('LID S Bio detail.txt', 'NODESTATS YES'):
                source = m.to_document().text + '[REPORT]\n' + row + '\n'
                result = self.solve(directory, 'invalid', source, allow_error=True)
                self.assertGreater(result['error'], 0)
                parsed = Model.from_document(InpDocument.from_text(source))
                self.assertFalse(parsed.validate(for_run=True).is_valid)

    def test_repository_inp_retains_typed_quality_during_project_edits(self):
        source = (Path(__file__).parent / 'test_data' / 'Model' / 'cubic.inp').read_text(encoding='utf-8')
        m = Model.from_document(InpDocument.from_text(source), strict=True)
        opaque = tuple(line.content for line in m.support.opaque_records)
        self.assertEqual(len(opaque), 0)
        self.assertEqual(len(m.pollutants), 2)
        with tempfile.TemporaryDirectory() as directory:
            original = self.solve(directory, 'original', source)
            m.update_title(lines=('Migrated project notes',))
            m.update_map(units='METERS')
            m.update_report(input=True, controls=True)
            edited = m.to_document()
            for line in opaque:
                self.assertIn(line, edited.text)
            actual = self.solve(directory, 'edited', edited.text)
            self.assertEqual(actual['ids'], original['ids'])
            self.assertEqual(actual['series'], original['series'])
            self.assertEqual(actual['system'], original['system'])
            self.assertIn('Migrated project notes', actual['report'])


if __name__ == '__main__':
    unittest.main()
