"""RDII and routing input frame owners, not complete solver process recovery."""
import ctypes as c
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import tempfile
import unittest

from test_native_v2_checkpoint_streams import Engine as StreamEngine, OPEN, rdii, routing
from test_native_v2_rdii_io import SOURCE, binary_header, binary_frame, text_header
from test_native_v2_routing_io import header, row
from test_native_v2_standard_io import handles


class Engine(StreamEngine):
    def __init__(self, *args):
        super().__init__(*args)
        for name, args, result in (
            ("es_test_frames_save", [c.c_void_p, c.c_size_t, c.POINTER(c.c_size_t), c.c_int], c.c_int),
            ("es_test_frames_restore", [c.c_void_p, c.c_size_t, c.c_int, OPEN, c.c_int,
                                        c.POINTER(c.c_int), c.POINTER(c.c_int)], c.c_int),
            ("es_test_frames_poison", [c.c_int], None),
            ("es_test_frames_query", [c.c_double, c.POINTER(c.c_double), c.c_size_t], c.c_int),
        ):
            f = getattr(self.lib, name); f.argtypes, f.restype = args, result

    def dump(self, only=False):
        size = c.c_size_t()
        self.check(self.lib.es_test_frames_save(None, 0, c.byref(size), only))
        data = c.create_string_buffer(size.value)
        self.check(self.lib.es_test_frames_save(data, size.value, c.byref(size), only))
        return data.raw

    def restore(self, raw, fail_at=-1, only=False):
        cleanup, committed = c.c_int(), c.c_int()
        result = self.lib.es_test_frames_restore(c.create_string_buffer(bytes(raw)), len(raw),
            fail_at, self.provider, only, c.byref(cleanup), c.byref(committed))
        self.cleanup, self.committed = cleanup.value, bool(committed.value)
        self.calls, self.hit = self.lib.es_test_tables_calls(), self.lib.es_test_tables_hit()
        return result

    def query(self, minute):
        values = (c.c_double*100)()
        used = self.lib.es_test_frames_query(43831+minute/1440, values, len(values))
        if used < 0: raise AssertionError(used)
        return list(values[:used])


def fixture(root, kind="both", units="CFS"):
    root = Path(root)
    if kind.startswith("rdii-"):
        return rdii(root, kind[5:])
    if kind == "empty-rdii":
        path = root/"empty.bin"; path.write_bytes(binary_header())
        return SOURCE+f'[FILES]\nUSE RDII "{path}"\n'
    if kind.startswith("routing-"):
        return routing(root, **{"eof": kind == "routing-eof", "ignore": kind == "routing-ignore"})
    text = rdii(root, "text") if kind == "both" else SOURCE.split('[RAINGAGES]')[0]
    text += '[POLLUTANTS]\nP MG/L 0 0 0 0\nQ UG/L 0 0 0 0\nMissing #/L 0 0 0 0\n'
    path = root/"mapped.ifc"
    values = header(nodes=("Foreign", "j", "O"), pollutants=(("Q", "UG/L"), ("Unused", "MG/L"), ("p", "MG/L")), units=units)
    factors = {"CFS": 1, "GPM": 448.831, "MGD": .64632, "CMS": .028317, "LPS": 28.317, "MLD": 2.4466}
    for minute, flow, q, p in ((0, .2, 2, 3), (3, .8, 11, 7), (7, .1, 5, 1)):
        values += row("Foreign", minute, "1e100", " 1e100 1e100 1e100")
        values += row("j", minute, str(flow*factors[units]), f" {q} 99 {p}")
        values += row("O", minute, "0", " 0 3 0")
    path.write_bytes(values.replace(b"\n", b"\r\n").rstrip())
    return text+f'[FILES]\nUSE INFLOWS "{path}"\n'


