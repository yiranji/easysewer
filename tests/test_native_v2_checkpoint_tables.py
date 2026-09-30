"""Table/stream transactions, including destroyed-owner reconstruction.

The test resource provider verifies captured bytes and opens independent immutable
copies. Production resource/container coordination is still a separate owner.
"""
import ctypes as c
from datetime import datetime
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import tempfile
import unittest

from test_native_v2_checkpoint_climate import Engine as ClimateEngine, fixture
from test_native_v2_checkpoint_controls import fixture as controls
from test_native_v2_checkpoint_gage import fixture as rainfall
from test_native_v2_standard_io import handles

OPEN = c.CFUNCTYPE(c.c_void_p, c.c_void_p, c.c_char_p, c.POINTER(c.c_int))


class Engine(ClimateEngine):
    def __init__(self, *args):
        super().__init__(*args)
        for name, args, result in (
            ("es_test_tables_save", [c.c_void_p, c.c_size_t, c.POINTER(c.c_size_t), c.c_int], c.c_int),
            ("es_test_tables_restore", [c.c_void_p, c.c_size_t, c.c_int, OPEN, c.c_int,
                                        c.POINTER(c.c_int), c.POINTER(c.c_int)], c.c_int),
            ("es_test_tables_resource", [c.c_int], c.c_char_p),
            ("es_test_tables_open_read", [c.c_char_p, c.POINTER(c.c_int)], c.c_void_p),
            ("es_test_tables_drop", [], None),
            ("es_test_tables_calls", [], c.c_int),
            ("es_test_tables_hit", [], c.c_int),
            ("es_test_tables_lookup", [c.c_int, c.c_double, c.c_int], c.c_double),
            ("es_test_tables_next", [c.c_int, c.POINTER(c.c_double), c.POINTER(c.c_double)], c.c_int),
        ):
            f = getattr(self.lib, name); f.argtypes, f.restype = args, result
        self.resources = {}
        self.resource_dir = self.root / "captured-resources"
        self.resource_dir.mkdir()
        for index in range(10000):
            name = self.lib.es_test_tables_resource(index)
            if name is None: break
            if not name or name in self.resources: continue
            source = Path(os.fsdecode(name)); raw = source.read_bytes()
            copy = self.resource_dir / (str(index)+".bin"); copy.write_bytes(raw)
            self.resources[name] = (source, raw, copy)
        else: raise AssertionError("Unbounded root inventory")
        self.root_count = index
        self.reject_resources = False
        self.provider_calls = 0
        def provider(context, name, error):
            self.provider_calls += 1
            try:
                source, raw, copy = self.resources[name]
                if self.reject_resources:
                    error[0] = 7; return None
                if source.read_bytes() != raw:
                    error[0] = 3; return None
                return self.lib.es_test_tables_open_read(os.fsencode(copy), error)
            except (OSError, KeyError):
                error[0] = 7; return None
        self.provider = OPEN(provider)

    def dump(self, only=False):
        size = c.c_size_t()
        self.check(self.lib.es_test_tables_save(None, 0, c.byref(size), only))
        data = c.create_string_buffer(size.value)
        self.check(self.lib.es_test_tables_save(data, size.value, c.byref(size), only))
        return data.raw

    def restore(self, raw, fail_at=-1, only=False):
        cleanup, committed = c.c_int(), c.c_int()
        result = self.lib.es_test_tables_restore(c.create_string_buffer(bytes(raw)), len(raw),
            fail_at, self.provider, only, c.byref(cleanup), c.byref(committed))
        self.cleanup, self.committed = cleanup.value, bool(committed.value)
        self.calls, self.hit = self.lib.es_test_tables_calls(), self.lib.es_test_tables_hit()
        return result


def layout(raw):
    """Independent wire reader used for bounded corruption tests."""
    start = raw.index(b"ESTBL001"); pos = start+8
    bindings, integers, floats, positions, cursors = [], [], [], [], []
    def integer(fixed=False):
        nonlocal pos
        value = struct.unpack_from("<I", raw, pos)[0]
        (bindings if fixed else integers).append(pos); pos += 4
        return value
    def text():
        nonlocal pos
        size = integer(True)
        if size: bindings.append(pos)
        pos += size
    def number(fixed=False):
        nonlocal pos
        (bindings if fixed else floats).append(pos); pos += 8
    def cursor(count):
        nonlocal pos
        begin = pos; ordinal = integer()
        for _ in range(5): number()
        for _ in range(3): integer()
        number(); opened = integer()
        if opened: positions.append(pos); pos += 8
        child = integer()
        cursors.append(dict(begin=begin, ordinal=ordinal, count=count, opened=opened, child=child))
        if child: cursor(count)
    def owner():
        text(); integer(True); integer(True); number(True); integer(True); text()
        count = integer(True)
        for _ in range(count): number(True); number(True)
        cursor(count)
    curves, series = integer(True), integer(True)
    for _ in range(curves+series): owner()
    for _ in range(2):
        if integer(True): owner()
    assert pos == len(raw), (pos, len(raw))
    return dict(start=start, bindings=bindings, integers=integers, floats=floats,
                positions=positions, cursors=cursors)


