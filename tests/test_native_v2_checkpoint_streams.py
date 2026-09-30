"""Five post-start input streams; output prefixes and private RDII/iface state
remain separate owners. Resource copies are immutable and verified by the test
coordinator, not by trusting a filename in the stream block.
"""
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

from test_native_v2_checkpoint_tables import Engine as TableEngine, OPEN
from test_native_v2_checkpoint_climate import fixture as climate
from test_native_v2_checkpoint_gage import fixture as rain
from test_native_v2_checkpoint_runoff import fixture as runoff
from test_native_v2_rdii_io import SOURCE as RDII_SOURCE, binary_header, binary_frame, text_header
from test_native_v2_routing_io import SOURCE as ROUTING_SOURCE, header as routing_header, row as routing_row
from test_native_v2_standard_io import handles


class Engine(TableEngine):
    def __init__(self, *args):
        super().__init__(*args)
        for name, args, result in (
            ("es_test_streams_save", [c.c_void_p, c.c_size_t, c.POINTER(c.c_size_t), c.c_int], c.c_int),
            ("es_test_streams_restore", [c.c_void_p, c.c_size_t, c.c_int, OPEN, c.c_int,
                                         c.POINTER(c.c_int), c.POINTER(c.c_int)], c.c_int),
            ("es_test_streams_resource", [c.c_int], c.c_char_p),
            ("es_test_streams_path", [c.c_int], c.c_char_p),
            ("es_test_streams_drop", [], None),
            ("es_test_streams_peek", [c.c_int, c.c_void_p, c.c_size_t, c.POINTER(c.c_size_t)], c.c_int),
        ):
            f = getattr(self.lib, name); f.argtypes, f.restype = args, result
        self.streams = {}
        for role in range(5):
            key = self.lib.es_test_streams_resource(role)
            if key is None: continue
            source = Path(os.fsdecode(self.lib.es_test_streams_path(role)))
            raw = source.read_bytes(); copy = self.resource_dir / ("stream-"+str(role)+".bin")
            copy.write_bytes(raw); self.resources[key] = (source, raw, copy)
            self.streams[role] = key
        self.base_provider = self.provider
        self.reject_roles = set()
        def provider(context, key, error):
            if key in self.reject_roles:
                self.provider_calls += 1; error[0] = 7; return None
            return self.base_provider(context, key, error)
        self.provider = OPEN(provider)

    def dump(self, only=False):
        size = c.c_size_t()
        self.check(self.lib.es_test_streams_save(None, 0, c.byref(size), only))
        data = c.create_string_buffer(size.value)
        self.check(self.lib.es_test_streams_save(data, size.value, c.byref(size), only))
        return data.raw

    def restore(self, raw, fail_at=-1, only=False):
        cleanup, committed = c.c_int(), c.c_int()
        result = self.lib.es_test_streams_restore(c.create_string_buffer(bytes(raw)), len(raw),
            fail_at, self.provider, only, c.byref(cleanup), c.byref(committed))
        self.cleanup, self.committed = cleanup.value, bool(committed.value)
        self.calls, self.hit = self.lib.es_test_tables_calls(), self.lib.es_test_tables_hit()
        return result

    def peek(self):
        values = {}
        for role in self.streams:
            data = c.create_string_buffer(37); used = c.c_size_t()
            self.check(self.lib.es_test_streams_peek(role, data, len(data), c.byref(used)))
            values[str(role)] = data.raw[:used.value].hex()
        return values


def layout(raw):
    start = raw.index(b"ESREAD01"); pos = start+8
    count = struct.unpack_from("<I", raw, pos)[0]; bindings = [pos]; pos += 4
    assert count == 5
    roles = []
    for index in range(count):
        role, mode, active = struct.unpack_from("<iii", raw, pos)
        bindings.extend(range(pos, pos+12, 4)); pos += 12
        assert role == index
        identity, offset = None, None
        if active:
            length = struct.unpack_from("<I", raw, pos)[0]; bindings.append(pos); pos += 4
            if length: bindings.append(pos)
            identity = raw[pos:pos+length]; pos += length
            offset = pos; pos += 8
        roles.append(dict(role=role, mode=mode, active=active, identity=identity, offset=offset))
    assert pos == len(raw)
    return dict(start=start, bindings=bindings, roles=roles)


def rdii(root, kind):
    root = Path(root); text = RDII_SOURCE
    if kind == "scratch": return text
    path = root/"rdii.bin"
    if kind == "save": return text+f'[FILES]\nSAVE RDII "{path}"\n'
    if kind == "text":
        data = text_header()+b"J 2020 1 1 0 0 0 .2\r\nJ 2020 1 1 0 3 0 .8\r\nJ 2020 1 1 0 7 0 .1"
    else:
        data = binary_header()+b"".join(binary_frame(43831+minute/1440, (value,))
                                      for minute, value in ((0, .2), (3, .8), (7, .1)))
    path.write_bytes(data); text += f'[FILES]\nUSE RDII "{path}"\n'
    if kind == "ignored": text += "[OPTIONS]\nIGNORE_RDII YES\n"
    if kind == "ignore-rain": text += "[OPTIONS]\nIGNORE_RAINFALL YES\n"
    return text