def layout(raw):
    start = raw.index(b"ESRDI001"); pos = start+8
    bindings, integers, floats, rdii_flows = [], [], [], []
    def integer(fixed=True):
        nonlocal pos
        result = struct.unpack_from('<i', raw, pos)[0]
        (bindings if fixed else integers).append(pos); pos += 4
        return result
    def text():
        nonlocal pos
        size = integer()
        if size: bindings.append(pos)
        pos += size
    def number():
        nonlocal pos
        floats.append(pos); pos += 8
    active = integer(); integer(); integer()
    if active:
        kind = integer(); integer(); count = integer()
        if kind == 1: integer()
        for _ in range(count): integer(); text()
        integer(False)
        for _ in range(3): number()
        for _ in range(count): rdii_flows.append(pos); number()
    assert raw[pos:pos+8] == b"ESIFC001"; pos += 8
    active = integer(); integer(); pollutants = integer()
    if active:
        integer(); integer(); nodes = integer(); columns = integer()
        bindings.append(pos); pos += 8
        for _ in range(columns+1): text()
        for _ in range(pollutants): text(); integer(); integer()
        for _ in range(nodes):
            text(); index = integer()
            if index >= 0: text()
        integer(False)
        for _ in range(4+2*nodes*(columns+1)): number()
    assert pos == len(raw)
    return dict(start=start, bindings=bindings, integers=integers, floats=floats, rdii_flows=rdii_flows)


@unittest.skipUnless(os.environ.get("EASYSEWER_CHECKPOINT_STANDARD") and
                     os.environ.get("EASYSEWER_CHECKPOINT_CUSTOM"),
                     "Requires RDII/routing frame checkpoint builds")