@unittest.skipUnless(os.environ.get("EASYSEWER_CHECKPOINT_STANDARD") and
                     os.environ.get("EASYSEWER_CHECKPOINT_CUSTOM"),
                     "Requires staged-table instrumented libraries")
class NativeCheckpointTableTests(unittest.TestCase):
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
                        e.lib.es_test_tables_drop()
                        self.assertEqual(e.restore(raw), 0)
                        self.assertTrue(e.committed); self.assertEqual(e.cleanup, 0)
                        self.assertEqual(e.dump(), raw)
                    now = e.step()[0]
                    history.append((now, *(e.lib.swmm_getValue(code, 0) for code in (303, 305, 306, 407, 410)),
                                    *(e.lib.es_test_climate_observe(k) for k in range(27))))
                    if not now: break
                else: self.fail("Table fixture did not finish")
            finally: e.close()
            report = re.sub(rb"(?m)^[ \t]*(?:Analysis (?:begun|ended)|Total elapsed time)[^\r\n]*", b"", e.paths[1].read_bytes())
            return history, e.paths[2].read_bytes(), report

    def test_destroyed_cursors_and_streams_restore_full_results(self):
        cases = [dict(evaporation=kind, temperature="series", file_series=external)
                 for kind in ("CONSTANT", "MONTHLY", "TIMESERIES", "FILE")
                 for external in (False, True)]
        cases += [dict(evaporation="TIMESERIES", temperature="series", file_series=True, shared=True),
                  dict(evaporation="TIMESERIES", temperature="series", file_series=True, days=12),
                  dict(evaporation="TIMESERIES", temperature="series", file_series=True, units="CMS"),
                  dict(evaporation="TEMPERATURE", days=10)]
        for family in ("standard", "custom"):
            for options in cases:
                with self.subTest(family=family, **options), tempfile.TemporaryDirectory() as root:
                    text = fixture(root, **options)
                    self.assertEqual(self.run_case(family, text, True), self.run_case(family, text))
            self.assertEqual(self.run_case(family, controls(), True), self.run_case(family, controls()))

    def test_every_stage_fault_preserves_session_and_reports_commit_cleanup(self):
        kinds = set(); committed_cleanup = 0
        for family in ("standard", "custom"):
            with tempfile.TemporaryDirectory() as root:
                text = fixture(root, evaporation="TIMESERIES", temperature="series", file_series=True, shared=True)
                e = self.engine(family, root, text)
                try:
                    for _ in range(35): e.step()
                    raw = e.dump()
                    self.assertEqual(e.restore(raw), 0); count = e.calls
                    initial_handles = handles()
                    self.assertGreater(count, 10)
                    for fail_at in range(count):
                        error = e.restore(raw, fail_at); kinds.add(e.hit)
                        if e.committed:
                            self.assertEqual(error, 0); self.assertEqual(e.cleanup, 7)
                            committed_cleanup += 1
                        else: self.assertIn(error, (6, 7))
                        self.assertEqual(e.dump(), raw)
                        self.assertEqual(e.restore(raw), 0); self.assertEqual(e.cleanup, 0)
                        self.assertEqual(handles(), initial_handles)
                    e.reject_resources = True
                    self.assertEqual(e.restore(raw), 7); self.assertFalse(e.committed)
                    self.assertEqual(e.dump(), raw)
                    e.reject_resources = False
                    source, original, copy = next(iter(e.resources.values()))
                    source.write_bytes(original+b"\n1 999\n")
                    try:
                        self.assertEqual(e.restore(raw), 3); self.assertFalse(e.committed)
                        self.assertEqual(e.dump(), raw)
                    finally: source.write_bytes(original)
                    self.assertEqual(e.restore(raw), 0)
                finally: e.close()
        self.assertEqual(kinds, {1, 2, 3, 4, 5})
        self.assertGreater(committed_cleanup, 0)

    def test_corruption_rejected_before_resource_staging_or_numeric_apply(self):
        for family in ("standard", "custom"):
            with tempfile.TemporaryDirectory() as root:
                e = self.engine(family, root, fixture(root, evaporation="TIMESERIES",
                                temperature="series", file_series=True))
                try:
                    for _ in range(35): e.step()
                    raw = e.dump(); fields = layout(raw)
                    def reject(bad, stage=False):
                        calls = e.provider_calls
                        self.assertNotEqual(e.restore(bad), 0); self.assertFalse(e.committed)
                        self.assertEqual(e.dump(), raw)
                        if not stage: self.assertEqual(e.provider_calls, calls)
                    for size in range(fields["start"], len(raw)): reject(raw[:size])
                    reject(raw+b"x")
                    for offset in fields["bindings"]:
                        bad = bytearray(raw); bad[offset] ^= 1; reject(bad)
                    for offset in fields["floats"]:
                        for value in (float("nan"), float("inf"), -float("inf")):
                            bad = bytearray(raw); struct.pack_into("<d", bad, offset, value); reject(bad)
                    for offset in fields["integers"]:
                        bad = bytearray(raw); struct.pack_into("<I", bad, offset, 2147483647); reject(bad)
                    for offset in fields["positions"]:
                        for value in (2**64-1, 2**40):
                            bad = bytearray(raw); struct.pack_into("<Q", bad, offset, value)
                            reject(bad, stage=value==2**40)
                finally: e.close()

    def test_table_only_state_survives_project_reconstruction(self):
        for family in ("standard", "custom"):
            with tempfile.TemporaryDirectory() as fixtures:
                text = fixture(fixtures, evaporation="TIMESERIES", temperature="series", file_series=True, shared=True)
                with tempfile.TemporaryDirectory() as root:
                    e = self.engine(family, root, text)
                    try:
                        for _ in range(60): e.step()
                        raw = e.dump(only=True)
                    finally: e.close()
                with tempfile.TemporaryDirectory() as root:
                    e = self.engine(family, root, text)
                    try:
                        self.assertNotEqual(e.dump(only=True), raw)
                        e.lib.es_test_tables_drop()
                        self.assertEqual(e.restore(raw, only=True), 0)
                        self.assertEqual(e.dump(only=True), raw)
                    finally: e.close()

    def test_date_continuation_unterminated_eof_and_rain_consumers(self):
        for family in ("standard", "custom"):
            with tempfile.TemporaryDirectory() as root:
                text = fixture(root, evaporation="TIMESERIES", temperature="series", file_series=True, days=12)
                (Path(root)/"Air.txt").write_bytes(b"01/30/2020 0:00 20\r\n1:30 80\r\n20:00 -10\r\n01/31/2020 24:00 50\r\n02/03/2020 0 40\r\n02/09/2020 0 30")
                (Path(root)/"PET.txt").write_bytes(b"01/30/2020 0 .1\n13 .5\n01/31/2020 1 .2\n02/01/2020 12 .8\n02/05/2020 0 .4")
                self.assertEqual(self.run_case(family, text, True), self.run_case(family, text))
                for source in ("inline", "timeseries-file", "rain-file"):
                    text = rainfall(root, source=source, co=True)
                    self.assertEqual(self.run_case(family, text, True), self.run_case(family, text))

    def test_table_state_and_query_history_rebuild_in_fresh_process(self):
        start = (datetime(2020, 1, 30)-datetime(1899, 12, 30)).days
        dates = [start+hour/24 for hour in (300, -24, 1, 60, 150, 0)]
        for family in ("standard", "custom"):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory); source = root/"fixture.inp"; saved = root/"table.bin"
                source.write_text(fixture(root, evaporation="TIMESERIES", temperature="series", file_series=True, shared=True))
                first = root/"first"; second = root/"second"; first.mkdir(); second.mkdir()
                e = self.engine(family, first, source.read_text())
                try:
                    for _ in range(60): e.step()
                    saved.write_bytes(e.dump(only=True))
                    expected = query_history(e, dates)
                finally: e.close()
                code = """import json,sys
from pathlib import Path
from test_native_v2_checkpoint_tables import Engine,query_history
library,source,saved,destination,dates=sys.argv[1:]
e=Engine(library,destination,Path(source).read_text())
try:
 raw=Path(saved).read_bytes()
 assert e.restore(raw,only=True)==0 and e.committed and e.cleanup==0
 assert e.dump(only=True)==raw
 Path(destination,'queries.json').write_text(json.dumps(query_history(e,json.loads(dates))))
finally:e.close()
"""
                env = dict(os.environ, PYTHONPATH=os.pathsep.join((str(Path(__file__).parent), str(Path(__file__).parents[1]/"src"))))
                result = subprocess.run([sys.executable, "-B", "-c", code,
                    os.environ["EASYSEWER_CHECKPOINT_"+family.upper()], str(source), str(saved),
                    str(second), json.dumps(dates)], env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
                self.assertEqual(json.loads((second/"queries.json").read_text()), expected)


def query_history(engine, dates):
    rows = []
    for index in range(engine.root_count):
        values = [engine.lib.es_test_tables_lookup(index, date, 1) for date in dates]
        x, y = c.c_double(), c.c_double()
        found = engine.lib.es_test_tables_next(index, c.byref(x), c.byref(y))
        rows.append([values, found, x.value, y.value])
    return rows
