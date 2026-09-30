"""Lossless line boundaries and bounded working memory for large reports."""

import gc
from dataclasses import replace
import tracemalloc
import unittest
from unittest.mock import patch
import weakref

from easysewer.io.report_document import (
    ReportDocument, SWMM_UTF8_REPORT, _lines, _matches_source, _VERIFIED_REPORTS,
)


class ReportStreamingTests(unittest.TestCase):
    def test_verified_report_avoids_reparse_but_copies_record_structure(self):
        document = ReportDocument.from_bytes(
            b'  <<< Node J >>>\n  ERROR 209: undefined object X\n', source='source.rpt')
        snapshot = _VERIFIED_REPORTS[id(document)][1]
        self.assertIsNot(snapshot, document)
        self.assertIs(snapshot.raw, document.raw)
        self.assertIs(snapshot.text, document.text)
        self.assertIsNot(snapshot.blocks[0], document.blocks[0])
        self.assertIs(snapshot.blocks[0].text, document.blocks[0].text)
        self.assertIsNot(snapshot.messages[0], document.messages[0])
        with patch.object(ReportDocument, 'from_bytes', side_effect=AssertionError('reparsed')):
            self.assertTrue(_matches_source(document))
            self.assertTrue(_matches_source(document))

    def test_directly_constructed_equal_report_requires_source_verification_once(self):
        document = replace(ReportDocument.from_bytes(b'  <<< Node J >>>\nvalue\n'))
        self.assertNotIn(id(document), _VERIFIED_REPORTS)
        with patch.object(ReportDocument, 'from_bytes', wraps=ReportDocument.from_bytes) as parse:
            self.assertTrue(_matches_source(document))
            self.assertEqual(parse.call_count, 1)
            self.assertTrue(_matches_source(document))
            self.assertEqual(parse.call_count, 1)

    def test_cached_nested_records_are_still_checked_against_original_evidence(self):
        for target in ('block', 'diagnostic', 'context', 'document'):
            with self.subTest(target=target):
                document = ReportDocument.from_bytes(
                    b'  <<< Node J >>>\n  ERROR 209: undefined object X\n')
                if target == 'block':
                    object.__setattr__(document.blocks[0], 'text', 'forged')
                elif target == 'diagnostic':
                    object.__setattr__(document.messages[0], 'message', 'forged')
                elif target == 'context':
                    object.__setattr__(document.message_contexts[0], 'raw_text', 'forged')
                else:
                    object.__setattr__(document, 'text', 'forged')
                self.assertFalse(_matches_source(document))
                from easysewer.io.report_document import ReportCapture
                from test_result_archive_v2 import failure_result
                with self.assertRaisesRegex(ValueError, 'decoding differs'):
                    replace(failure_result(), failure_report=ReportCapture(
                        document=document, truncated=False))

    def test_source_evidence_is_released_when_original_document_is_collected(self):
        document = ReportDocument.from_bytes(b'  <<< Node J >>>\nvalue\n')
        key = id(document)
        reference = weakref.ref(document)
        snapshot_reference = weakref.ref(_VERIFIED_REPORTS[key][1])
        del document
        gc.collect()
        self.assertIsNone(reference())
        self.assertNotIn(key, _VERIFIED_REPORTS)
        self.assertIsNone(snapshot_reference())

    def test_subclasses_do_not_receive_cached_parse_evidence(self):
        class DerivedReport(ReportDocument):
            pass

        document = DerivedReport.from_bytes(b'value\n')
        self.assertNotIn(id(document), _VERIFIED_REPORTS)
        self.assertFalse(_matches_source(document))

    def test_line_boundaries_match_python_text_and_byte_contracts(self):
        separators = ('\n', '\r', '\r\n', '\v', '\f', '\x1c', '\x1d',
                      '\x1e', '\x85', '\u2028', '\u2029')
        for separator in separators:
            for ending in ('', separator, separator * 2):
                text = 'prefix' + separator + '  <<< Node J >>>' + separator + 'tail' + ending
                for value in (text, text.encode('utf-8')):
                    with self.subTest(separator=repr(separator), binary=isinstance(value, bytes), ending=repr(ending)):
                        spans = list(_lines(value))
                        self.assertEqual([line for _, _, line in spans], value.splitlines(keepends=True))
                        self.assertEqual(type(value)().join(value[a:b] for a, b, _ in spans), value)
                document = ReportDocument.from_bytes(text.encode('utf-8'))
                self.assertEqual(''.join(block.text for block in document.blocks), text)
                self.assertEqual(document.blocks[1].start_line, 2)
                self.assertEqual(document.blocks[-1].end_line, len(text.splitlines()))
        self.assertEqual(list(_lines('')), [])
        self.assertEqual(list(_lines(b'')), [])

    def test_echo_consumption_and_message_locations_across_line_boundaries(self):
        text = ('preamble\r\n  ERROR 209: undefined object X at line 8 of [CONDUITS] section:\n'
                '  P X O 10 .01\r  WARNING 01: ordinary warning\n'
                '  ERROR 318: rainfall sequence error\r\n  station bad row\n'
                '  ERROR 209: another error at line 9 of input file:\n')
        document = ReportDocument.from_bytes(text.encode(), source='source.rpt')
        self.assertEqual([v.diagnostic.span.line for v in document.message_contexts], [2, 4, 5, 7])
        self.assertEqual([v.input_text for v in document.message_contexts], ['P X O 10 .01', None, 'station bad row', None])
        self.assertEqual(document.message_contexts[0].input_report_span.line, 3)
        self.assertEqual(document.message_contexts[2].input_report_span.line, 6)
        self.assertEqual(document.message_contexts[0].raw_text,
                         text.splitlines(keepends=True)[1] + text.splitlines(keepends=True)[2])

    def test_report_scanning_does_not_allocate_one_object_per_line(self):
        header = (b'  ********************\r\n  Node Flooding Summary\r\n  ********************\r\n'
                  b'  Node Flooded CMS days hr:min 10^6 ltr 1000 m\xb3\r\n'
                  b'  <<< Node J >>>\r\n')
        raw = header + b'  01/01/2020 00:05:00 1.234\r\n' * 50000
        gc.collect()
        tracemalloc.start()
        try:
            document = ReportDocument.from_bytes(raw, profile=SWMM_UTF8_REPORT)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(document.raw, raw)
        self.assertEqual(document.text.encode('utf-8'), raw.replace(b'\xb3', b'\xc2\xb3'))
        self.assertEqual(''.join(block.text for block in document.blocks), document.text)
        self.assertEqual(document.repaired_byte_offsets, (raw.index(b'\xb3'),))
        self.assertLess(peak, 4 * len(raw) + 512 * 1024)


if __name__ == '__main__':
    unittest.main()