class NativeCheckpointFrameTests(unittest.TestCase):
    def engine(self, family, root, text):
        return Engine(os.environ["EASYSEWER_CHECKPOINT_"+family.upper()], root, text)

    def run_case(self, family, text, action="none"):
        with tempfile.TemporaryDirectory() as directory:
            e = self.engine(family, directory, text); history = []
            try:
                for _ in range(10000):
                    raw = e.dump(); layout(raw)
                    if action == "restore":
                        e.lib.es_test_climate_bundle_poison(); e.lib.es_test_frames_poison(0)
                        e.lib.es_test_tables_drop(); e.lib.es_test_streams_drop()
                        self.assertEqual(e.restore(raw), 0); self.assertTrue(e.committed)
                        self.assertEqual(e.cleanup, 0); self.assertEqual(e.dump(), raw)
                    elif action == "scratch": e.lib.es_test_frames_poison(1)
                    now = e.step()[0]
                    history.append((now, *(e.lib.swmm_getValue(code, 0) for code in (303, 305, 306, 407, 410))))
                    if not now: break
                else: self.fail("Frame fixture did not finish")
            finally: e.close()
            report = re.sub(rb"(?m)^[ \t]*(?:Analysis (?:begun|ended)|Total elapsed time)[^\r\n]*", b"", e.paths[1].read_bytes())
            saved = {kind: Path(path).read_bytes() for kind, path in
                     re.findall(r'(?im)^SAVE (RDII|OUTFLOWS)\s+"([^"]+)"\s*$', text)}
            return history, e.paths[2].read_bytes(), report, saved

    def test_restore_numerical_frames_and_streams_for_complete_run(self):
        kinds = ('rdii-scratch', 'rdii-save', 'rdii-binary', 'rdii-text', 'rdii-ignored',
                 'rdii-ignore-rain', 'empty-rdii', 'routing-normal', 'routing-eof', 'routing-ignore', 'both')
        for family in ('standard', 'custom'):
            with tempfile.TemporaryDirectory() as root:
                cases = [(kind, 'CFS') for kind in kinds]+[('mapped', u) for u in ('CFS','GPM','MGD','CMS','LPS','MLD')]
                for kind, units in cases:
                    with self.subTest(family=family, kind=kind, units=units):
                        text = fixture(root, kind, units)
                        self.assertEqual(self.run_case(family, text, 'restore'), self.run_case(family, text))

    def test_pending_buffers_and_completed_generation_totals_are_scratch(self):
        for family in ('standard', 'custom'):
            with tempfile.TemporaryDirectory() as root:
                for kind in ('rdii-scratch', 'rdii-binary', 'rdii-text', 'mapped', 'both'):
                    text = fixture(root, kind)
                    self.assertEqual(self.run_case(family, text, 'scratch'), self.run_case(family, text))

    def test_last_frame_block_corruption_rejects_before_resource_staging(self):
        for family in ('standard', 'custom'):
            with tempfile.TemporaryDirectory() as root:
                e = self.engine(family, root, fixture(root))
                try:
                    for _ in range(7): e.step()
                    raw = e.dump(); fields = layout(raw); before = handles()
                    def reject(bad):
                        calls = e.provider_calls
                        self.assertNotEqual(e.restore(bad), 0); self.assertFalse(e.committed)
                        self.assertEqual(e.provider_calls, calls); self.assertEqual(e.dump(), raw)
                        self.assertEqual(handles(), before)
                    for size in range(fields['start'], len(raw)): reject(raw[:size])
                    reject(raw+b'x')
                    for offset in fields['bindings']:
                        bad = bytearray(raw); bad[offset] ^= 1; reject(bad)
                    for offset in fields['integers']:
                        bad = bytearray(raw); struct.pack_into('<i', bad, offset, -1); reject(bad)
                    for offset in fields['floats']:
                        for value in (float('nan'), float('inf'), -float('inf')):
                            bad = bytearray(raw); struct.pack_into('<d', bad, offset, value); reject(bad)
                    for offset in fields['rdii_flows']:
                        for value in (.1, 1e100):
                            bad = bytearray(raw); struct.pack_into('<d', bad, offset, value); reject(bad)
                    self.assertEqual(e.restore(raw), 0)
                finally: e.close()

    def test_resource_stage_failure_preserves_current_frames(self):
        for family in ('standard', 'custom'):
            with tempfile.TemporaryDirectory() as root:
                e = self.engine(family, root, fixture(root))
                try:
                    for _ in range(20): e.step()
                    raw = e.dump(); self.assertEqual(e.restore(raw), 0); count = e.calls
                    before = handles()
                    for fail_at in range(count):
                        result = e.restore(raw, fail_at)
                        if e.committed:
                            self.assertEqual(result, 0); self.assertEqual(e.cleanup, 7)
                        else: self.assertIn(result, (6, 7))
                        self.assertEqual(e.dump(), raw); self.assertEqual(handles(), before)
                        self.assertEqual(e.restore(raw), 0)
                finally: e.close()

    def test_fresh_process_rebuilds_future_input_query_history(self):
        for family in ('standard', 'custom'):
            for kind in ('rdii-scratch', 'rdii-binary', 'rdii-text', 'mapped', 'both'):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory); text = fixture(root, kind)
                    source = root/'source.inp'; source.write_text(text)
                    first = root/'first'; second = root/'second'; first.mkdir(); second.mkdir()
                    e = self.engine(family, first, text)
                    try:
                        # Advance only input owners, through a noninitial bracket.
                        e.query(3.5); raw = e.dump(only=True); (root/'frames.bin').write_bytes(raw)
                        (root/'digests.json').write_text(json.dumps({os.fsdecode(k): hashlib.sha256(e.resources[k][1]).hexdigest() for k in e.streams.values()}))
                        minutes = [3.5, 4, 5.5, 6.99, 7, 8, 9, 10, 12]
                        expected = [e.query(t) for t in minutes]
                    finally: e.close()
                    code = """import hashlib,json,os,sys
from pathlib import Path
from test_native_v2_checkpoint_frames import Engine
from test_native_v2_checkpoint_streams import Engine as StreamEngine
library,source,saved,destination,digests=sys.argv[1:]
e=Engine(library,destination,Path(source).read_text())
try:
 assert {os.fsdecode(k):hashlib.sha256(e.resources[k][1]).hexdigest() for k in e.streams.values()}==json.loads(Path(digests).read_text())
 raw=Path(saved).read_bytes()
 assert StreamEngine.restore(e,raw[:raw.index(b'ESRDI001')],only=True)==0
 omitted=[e.query(t) for t in [3.5,4,5.5,6.99,7,8,9,10,12]]
 e.lib.es_test_streams_drop();e.lib.es_test_frames_poison(0)
 assert e.restore(raw,only=True)==0 and e.committed and e.cleanup==0
 assert e.dump(only=True)==raw
 history=[e.query(t) for t in [3.5,4,5.5,6.99,7,8,9,10,12]]
 assert history!=omitted, 'Fixture must detect omitted numerical frame state'
 Path(destination,'history.json').write_text(json.dumps(history))
finally:e.close()
"""
                    env = dict(os.environ, PYTHONPATH=os.pathsep.join((str(Path(__file__).parent), str(Path(__file__).parents[1]/'src'))))
                    p = subprocess.run([sys.executable, '-B', '-c', code,
                        os.environ['EASYSEWER_CHECKPOINT_'+family.upper()], str(source), str(root/'frames.bin'), str(second), str(root/'digests.json')],
                        env=env, capture_output=True, text=True, timeout=60)
                    self.assertEqual(p.returncode, 0, p.stdout+p.stderr)
                    self.assertEqual(json.loads((second/'history.json').read_text()), expected)
