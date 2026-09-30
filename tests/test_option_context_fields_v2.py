"""Sweeping calendar and directory facts are scoped to the owning option."""
from dataclasses import dataclass, replace
from datetime import date, time
from pathlib import PureWindowsPath
import unittest
from unittest.mock import patch

from easysewer.io.inp import InpDocument
from easysewer.model import Model, Ref
from easysewer.model.options import MonthDay
from easysewer.model.values import FileReference
from easysewer.validation import ValidationError
from test_context_fields_v2 import UNITS
from test_options_v2 import ALL_OPTIONS

OWNER = Ref(collection='swmm:options', key='settings')
PATHS = (('sweep_start', 'month'), ('sweep_start', 'day'), ('sweep_end', 'month'), ('sweep_end', 'day'),
         ('temp_directory', 'path'), ('temp_directory', 'base_directory'), ('temp_directory', 'flavor'), ('temp_directory', 'direction'))
PERIOD = '[OPTIONS]\nSTART_DATE 01/01/2020\nEND_DATE 01/03/2020\n'


def load(text, *, source='C:/fixtures/model.inp', strict=True):
    return Model.from_document(InpDocument.from_text(text, source=source), strict=strict)


def queries(model):
    return tuple((model.inspect_field(OWNER, path), model.field_provenance(OWNER, path)) for path in PATHS
                 if getattr(model.options, path[0]) is not None)