def routing(root, *, ignore=False, eof=False):
    path = Path(root)/"inflows.dat"
    path.write_bytes(routing_header()+b"".join(routing_row(minute=i, flow=str(v))
                    for i, v in ((0, .2), (3, .8), (5 if eof else 11, .1))))
    text = ROUTING_SOURCE+f'[FILES]\nUSE INFLOWS "{path}"\n'
    if ignore: text += "[OPTIONS]\nIGNORE_ROUTING YES\n"
    return text


def combined(root):
    root = Path(root)
    text = rain(root, source="rain-file", co=True, replay=True)
    weather = root/"weather.dat"
    weather.write_text("Station 2020 1 30 50 20 .1 5\nStation 2020 1 31 70 30 .3 8\n"
                       "Station 2020 2 1 40 10 .4 3\nStation 2020 2 2 60 25 .2 7")
    return text+f'[TEMPERATURE]\nFILE "{weather}"\nWINDSPEED FILE\n[EVAPORATION]\nFILE 1 1 1 1 1 1 1 1 1 1 1 1\n'


@unittest.skipUnless(os.environ.get("EASYSEWER_CHECKPOINT_STANDARD") and
                     os.environ.get("EASYSEWER_CHECKPOINT_CUSTOM"),
                     "Requires five-input-stream instrumented libraries")
