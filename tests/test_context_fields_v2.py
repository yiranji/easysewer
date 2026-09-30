"""Context facts follow native slots and source coordinates, without doing IO."""
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import date, datetime, time
import unittest
from unittest.mock import patch

from easysewer.io.inp import InpDocument
from easysewer.model import Model, Ref
from easysewer.model.events import RoutingEvent
from easysewer.model.files import InterfaceFile
from easysewer.model.options import DayTime
from easysewer.model.report import REPORT_DEFAULTS, ReportOptions, ReportSelection
from easysewer.model.values import FileReference
from easysewer.validation import ValidationError
from test_project_fields_v2 import fixture as project_fixture, UNITS

REPORT = Ref(collection='swmm:report', key='settings')
OPTIONS = Ref(collection='swmm:options', key='settings')
EVENTS = Ref(collection='swmm:events', key='schedule')
COLLECTIONS = ('swmm:report', 'swmm:files', 'swmm:events', 'swmm:options')


def load(text, *, strict=True, source='C:/fixtures/model.inp'):
    return Model.from_document(InpDocument.from_text(text, source=source), strict=strict)


def fixture(units='CFS', *, variant='mixed'):
    text = project_fixture(units, variant='absent')
    blocks = {
        'report': '[REPORT]\nINPUT NO\nCONTINUITY YES\nFLOWSTATS YES\nCONTROLS YES\nAVERAGES NO\nDISABLED NO\nNODES ALL\nNODES J j O\nNODES NONE\nLINKS P\n',
        'events': '[EVENTS]\n01/01/2020 00:15 01/01/2020 00:25\n01/01/2020 00:01 01/01/2020 00:20\n[OPTIONS]\nRULE_STEP 00:00:17\n',
        'clock': '[OPTIONS]\nSTART_DATE 12/31/2019\nSTART_TIME 24:00\nEND_DATE 12/31/2019\nEND_TIME 24:40\nREPORT_START_DATE 12/30/2019\nREPORT_START_TIME 25:00\n',
    }
    return text + (''.join(blocks.values()) if variant == 'mixed' else blocks.get(variant, ''))


def queries(model):
    result = []
    def walk(owner, value, path=()):
        if is_dataclass(value):
            for f in fields(value):
                p = (*path, f.name)
                result.extend((model.inspect_field(owner, p), model.field_provenance(owner, p)))
                walk(owner, getattr(value, f.name), p)
        elif isinstance(value, tuple):
            for i, child in enumerate(value):
                p = (*path, i)
                result.extend((model.inspect_field(owner, p), model.field_provenance(owner, p)))
                walk(owner, child, p)
    for collection in COLLECTIONS:
        for key, value in model.collection(collection).items():
            walk(Ref(collection=collection, key=key), value)
    return tuple(result)


