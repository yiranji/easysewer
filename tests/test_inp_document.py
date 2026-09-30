import codecs
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

from easysewer.io.inp import InpDocument, PatchConflictError, TextEdit, format_token
from easysewer.validation import ValidationError


class TestInpDocument(unittest.TestCase):
    def test_byte_roundtrip_unknown_repeated_sections_and_mixed_newlines(self):
        source = codecs.BOM_UTF8 + (
            '; 原文\r\n[Options] ; options\nRULE_STEP 00:05:00\r'
            '[TEMPERATURE]\r\nFILE "../气候 Data.txt" * C\n'
            '[FUTURE]\nopaque value ; keep me\n[OPTIONS]\nROUTING_STEP 0.25'
        ).encode('utf-8')
        document = InpDocument.from_bytes(source, source='network.inp')
        self.assertEqual(document.to_bytes(), source)
        self.assertEqual(document.encoding, 'utf-8-sig')
        self.assertEqual(document.newline, '\r\n')
        self.assertEqual(len(document.find_sections('[ options ]')), 2)
        self.assertEqual([r.values for r in document.records('OPTIONS')],
                         [('RULE_STEP', '00:05:00'), ('ROUTING_STEP', '0.25')])
        self.assertEqual(document.records('FUTURE')[0].comment, '; keep me')
        self.assertTrue(document.report.is_valid)

    def test_empty_blank_and_no_final_newline_inputs(self):
        for data in (b'', b'\n', b'\r\n', b'\r', b'; comment', b'[OPTIONS]',
                     b'[OPTIONS]\r\n\r\n', b'[OPTIONS]\nRULE_STEP 0'):
            with self.subTest(data=data):
                document = InpDocument.from_bytes(data)
                self.assertEqual(document.to_bytes(), data)
                self.assertEqual(''.join(line.raw for line in document.lines), document.text)

    def test_known_fixture_roundtrips_without_a_model_decoder(self):
        path = Path(__file__).parent / 'test_data' / 'Model' / 'cubic.inp'
        document = InpDocument.read(path)
        self.assertEqual(document.to_bytes(), path.read_bytes())
        self.assertTrue(document.report.is_valid, document.report)
        self.assertEqual(document.source, str(path.resolve()))
        self.assertTrue(document.records('XSECTIONS'))

    def test_raw_title_is_not_json_or_a_quoted_grammar(self):
        text = '[TITLE]\nA "half quote ; original title\n{not JSON}\n[OPTIONS]\nRULE_STEP 0\n'
        document = InpDocument.from_text(text)
        self.assertTrue(document.report.is_valid)
        self.assertEqual([r.content for r in document.records('TITLE')],
                         ['A "half quote ; original title', '{not JSON}'])
        self.assertEqual(document.to_bytes(), text.encode())

    def test_windows_paths_have_literal_backslashes(self):
        document = InpDocument.from_text('[TIMESERIES]\nTs FILE "D:\\new folder\\test.txt"\n')
        row = document.records('TIMESERIES')[0]
        self.assertEqual(row.values, ('Ts', 'FILE', 'D:\\new folder\\test.txt'))
        self.assertTrue(row.tokens[2].quoted)
        self.assertEqual(row.tokens[2].raw, '"D:\\new folder\\test.txt"')

    def test_ids_and_values_do_not_change_case(self):
        document = InpDocument.from_text('[jUnCtIoNs]\nMixedCaseNode 1.0\n')
        self.assertEqual(document.sections[0].name, 'JUNCTIONS')
        self.assertEqual(document.records('JUNCTIONS')[0].values[0], 'MixedCaseNode')

    def test_semicolon_in_quotes_matches_engine_comment_boundary(self):
        text = '[TIMESERIES]\nTs FILE "rain;2026.dat"\n'
        document = InpDocument.from_text(text, source='bad.inp')
        self.assertEqual(document.to_bytes(), text.encode())
        self.assertEqual(document.report.errors[0].code, 'inp.unterminated_quote')
        span = document.report.errors[0].span
        self.assertEqual((span.source, span.line, span.column), ('bad.inp', 2, 9))
        self.assertEqual(document.records('TIMESERIES')[0].comment, ';2026.dat"')
        with self.assertRaises(ValidationError) as error:
            document.report.raise_for_errors()
        self.assertIs(error.exception.report, document.report)

    def test_single_quotes_are_literal_not_shell_quotes(self):
        document = InpDocument.from_text("[TIMESERIES]\nO'Brien FILE 'two words'\n")
        self.assertEqual(document.records('TIMESERIES')[0].values,
                         ("O'Brien", 'FILE', "'two", "words'"))

    def test_malformed_header_stops_previous_section(self):
        document = InpDocument.from_text('[OPTIONS]\nRULE_STEP 0\n[BROKEN\nN1 4\n[TAGS]\nNode N1 zone\n')
        self.assertEqual(len(document.records('OPTIONS')), 1)
        self.assertEqual(len(document.records('TAGS')), 1)
        self.assertIsNone(document.lines[3].section)
        self.assertEqual([item.code for item in document.report.errors],
                         ['inp.invalid_section_header', 'inp.record_without_section'])

    def test_unscoped_records_and_nul_are_reported_but_retained(self):
        data = b'bad data\n[OPTIONS]\nBAD \x00\n'
        document = InpDocument.from_bytes(data)
        self.assertEqual(document.to_bytes(), data)
        self.assertEqual([item.code for item in document.report.errors],
                         ['inp.record_without_section', 'inp.nul_character'])

    def test_empty_or_nul_header_is_retained_with_diagnostics(self):
        for header in ('[ ]', '[\t]', '[BAD\x00SECTION]'):
            with self.subTest(header=header):
                source = f'[OPTIONS]\nRULE_STEP 0\n{header}\nunknown data\n'
                document = InpDocument.from_text(source)
                self.assertEqual(document.to_bytes(), source.encode())
                self.assertIn('inp.invalid_section_header',
                              [item.code for item in document.report.errors])
                self.assertIsNone(document.lines[3].section)
                self.assertEqual(len(document.records('OPTIONS')), 1)

    def test_unicode_line_separator_is_not_a_physical_inp_newline(self):
        document = InpDocument.from_text('[TITLE]\nlabel\u2028text\n')
        self.assertEqual(len(document.lines), 2)
        self.assertEqual(document.lines[1].content, 'label\u2028text')

    def test_explicit_encoding_and_utf8_bom(self):
        source = b'[TITLE]\r\n\xb3 caf\xe9\r\n'
        with self.assertRaises(UnicodeDecodeError):
            InpDocument.from_bytes(source)
        document = InpDocument.from_bytes(source, encoding='cp1252')
        self.assertEqual(document.to_bytes(), source)
        self.assertIn('café', document.text)
        self.assertEqual(document.to_bytes(encoding='utf-8'), document.text.encode('utf-8'))
        bom = codecs.BOM_UTF8 + b'[OPTIONS]\n'
        self.assertEqual(InpDocument.from_bytes(bom, encoding='utf8').sections[0].name, 'OPTIONS')

    def test_document_is_immutable(self):
        document = InpDocument.from_text('[OPTIONS]\n')
        with self.assertRaises(FrozenInstanceError):
            document.text = 'different'
        with self.assertRaises(FrozenInstanceError):
            document.lines[0].section = 'OTHER'

    def test_token_edit_preserves_every_unrelated_character_and_bom(self):
        text = '; 注释\r\n[OPTIONS]\r\n  RULE_STEP\t00:05:00  ; original\n[FUTURE]\nx y'
        document = InpDocument.from_bytes(codecs.BOM_UTF8 + text.encode())
        edited = document.replace_token(3, 1, '00:10:00')
        expected = codecs.BOM_UTF8 + text.replace('00:05:00', '00:10:00').encode()
        self.assertEqual(edited.to_bytes(), expected)
        self.assertEqual(document.to_bytes(), codecs.BOM_UTF8 + text.encode())
        self.assertEqual(edited.lines[2].tokens[1].span.column, 13)

    def test_token_edit_quotes_paths_and_reparses_positions(self):
        document = InpDocument.from_text('[TIMESERIES]\nTs FILE old.dat ; retain\n')
        edited = document.replace_token(2, 2, '新目录\\rain data.txt')
        self.assertEqual(edited.lines[1].values[2], '新目录\\rain data.txt')
        self.assertEqual(edited.lines[1].comment, '; retain')
        self.assertIn('"新目录\\rain data.txt"', edited.text)

    def test_patch_multiple_edits_and_creation_from_empty(self):
        document = InpDocument.from_text('[OPTIONS]\nRULE_STEP 0\nROUTING_STEP 1\n')
        first, second = document.lines[1].tokens[1], document.lines[2].tokens[1]
        edited = document.apply(document.patch([
            TextEdit(start=second.start, end=second.end, replacement='0.25'),
            TextEdit(start=first.start, end=first.end, replacement='00:05:00'),
        ]))
        self.assertEqual(edited.lines[1].values[1], '00:05:00')
        self.assertEqual(edited.lines[2].values[1], '0.25')
        empty = InpDocument.from_text('')
        created = empty.apply(empty.patch([TextEdit(start=0, end=0, replacement=edited.text)]))
        self.assertEqual(created.to_bytes(), edited.to_bytes())

    def test_stale_patch_is_rejected(self):
        document = InpDocument.from_text('[OPTIONS]\nRULE_STEP 0\n')
        token = document.lines[1].tokens[1]
        patch_value = document.patch([TextEdit(start=token.start, end=token.end, replacement='1')])
        edited = document.apply(patch_value)
        with self.assertRaises(PatchConflictError):
            edited.apply(patch_value)

    def test_encoding_interpretation_is_part_of_patch_revision(self):
        data = '[TITLE]\né\n'.encode()
        utf8 = InpDocument.from_bytes(data)
        ansi = InpDocument.from_bytes(data, encoding='cp1252')
        with self.assertRaises(PatchConflictError):
            ansi.apply(utf8.patch([TextEdit(start=0, end=0, replacement='; new\n')]))

    def test_invalid_patches_do_not_mutate_document(self):
        document = InpDocument.from_text('[TITLE]\ntext\n')
        for edits in (
            [TextEdit(start=0, end=5, replacement=''), TextEdit(start=4, end=6, replacement='')],
            [TextEdit(start=0, end=0, replacement='a'), TextEdit(start=0, end=0, replacement='b')],
            [TextEdit(start=0, end=100, replacement='')],
        ):
            with self.subTest(edits=edits), self.assertRaises(PatchConflictError):
                document.apply(document.patch(edits))
        self.assertEqual(document.text, '[TITLE]\ntext\n')

    def test_noop_patch_preserves_original_bytes(self):
        document = InpDocument.from_bytes(b'[TITLE]\ncaf\xe9\n', encoding='cp1252')
        self.assertIs(document.apply(document.patch([])), document)
        self.assertIs(document.apply(document.patch([
            TextEdit(start=0, end=len(document.text), replacement=document.text)
        ])), document)

    def test_replacement_that_cannot_be_encoded_fails_before_mutation(self):
        document = InpDocument.from_bytes(b'[OPTIONS]\nWORD value\n', encoding='ascii')
        with self.assertRaises(UnicodeEncodeError):
            document.replace_token(2, 1, '中文')
        self.assertEqual(document.to_bytes(), b'[OPTIONS]\nWORD value\n')

    def test_edit_positions_are_not_negative_python_indexes(self):
        document = InpDocument.from_text('[OPTIONS]\nRULE_STEP 0\n')
        for line, token in ((0, 0), (-1, 0), (3, 0), (2, -1), (2, 5)):
            with self.subTest(line=line, token=token), self.assertRaises(IndexError):
                document.replace_token(line, token, 'value')

    def test_format_token_has_no_shell_escape_behavior(self):
        self.assertEqual(format_token(''), '""')
        self.assertEqual(format_token('a b'), '"a b"')
        self.assertEqual(format_token('[North]'), '"[North]"')
        self.assertEqual(format_token('D:\\new\\rain.txt'), 'D:\\new\\rain.txt')
        for value in ('a;b', 'a"b', 'a\nb', '\r', '\x00'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                format_token(value)

    def test_write_and_read_keep_original_bytes(self):
        source = codecs.BOM_UTF8 + '[TITLE]\r\n模型\n[FUTURE]\rX Y'.encode()
        document = InpDocument.from_bytes(source)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / '子目录' / 'result.inp'
            self.assertEqual(document.write(target), target)
            self.assertEqual(InpDocument.read(target).to_bytes(), source)
            self.assertEqual(list(target.parent.iterdir()), [target])

    def test_failed_replace_does_not_destroy_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'existing.inp'
            target.write_bytes(b'valuable original')
            with patch('easysewer.io.inp.document.os.replace', side_effect=OSError('replace failed')):
                with self.assertRaisesRegex(OSError, 'replace failed'):
                    InpDocument.from_text('[TITLE]\nnew\n').write(target)
            self.assertEqual(target.read_bytes(), b'valuable original')
            self.assertEqual(list(Path(directory).iterdir()), [target])

    def test_failed_flush_keeps_existing_file_and_cleans_temp(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'existing.inp'
            target.write_bytes(b'original')
            with patch('easysewer.io.inp.document.os.fsync', side_effect=OSError('flush failed')):
                with self.assertRaisesRegex(OSError, 'flush failed'):
                    InpDocument.from_text('[TITLE]\nnew').write(target)
            self.assertEqual(target.read_bytes(), b'original')
            self.assertEqual(list(Path(directory).iterdir()), [target])

    def test_write_encoding_failure_does_not_create_output_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'not-created' / 'out.inp'
            with self.assertRaises(UnicodeEncodeError):
                InpDocument.from_text('[TITLE]\n中文').write(target, encoding='ascii')
            self.assertFalse(target.parent.exists())


if __name__ == '__main__':
    unittest.main()
