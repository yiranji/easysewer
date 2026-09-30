"""Record cancellation retains wire bytes, strict validation and ownership."""
from collections import OrderedDict, defaultdict, namedtuple
from dataclasses import asdict, dataclass
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from easysewer.io import _record_work as rw
from easysewer.io.json import JsonDocument
from easysewer.io.json import document as jd
from easysewer.io.report import read_report_tables
from easysewer.io.report_document import ReportDocument
from easysewer.results import tables
from easysewer.validation import ValidationError
from easysewer.validation._cooperative import checkpoint_scope
from test_report_cancel_v2 import large_depth


@dataclass
class Record:
    value: object


class RecordCancellationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.table, = read_report_tables(ReportDocument.from_bytes(large_depth(1200)), ('swmm:node_depth',))

    def test_table_versions_noop_bytes_and_roundtrip(self):
        for version in ('1.1', '1.2'):
            expected = self.table.to_json_document(version=version)
            calls = []
            actual = self.table.to_json_document(version=version, checkpoint=lambda: calls.append(1))
            self.assertEqual(actual.to_bytes(), expected.to_bytes())
            self.assertEqual(tables.ResultTable.from_json_document(actual), self.table)
            self.assertGreater(len(calls), 5)
        with self.assertRaises(TypeError):
            self.table.to_json_document(checkpoint=1)
        with self.assertRaises(ValueError):
            self.table.to_json_document(version='future')

    def test_interrupt_clone_encode_parse_and_validation_keeps_exact_exception(self):
        for module, name in ((tables, 'record_asdict'), (tables, 'record_dumps'),
                             (jd, '_pairs'), (jd, 'check_json')):
            for kind in (ValueError, OSError, UnicodeError, RecursionError):
                with self.subTest(phase=name, error=kind):
                    active = []; calls = []; failure = kind('caller interrupted ' + name)
                    original = getattr(module, name)
                    def entered(*args, **kwargs):
                        active.append(True)
                        try: return original(*args, **kwargs)
                        finally: active.pop()
                    def check():
                        if active:
                            calls.append(1)
                            if len(calls) == 4: raise failure
                    with patch.object(module, name, entered), self.assertRaises(kind) as caught:
                        self.table.to_json_document(checkpoint=check)
                    self.assertIs(caught.exception, failure)
                    self.assertEqual(len(calls), 4)
        self.assertEqual(self.table.to_json_document().data['kind'], 'easysewer:result-table')

    def test_strict_json_rejections_and_locations_are_unchanged(self):
        inputs = ['{"x":1,"x":2}', '{"x":NaN}', '{"x":9007199254740992}',
                  '{"x":}', '[' * 130 + '0' + ']' * 130]
        for text in inputs:
            with self.subTest(text=text[:30]):
                with self.assertRaises(ValidationError) as expected:
                    JsonDocument.from_text(text, source='input.json')
                with checkpoint_scope(lambda: None), self.assertRaises(ValidationError) as actual:
                    JsonDocument.from_text(text, source='input.json')
                self.assertEqual(actual.exception.report, expected.exception.report)

    def test_nested_container_dataclass_conversion_matches_current_interpreter(self):
        Point = namedtuple('Point', 'x y')
        class ListChild(list): pass
        class DictChild(dict): pass
        values = [Point(Record(1), 2), ListChild([Record(1)]), DictChild(a=Record(1)),
                  defaultdict(list, a=Record(1)), OrderedDict(a=Record(1))]
        for value in values:
            with self.subTest(container=type(value).__name__):
                try: expected = asdict(Record(value))
                except Exception as error:
                    with checkpoint_scope(lambda: None), self.assertRaises(type(error)) as actual:
                        rw.record_asdict(Record(value))
                    self.assertEqual(str(actual.exception), str(error))
                else:
                    with checkpoint_scope(lambda: None): actual = rw.record_asdict(Record(value))
                    self.assertEqual(actual, expected)

    def test_large_primitive_container_clone_is_interruptible_and_independent(self):
        original = Record(list(range(10000))); seen = []; failure = InterruptedError('clone')
        def check():
            seen.append(1)
            if len(seen) == 6: raise failure
        with self.assertRaises(InterruptedError) as caught:
            with checkpoint_scope(check): rw.record_asdict(original)
        self.assertIs(caught.exception, failure)
        self.assertEqual(original.value, list(range(10000)))
        with checkpoint_scope(lambda: None): result = rw.record_asdict(original)
        result['value'].append('changed')
        self.assertEqual(len(original.value), 10000)

    def test_encoder_large_format_keys_shared_values_and_rejections(self):
        shared = {'text': '中文\n"\\\u2028', 'values': (None, True, -0.0, 1e20)}
        values = [[shared] * 800, {i: shared for i in range(700)}, {'nested': [[], {}] * 800}]
        for value in values:
            with checkpoint_scope(lambda: None): actual = rw.record_dumps(value)
            self.assertEqual(actual, json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2))
        cycle = []; cycle.append(cycle)
        for value in (cycle, {'rows': list(range(800)), 'invalid': float('nan')}, {(): 1}):
            with self.assertRaises((ValueError, TypeError)) as expected:
                json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2)
            with checkpoint_scope(lambda: None), self.assertRaises(type(expected.exception)):
                rw.record_dumps(value)

    def test_private_write_interrupt_closes_and_short_write_is_an_error(self):
        streams = []; payload = b'x' * 200000
        class Stream(io.BytesIO):
            def close(self):
                self.saved = self.getvalue()
                super().close()
        def opened(*args, **kwargs):
            stream = Stream(); streams.append(stream); return stream
        failure = OSError('stop write')
        def check():
            if streams and not streams[-1].closed and streams[-1].tell() >= 65536: raise failure
        with patch.object(Path, 'open', opened), self.assertRaises(OSError) as caught:
            with checkpoint_scope(check): rw.write_record_bytes(Path('private.json'), payload)
        self.assertIs(caught.exception, failure)
        self.assertTrue(streams[0].closed); self.assertEqual(streams[0].saved, payload[:65536])
        class Short(Stream):
            def write(self, data): return super().write(data[:10])
        short = Short()
        with patch.object(Path, 'open', return_value=short), self.assertRaisesRegex(OSError, 'Short write'):
            rw.write_record_bytes(Path('private.json'), payload)
        self.assertTrue(short.closed)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'record.json'
            with checkpoint_scope(lambda: None): rw.write_record_bytes(path, payload)
            self.assertEqual(path.read_bytes(), payload)

    def test_nested_scope_restores_original_exception_chain(self):
        cause = RuntimeError('cause'); failure = OSError('stop')
        failure.__cause__ = cause
        calls = []
        def check():
            calls.append(1)
            if len(calls) == 8: raise failure
        with self.assertRaises(OSError) as caught:
            with checkpoint_scope(check): self.table.to_json_document()
        self.assertIs(caught.exception, failure); self.assertIs(caught.exception.__cause__, cause)
        self.assertTrue(JsonDocument.from_data({'normal': True}).data['normal'])


if __name__ == '__main__': unittest.main()