class OptionContextFieldTests(unittest.TestCase):
    def test_all_eight_paths_six_units_no_io_and_json(self):
        for units in UNITS:
            with self.subTest(units=units):
                m = load(ALL_OPTIONS); m.reinterpret_units(units); before = queries(m)
                self.assertEqual(len(before), 8)
                for info, provenance in before:
                    self.assertEqual(info.semantics.effective.status, 'known', info)
                    self.assertEqual(info.semantics.effective.value, info.value)
                    self.assertNotEqual(info.semantics.unit.status, 'unknown')
                    self.assertIn(provenance.status, ('explicit', 'derived'))
                with patch('builtins.open', side_effect=AssertionError('Unexpected IO')), \
                     patch.object(Model, 'to_document', side_effect=AssertionError('Unexpected rendering')):
                    self.assertEqual(queries(m), before)
                restored = Model.from_json_document(m.to_json_document(), strict=True)
                self.assertEqual(queries(restored), before)
                self.assertEqual(restored.to_document().to_bytes(), m.to_document().to_bytes())
                m.convert_units('CMS' if units != 'CMS' else 'CFS')
                self.assertEqual(queries(m), before)

    def test_parent_defaults_are_separate_from_component_defaults(self):
        m = load(PERIOD)
        self.assertEqual(m.inspect_field(OWNER, 'sweep_start').semantics.default.value, MonthDay(month=1, day=1))
        self.assertEqual(m.inspect_field(OWNER, 'sweep_end').semantics.default.value, MonthDay(month=12, day=31))
        self.assertIsNone(m.inspect_field(OWNER, 'temp_directory').semantics.default.value)
        self.assertEqual(m.field_provenance(OWNER, ('sweep_start', 'month')).status, 'absent_path')
        m.update_options(sweep_start=MonthDay(month=3, day=1), sweep_end=MonthDay(month=12, day=31),
                         temp_directory=FileReference(path='scratch', direction='output'))
        for info, _ in queries(m):
            expected = 'required' if info.path == ('temp_directory', 'path') else 'not_applicable'
            self.assertEqual(info.semantics.default.status, expected)
        self.assertEqual(m.inspect_field(OWNER, ('sweep_start', 'month')).semantics.unit.value, 'month')
        self.assertEqual(m.inspect_field(OWNER, ('sweep_start', 'day')).semantics.unit.value, 'day')
        self.assertIn('leap-year', m.inspect_field(OWNER, ('sweep_start', 'day')).semantics.effective.reason)

    def test_original_token_spans_last_assignment_and_lexical_directory_context(self):
        text = PERIOD + 'SWEEP_START 1/2\nSWEEP_START 12/31\nSWEEP_END 2/28\nTEMPDIR "old folder"\n[OPTIONS]\nTEMPDIR "新 folder" ; note\n'
        m = load(text)
        for path in (('sweep_start', 'month'), ('sweep_start', 'day')):
            p = m.field_provenance(OWNER, path)
            self.assertEqual([d.contributes for d in p.declarations], [False, True])
            self.assertEqual([d.tokens[0].value for d in p.declarations], ['1/2', '12/31'])
            self.assertEqual(p.status, 'derived')
        for name in ('path', 'base_directory', 'flavor', 'direction'):
            p = m.field_provenance(OWNER, ('temp_directory', name))
            self.assertEqual([d.contributes for d in p.declarations], [False, True])
            self.assertEqual(p.declarations[-1].tokens[0].value, 'TEMPDIR' if name == 'direction' else '新 folder')
        self.assertEqual(m.options.temp_directory.base_directory, 'C:\\fixtures')
        self.assertEqual(m.options.temp_directory.flavor, 'windows')
        for _, p in queries(m):
            for declaration in p.declarations:
                for token in declaration.tokens:
                    self.assertEqual(text.splitlines()[token.span.line - 1][token.span.column - 1:token.span.end_column - 1], token.raw)

    def test_missing_base_and_windows_absolute_path_do_not_imply_availability(self):
        for source, path, expected_base in ((None, '不存在/scratch', None),
                (None, 'C:/missing/scratch', None), ('C:/fixtures/model.inp', 'other/scratch', 'C:\\fixtures')):
            with self.subTest(source=source, path=path):
                m = load(PERIOD + f'TEMPDIR "{path}"\n', source=source)
                self.assertEqual(m.inspect_field(OWNER, ('temp_directory', 'base_directory')).semantics.effective.value, expected_base)
                self.assertEqual(m.inspect_field(OWNER, ('temp_directory', 'path')).semantics.effective.value, path)
                self.assertEqual(m.inspect_field(OWNER, ('temp_directory', 'direction')).semantics.effective.value, 'output')
                self.assertEqual(queries(Model.from_json_document(m.to_json_document(), strict=True)), queries(m))

    def test_bad_known_assignments_block_all_components_but_keep_valid_history(self):
        text = PERIOD + 'SWEEP_START 3/1\nSWEEP_END 10/31\nTEMPDIR "good folder"\n'
        for bad, parent in (('SWEEP_START 2/29', 'sweep_start'), ('SWEEP_END 13/1', 'sweep_end'),
                            ('TEMPDIR "unterminated', 'temp_directory'), ('TEMPDIR', 'temp_directory')):
            with self.subTest(bad=bad):
                m = load(text + bad + '\n', strict=False)
                for info, p in queries(m):
                    if info.path[0] == parent:
                        self.assertEqual(p.status, 'unknown'); self.assertEqual(len(p.declarations), 1)
                    else:
                        self.assertIn(p.status, ('explicit', 'derived'))
                self.assertEqual(m.document.text, text + bad + '\n')
                with self.assertRaises(ValidationError): m.to_document()

    def test_rollback_clear_and_rebase_keep_original_sources(self):
        m = load(PERIOD + 'SWEEP_START 3/1\nSWEEP_END 10/31\nTEMPDIR scratch\n')
        before = queries(m)
        with self.assertRaises(RuntimeError):
            with m.transaction():
                m.update_options(sweep_start=MonthDay(month=4, day=2), temp_directory=None)
                raise RuntimeError('rollback')
        self.assertEqual(queries(m), before); self.assertEqual(queries(m.copy()), before)
        file = m.options.temp_directory
        self.assertEqual(file.resolve(), PureWindowsPath('C:/fixtures/scratch'))
        self.assertEqual(file.for_directory('C:/elsewhere'), '../fixtures/scratch'.replace('/', '\\'))
        m.update_options(sweep_start=MonthDay(month=4, day=2), temp_directory=replace(file, path='changed'))
        self.assertTrue(m.inspect_field(OWNER, ('sweep_start', 'month')).changed)
        self.assertTrue(m.inspect_field(OWNER, ('temp_directory', 'path')).changed)
        self.assertEqual([p for _, p in queries(m)], [p for _, p in before])
        self.assertEqual(queries(Model.from_json_document(m.to_json_document(), strict=True)), queries(m))
        m.update_options(sweep_start=None, temp_directory=None)
        self.assertEqual(m.field_provenance(OWNER, ('temp_directory', 'path')), before[4][1])

    def test_invalid_calendar_or_direction_propagates_parent_diagnostics(self):
        m = load(PERIOD + 'SWEEP_START 3/1\nTEMPDIR scratch\n')
        m.update_options(end_date=date(2019, 1, 1))
        for path in (('sweep_start', 'month'), ('temp_directory', 'path')):
            child = m.inspect_field(OWNER, path).semantics
            self.assertEqual(child.effective.status, 'invalid')
            self.assertEqual(child.diagnostics, m.inspect_field(OWNER, path[0]).semantics.diagnostics)
        with self.assertRaises(ValidationError):
            m.update_options(end_date=date(2020, 1, 3), temp_directory=FileReference(path='scratch'))
        self.assertEqual(m.options.end_date, date(2019, 1, 1))
        m.update_options(end_date=date(2020, 1, 3))
        self.assertEqual(m.inspect_field(OWNER, ('temp_directory', 'direction')).semantics.effective.value, 'output')

    def test_exact_extension_types_remain_unknown(self):
        @dataclass(frozen=True, kw_only=True)
        class FutureMonthDay(MonthDay):
            future: int = 1
        @dataclass(frozen=True, kw_only=True)
        class FutureFile(FileReference):
            future: int = 1
        m = Model(); m.update_options(start_date=date(2020, 1, 1), end_date=date(2020, 1, 3),
            sweep_start=FutureMonthDay(month=3, day=1), temp_directory=FutureFile(path='scratch', direction='output'))
        for path in (('sweep_start', 'month'), ('temp_directory', 'path')):
            self.assertEqual(m.inspect_field(OWNER, path).semantics.effective.status, 'unknown')


if __name__ == '__main__': unittest.main()