class NativeCheckpointStreamTests(unittest.TestCase):
    def engine(self, family, root, text):
        return Engine(os.environ["EASYSEWER_CHECKPOINT_"+family.upper()], root, text)

    def run_case(self, family, text, restore=False):
        with tempfile.TemporaryDirectory() as root:
            e = self.engine(family, root, text); history = []
            try:
                for index in range(10000):
                    raw = e.dump(); layout(raw)
                    if restore:
                        e.lib.es_test_climate_bundle_poison()
                        e.lib.es_test_tables_drop(); e.lib.es_test_streams_drop()
                        self.assertEqual(e.restore(raw), 0)
                        self.assertTrue(e.committed); self.assertEqual(e.cleanup, 0)
                        self.assertEqual(e.dump(), raw)
                    now = e.step()[0]
                    history.append((now, *(e.lib.swmm_getValue(code, 0) for code in (303, 305, 306, 407, 410))))
                    if not now: break
                else: self.fail("Stream fixture did not finish")
            finally: e.close()
            report = re.sub(rb"(?m)^[ \t]*(?:Analysis (?:begun|ended)|Total elapsed time)[^\r\n]*", b"", e.paths[1].read_bytes())
            saved = {kind: Path(path).read_bytes() for kind, path in
                     re.findall(r'(?im)^SAVE (RAINFALL|RDII|RUNOFF)\s+"([^"]+)"\s*$', text)}
            return history, e.paths[2].read_bytes(), report, saved

    def test_all_five_roles_restore_after_closing_streams(self):
        for family in ("standard", "custom"):
            with tempfile.TemporaryDirectory() as root:
                makers = [lambda f=f: climate(root, format=f) for f in ("USER", "GHCND", "TD3200", "DLY0204")]
                makers += [lambda: climate(root, leap=True), lambda: climate(root, eof=True),
                           lambda: climate(root, missing=True),
                           lambda: climate(root, temperature="series", file_series=True),
                           lambda: rain(root, source="rain-file", co=True),
                           lambda: rain(root, source="rain-file", co=True)+f'[FILES]\nSAVE RAINFALL "{Path(root)/"rain.bin"}"\n',
                           lambda: rain(root, source="timeseries-file", replay=True),
                           lambda: runoff(root, save=True), lambda: combined(root)]
                makers += [lambda kind=kind: rdii(root, kind) for kind in ("scratch", "save", "binary", "text", "ignored", "ignore-rain")]
                makers += [lambda: routing(root), lambda: routing(root, eof=True), lambda: routing(root, ignore=True)]
                for index, make in enumerate(makers):
                    with self.subTest(family=family, case=index):
                        text = make()
                        self.assertEqual(self.run_case(family, text, True), self.run_case(family, text))
                # SAVE has completed its writes before start returns; reuse the
                # same captured binary rainfall as a caller-owned USE resource.
                text = rain(root, source="rain-file", co=True)+f'[FILES]\nUSE RAINFALL "{Path(root)/"rain.bin"}"\n'
                self.assertEqual(self.run_case(family, text, True), self.run_case(family, text))

    def test_faults_and_changed_resources_leave_all_owners_intact(self):
        seen = set(); cleaned = 0
        for family, kind in ((f, k) for f in ("standard", "custom")
                             for k in ("combined", "rdii", "routing")):
            with tempfile.TemporaryDirectory() as root:
                text = combined(root) if kind == "combined" else rdii(root, "text") if kind == "rdii" else routing(root)
                e = self.engine(family, root, text)
                try:
                    for _ in range(7): e.step()
                    raw = e.dump(); self.assertEqual(e.restore(raw), 0); count = e.calls
                    before = handles()
                    for fail_at in range(count):
                        result = e.restore(raw, fail_at); seen.add(e.hit)
                        if e.committed:
                            self.assertEqual(result, 0); self.assertEqual(e.cleanup, 7); cleaned += 1
                        else: self.assertIn(result, (6, 7))
                        self.assertEqual(e.dump(), raw); self.assertEqual(handles(), before)
                        self.assertEqual(e.restore(raw), 0)
                    for key in e.streams.values():
                        e.reject_roles = {key}
                        self.assertEqual(e.restore(raw), 7); self.assertFalse(e.committed)
                        self.assertEqual(e.dump(), raw); self.assertEqual(handles(), before)
                        e.reject_roles.clear()
                        source, original, copy = e.resources[key]
                        source.write_bytes(original+b"x")
                        try:
                            self.assertEqual(e.restore(raw), 3); self.assertFalse(e.committed)
                            self.assertEqual(e.dump(), raw); self.assertEqual(handles(), before)
                        finally: source.write_bytes(original)
                        self.assertEqual(e.restore(raw), 0)
                finally: e.close()
        self.assertEqual(seen, {1, 2, 3, 4, 5}); self.assertGreater(cleaned, 0)

    def test_corruption_is_rejected_before_apply(self):
        for family in ("standard", "custom"):
            with tempfile.TemporaryDirectory() as root:
                e = self.engine(family, root, combined(root))
                try:
                    raw = e.dump(); fields = layout(raw); before = handles()
                    def reject(bad, staged=False):
                        calls = e.provider_calls
                        self.assertNotEqual(e.restore(bad), 0); self.assertFalse(e.committed)
                        self.assertEqual(e.dump(), raw); self.assertEqual(handles(), before)
                        if not staged: self.assertEqual(e.provider_calls, calls)
                    for size in range(fields["start"], len(raw)): reject(raw[:size])
                    reject(raw+b"x")
                    for offset in fields["bindings"]:
                        bad = bytearray(raw); bad[offset] ^= 1; reject(bad)
                    for role in fields["roles"]:
                        if not role["active"]: continue
                        for value in (2**64-1, 2**40):
                            bad = bytearray(raw); struct.pack_into("<Q", bad, role["offset"], value)
                            reject(bad, staged=value==2**40)
                finally: e.close()

    def test_scratch_identity_and_positions_rebuild_in_fresh_process(self):
        for family in ("standard", "custom"):
            for kind in ("combined", "rdii", "routing"):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory); source = root/"fixture.inp"; saved = root/"streams.bin"
                    text = combined(root) if kind == "combined" else rdii(root, "scratch") if kind == "rdii" else routing(root)
                    source.write_text(text); first = root/"first"; second = root/"second"; first.mkdir(); second.mkdir()
                    e = self.engine(family, first, text)
                    try:
                        for _ in range(7): e.step()
                        raw = e.dump(only=True); saved.write_bytes(raw); expected = e.peek()
                        digests = {os.fsdecode(key): hashlib.sha256(e.resources[key][1]).hexdigest() for key in e.streams.values()}
                    finally: e.close()
                    (root/"digests.json").write_text(json.dumps(digests))
                    code = """import hashlib,json,os,sys
from pathlib import Path
from test_native_v2_checkpoint_streams import Engine
library,source,saved,destination,digests=sys.argv[1:]
e=Engine(library,destination,Path(source).read_text())
try:
 expected=json.loads(Path(digests).read_text())
 assert {os.fsdecode(k):hashlib.sha256(e.resources[k][1]).hexdigest() for k in e.streams.values()}==expected
 raw=Path(saved).read_bytes();e.lib.es_test_streams_drop()
 assert e.restore(raw,only=True)==0 and e.committed and e.cleanup==0
 assert e.dump(only=True)==raw
 Path(destination,'peek.json').write_text(json.dumps(e.peek()))
finally:e.close()
"""
                    env = dict(os.environ, PYTHONPATH=os.pathsep.join((str(Path(__file__).parent), str(Path(__file__).parents[1]/"src"))))
                    result = subprocess.run([sys.executable, "-B", "-c", code,
                        os.environ["EASYSEWER_CHECKPOINT_"+family.upper()], str(source), str(saved), str(second),
                        str(root/"digests.json")], env=env, capture_output=True, text=True, timeout=60)
                    self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
                    self.assertEqual(json.loads((second/"peek.json").read_text()), expected)