class ContextFieldTests(unittest.TestCase):
    def test_all_units_queries_are_pure_and_json_preserves_sources(self):
        for units in UNITS:
            with self.subTest(units=units):
                m = load(fixture(units) + '[FILES]\nSAVE HOTSTART "state file.hsf"\n')
                before = queries(m)
                for info in before[::2]:
                    self.assertEqual(info.semantics.effective.status, 'known', info)
                with patch('builtins.open', side_effect=AssertionError('Unexpected IO')), \
                     patch.object(Model, 'to_document', side_effect=AssertionError('Unexpected render')):
                    self.assertEqual(queries(m), before)
                restored = Model.from_json_document(m.to_json_document(), strict=True)
                self.assertEqual(queries(restored), before)
                self.assertEqual(restored.to_document().text, m.to_document().text)

    def test_report_native_sticky_members_precise_contributors_and_spans(self):
        m = load(fixture(variant='report'))
        self.assertEqual(m.report.nodes.mode, 'NONE')
        self.assertEqual([r.key for r in m.report.nodes.members], ['J', 'O'])
        self.assertEqual({r.key for r in m.effective_report.nodes}, {'J', 'O'})
        self.assertEqual(m.inspect_field(REPORT, 'nodes').semantics.effective.value, m.report.nodes)
        provenance = m.field_provenance(REPORT, 'nodes')
        self.assertEqual([d.contributes for d in provenance.declarations], [False, False, True, True])
        mode = m.field_provenance(REPORT, ('nodes', 'mode'))
        self.assertEqual([d.contributes for d in mode.declarations], [False, False, False, True])
        key = m.field_provenance(REPORT, ('nodes', 'members', 0, 'key'))
        self.assertEqual([d.contributes for d in key.declarations], [True, False])
        self.assertEqual([d.tokens[0].value for d in key.declarations], ['J', 'j'])
        ns = m.field_provenance(REPORT, ('nodes', 'members', 0, 'collection'))
        self.assertEqual(len(ns.declarations), 1)
        self.assertEqual(ns.status, 'derived')
        self.assertEqual(m.inspect_field(REPORT, ('nodes', 'members', 0)).provenance.status, 'untracked_path')
        for info in queries(m)[1::2]:
            for d in info.declarations:
                for token in d.tokens:
                    raw = m.to_document().text.splitlines()[token.span.line - 1]
                    self.assertEqual(raw[token.span.column - 1:token.span.end_column - 1], token.raw)

    def test_report_defaults_aliases_token_limit_and_failed_group(self):
        m = load('[REPORT]\nINPUTplease YESplease ignored\n')
        for name, default in REPORT_DEFAULTS:
            info = m.inspect_field(REPORT, name)
            self.assertEqual(info.semantics.default.value, default)
            self.assertEqual(info.semantics.effective.value, True if name == 'input' else default)
        text = fixture(variant='absent') + '[REPORT]\nNODE ' + ' '.join(['J'] * 39 + ['absent']) + '\n'
        m = load(text)
        self.assertEqual(len(m.report.nodes.members), 1)
        self.assertEqual(len(m.field_provenance(REPORT, ('nodes', 'members', 0, 'key')).declarations), 39)
        m = load(text + '[REPORT]\nNODES\n', strict=False)
        self.assertIsNone(m.report.nodes)
        self.assertEqual(m.field_provenance(REPORT, 'nodes').status, 'unknown')
        self.assertEqual(m.field_provenance(REPORT, 'links').status, 'derived')
        with self.assertRaises(ValidationError): m.to_document()

    def test_report_reference_errors_and_invalid_profile_defaults(self):
        m = Model(); m.update_report(nodes=ReportSelection(mode='SELECTED', members=(Ref(collection='swmm:nodes', key='missing'),)))
        self.assertEqual(m.inspect_field(REPORT, 'nodes').semantics.effective.status, 'invalid')
        profile = replace(m.profile, report_defaults=(('input', True),))
        m = Model(profile=profile); m.update_report(input=True)
        self.assertEqual(m.inspect_field(REPORT, 'input').semantics.default.status, 'invalid')

    def test_report_rename_copy_and_transaction_keep_original_coordinates(self):
        m = load(fixture(variant='report'))
        original = m.field_provenance(REPORT, ('nodes', 'members', 0, 'key'))
        m.nodes.rename('J', 'Upstream')
        self.assertEqual(m.inspect_field(REPORT, ('nodes', 'members', 0, 'key')).semantics.effective.value, 'Upstream')
        self.assertEqual(m.field_provenance(REPORT, ('nodes', 'members', 0, 'key')), original)
        before = queries(m)
        with self.assertRaises(RuntimeError):
            with m.transaction():
                m.update_report(nodes=ReportSelection(mode='ALL'))
                raise RuntimeError('rollback')
        self.assertEqual(queries(m), before)
        self.assertEqual(queries(m.copy()), before)
        self.assertEqual(queries(Model.from_json_document(m.to_json_document(), strict=True)), before)

    def test_files_ten_combinations_paths_and_replaced_slots(self):
        for kind in ('RAINFALL', 'RUNOFF', 'HOTSTART', 'RDII', 'INFLOWS', 'OUTFLOWS'):
            for mode in ('USE', 'SAVE'):
                if (kind, mode) in (('INFLOWS', 'SAVE'), ('OUTFLOWS', 'USE')): continue
                with self.subTest(kind=kind, mode=mode):
                    m = load(f'[FILES]\n{mode} {kind} "路径 file.dat"\n')
                    owner = Ref(collection='swmm:files', key=(kind, mode))
                    for info in queries(m)[::2]:
                        if info.owner.collection == 'swmm:files':
                            self.assertEqual(info.semantics.effective.status, 'known', info)
                    file = m.collection('swmm:files')[owner.key].file
                    self.assertEqual(file.base_directory, None if kind == 'RDII' else 'C:\\fixtures')
                    self.assertEqual(m.field_provenance(owner, ('file', 'direction')).status, 'derived')
                    self.assertEqual(m.field_provenance(owner, ('file', 'path')).status, 'explicit')
        m = load('[FILES]\nUSE RUNOFF old.dat\nSAVE RUNOFF new.dat\nUSE RUNOFF\n')
        owner = Ref(collection='swmm:files', key=('RUNOFF', 'SAVE'))
        p = m.field_provenance(owner, 'mode')
        self.assertEqual([d.tokens[0].value for d in p.declarations], ['USE', 'SAVE'])
        self.assertEqual([d.contributes for d in p.declarations], [False, True])
        self.assertEqual(m.collection('swmm:files')[owner.key].file.path, 'new.dat')
        m = load('[FILES]\nUSE HOTSTART a\nSAVE HOTSTART b\n')
        self.assertEqual(len(m.collection('swmm:files')), 2)

    def test_files_failed_slot_lexical_unknown_and_programmatic_conflict(self):
        m = load('[FILES]\nUSE RUNOFF old\nNO RUNOFF newer\nSAVE HOTSTART good\n')
        self.assertEqual(len(m.collection('swmm:files')), 1)
        m = load('[FILES]\nUSE RUNOFF old\nSAVE RUNOFF "unterminated\n', strict=False)
        owner = Ref(collection='swmm:files', key=('RUNOFF', 'USE'))
        self.assertEqual(m.field_provenance(owner, ('file', 'path')).status, 'unknown')
        m = Model()
        for mode, direction in (('USE', 'input'), ('SAVE', 'output')):
            m.collection('swmm:files').add(InterfaceFile(kind='RUNOFF', mode=mode, file=FileReference(path='x', direction=direction)))
        self.assertEqual(m.inspect_field(owner, 'file').semantics.effective.status, 'invalid')

    def test_events_configured_order_derived_tokens_and_partial_failure(self):
        text = '[EVENTS]\n01/01/2020 00:04 01/01/2020 00:06\n12/31/2019 24:01 01/01/2020 .08333333333333333\n'
        m = load(text)
        self.assertEqual(m.events.periods[1].start, datetime(2020, 1, 1, 0, 1))
        self.assertEqual(m.inspect_field(EVENTS, 'periods').semantics.effective.value, m.events.periods)
        p = m.field_provenance(EVENTS, ('periods', 1, 'start'))
        self.assertEqual([t.value for t in p.declarations[0].tokens], ['12/31/2019', '24:01'])
        self.assertEqual(p.status, 'derived')
        self.assertEqual(m.inspect_field(EVENTS, ('periods', 1, 'start')).semantics.unit.value, 'local model datetime')
        for bad in ('bad', '01/01/2020 4 01/01/2020 3', '"unterminated'):
            m = load(text + bad + '\n', strict=False)
            self.assertEqual(m.field_provenance(EVENTS, 'periods').status, 'unknown')
            retained = m.field_provenance(EVENTS, ('periods', 1, 'start'))
            self.assertEqual(retained.status, p.status)
            self.assertEqual(retained.declarations, p.declarations)
        m = load(text); original = m.field_provenance(EVENTS, ('periods', 0, 'start'))
        m.update_events(periods=tuple(reversed(m.events.periods)))
        self.assertEqual(m.field_provenance(EVENTS, ('periods', 0, 'start')), original)
        self.assertEqual(m.inspect_field(EVENTS, ('periods', 0, 'start')).provenance.status, 'untracked_path')

    def test_daytime_effective_components_follow_resolved_parent_calendar(self):
        m = load(fixture(variant='clock'))
        expected = {'start_time': (time(), 1), 'end_time': (time(0, 40), 1), 'report_start_time': (time(), 0)}
        for name, pair in expected.items():
            for field, value in zip(('clock', 'day_offset'), pair):
                info = m.inspect_field(OPTIONS, (name, field))
                self.assertEqual(info.semantics.effective.value, value)
                self.assertEqual(info.semantics.default.status, 'not_applicable')
                self.assertEqual(info.provenance.status, 'derived')
        self.assertEqual(m.inspect_field(OPTIONS, 'report_start_date').semantics.effective.value, date(2020, 1, 1))
        m.update_options(report_start_date=date(2019, 12, 31), report_start_time=DayTime(clock=time(0, 20), day_offset=1))
        self.assertEqual(m.inspect_field(OPTIONS, ('report_start_time', 'clock')).semantics.effective.value, time(0, 20))
        self.assertEqual(m.inspect_field(OPTIONS, ('report_start_time', 'day_offset')).semantics.effective.value, 0)
        m.update_options(end_time=DayTime(day_offset=0))
        self.assertEqual(m.inspect_field(OPTIONS, ('start_time', 'clock')).semantics.effective.status, 'invalid')

    def test_daytime_variant_switches_errors_and_json(self):
        base = '[OPTIONS]\nSTART_DATE 01/01/2020\nEND_DATE 01/03/2020\nSTART_TIME 00:00\nSTART_TIME 25:00\n'
        m = load(base)
        p = m.field_provenance(OPTIONS, ('start_time', 'day_offset'))
        self.assertEqual([d.contributes for d in p.declarations], [False, True])
        self.assertEqual(queries(Model.from_json_document(m.to_json_document())), queries(m))
        m = load(base + 'START_TIME 00:00\n')
        self.assertEqual(m.field_provenance(OPTIONS, ('start_time', 'day_offset')).status, 'absent_path')
        m = load(base + 'START_TIME invalid\n', strict=False)
        self.assertEqual(m.field_provenance(OPTIONS, ('start_time', 'clock')).status, 'unknown')

    def test_exact_types_do_not_claim_extension_semantics(self):
        @dataclass(frozen=True, kw_only=True)
        class FutureEvent(RoutingEvent):
            future: int = 1
        m = Model(); m.update_events(periods=(FutureEvent(start=datetime(2020, 1, 1), end=datetime(2020, 1, 2)),))
        self.assertEqual(m.inspect_field(EVENTS, ('periods', 0, 'start')).semantics.effective.status, 'unknown')


if __name__ == '__main__': unittest.main()
