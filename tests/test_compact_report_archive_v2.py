"""Realistic report sizes, lossless reconstruction and strict version boundaries."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from easysewer.io.report_document import ReportCapture, ReportDocument, SWMM_UTF8_REPORT
from easysewer.runtime import RunResult
from easysewer.runtime._result_codec import Codec
from easysewer.runtime.archive import _Blobs
from test_result_archive_v2 import failure_result


def document():
    return ReportDocument.from_bytes(
        b'  ********************\r\n  Node Flooding Summary\r\n  ********************\r\n'
        b'  Node Flooded CMS days hr:min 10^6 ltr 1000 m\xb3\r\n'
        b'  <<< Node J >>>\r\n  01/01/2020 00:05:00 1.234\r\n'
        b'  ERROR 209: undefined object X at line 8 of [CONDUITS] section:\r\n'
        b'  P X O 10 0.01\r\n', source='original/池.rpt', profile=SWMM_UTF8_REPORT)


class CompactReportArchiveTests(unittest.TestCase):
    def test_actual_1_2_writer_fixture_migrates_without_losing_report_evidence(self):
        fixture = Path(__file__).parent/'fixtures/report_archive_1_2'
        manifest = json.loads((fixture/'manifest.json').read_bytes())
        for name, digest in manifest['files'].items():
            self.assertEqual(hashlib.sha256((fixture/name).read_bytes()).hexdigest(), digest)
        expected = replace(failure_result(), failure_report=ReportCapture(document=document(), truncated=True))
        loaded = RunResult.load(fixture/'result')
        self.assertEqual(loaded, expected)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'upgraded'; loaded.save(path)
            self.assertEqual(json.loads((path/'result.json').read_bytes())['schema_version'], '1.3')
            self.assertEqual(RunResult.load(path), expected)

    def test_report_size_no_longer_doubles_the_manifest_budget(self):
        raw = b'  <<< Node J >>>\r\n' + b'  01/01/2020 00:05:00 1.234\r\n' * 50000
        doc = ReportDocument.from_bytes(raw, source='large.rpt')
        value = replace(failure_result(), failure_report=ReportCapture(document=doc, truncated=True))
        budget = len(raw) + 128 * 1024
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch('ctypes.CDLL', side_effect=AssertionError('No native library during archival')):
                value.save(root/'saved', max_manifest_bytes=budget)
                (root/'saved').rename(root/'moved')
                loaded = RunResult.load(root/'moved', max_manifest_bytes=budget)
            self.assertEqual(loaded, value)
            manifest = (root/'moved/result.json').read_bytes()
            self.assertLess(len(manifest), 16000)
            self.assertEqual(json.loads(manifest)['schema_version'], '1.3')
            self.assertNotIn(b'00:05:00', manifest)
            self.assertEqual((root/'moved/blobs'/hashlib.sha256(raw).hexdigest()).read_bytes(), raw)
            with self.assertRaisesRegex(ValueError, 'max_manifest_bytes'):
                RunResult.load(root/'moved', max_manifest_bytes=len(raw)-1)
            with self.assertRaisesRegex(ValueError, 'max_manifest_bytes'):
                value.save(root/'limited', max_manifest_bytes=len(raw)-1)
            self.assertFalse((root/'limited').exists())

    def test_all_decoding_forms_blocks_and_message_contexts_survive(self):
        docs = [document(), ReportDocument.from_bytes(b'bad \xff', source='unknown.rpt'),
                ReportDocument.from_bytes('café\r\n'.encode('cp1252'), encoding='cp1252'),
                ReportDocument.from_bytes(b'\xef\xbb\xbfUTF8 \xce\xb1\n'), ReportDocument.from_bytes(b'')]
        self.assertEqual(docs[0].decoding_strategy, 'swmm-volume-header-normalization')
        self.assertEqual(docs[0].message_contexts[0].input_line, 8)
        for doc in docs:
            with self.subTest(encoding=doc.encoding), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); value = replace(failure_result(),
                    failure_report=ReportCapture(document=doc, truncated=False))
                value.save(root/'one'); loaded = RunResult.load(root/'one')
                self.assertEqual(loaded, value)
                loaded.save(root/'two')
                self.assertEqual((root/'one/result.json').read_bytes(), (root/'two/result.json').read_bytes())

    def test_forged_decoded_evidence_is_rejected_at_construction_and_decode(self):
        doc = document()
        changes = [dict(text=doc.text+'invented'), dict(encoding='cp1252'),
                   dict(repaired_byte_offsets=()), dict(messages=()), dict(message_contexts=()),
                   dict(blocks=(replace(doc.blocks[0], title='Invented'),)+doc.blocks[1:]),
                   dict(blocks=(replace(doc.blocks[0], text='Invented'),)+doc.blocks[1:])]
        for change in changes:
            with self.subTest(change=tuple(change)), tempfile.TemporaryDirectory() as directory:
                forged = replace(doc, **change)
                with self.assertRaisesRegex(ValueError, 'decoding differs'):
                    replace(failure_result(), failure_report=ReportCapture(document=forged, truncated=False))
                codec = Codec(_Blobs(Path(directory), 8*1024**3), result_version='1.3')
                encoded = codec.encode(forged)
                with self.assertRaisesRegex(ValueError, 'decoding differs'):
                    codec.decode(encoded)

    def test_compact_wire_cannot_be_tampered_or_smuggled_into_older_formats(self):
        for mode in ('digest', 'version', 'encoding', 'source', 'extra', 'missing', 'raw-type', 'old-envelope'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                path = Path(directory)/'saved'
                value = replace(failure_result(), failure_report=ReportCapture(document=document(), truncated=True))
                value.save(path); manifest = path/'result.json'; data = json.loads(manifest.read_bytes())
                report = data['result']['fields']['failure_report']['fields']['document']
                if mode=='digest': report['decoded_sha256']='0'*64
                elif mode=='version': report['version']='2.0'
                elif mode=='encoding': report['requested_encoding']=23
                elif mode=='source': report['source']='different.rpt'
                elif mode=='extra': report['text']='invented'
                elif mode=='missing': del report['decoded_sha256']
                elif mode=='raw-type': report['raw']='not bytes'
                elif mode=='old-envelope': data['schema_version']='1.2'
                manifest.write_text(json.dumps(data), encoding='utf-8')
                with self.assertRaises((TypeError, ValueError)): RunResult.load(path)

    def test_legacy_report_codec_remains_lossless_and_rejects_new_wire(self):
        with tempfile.TemporaryDirectory() as directory:
            blobs = _Blobs(Path(directory), 8*1024**3)
            doc = document(); compact = Codec(blobs, result_version='1.3').encode(doc)
            for version in ('1.0', '1.1', '1.2'):
                with self.subTest(version=version):
                    codec = Codec(blobs, result_version=version)
                    encoded = codec.encode(doc)
                    self.assertEqual(encoded['type'], 'report:document')
                    self.assertEqual(codec.decode(encoded), doc)
                    if version == '1.2':
                        self.assertEqual(Codec(blobs, result_version='1.3').decode(encoded), doc)
                    with self.assertRaisesRegex(ValueError, 'Legacy result codec'):
                        codec.decode(compact)


if __name__ == '__main__': unittest.main()
