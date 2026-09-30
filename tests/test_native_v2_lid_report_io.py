"""Candidate-only detailed LID report boundaries and actual stream failures."""
import ctypes as C
import hashlib
from dataclasses import replace
from datetime import date, time, timedelta
import os
from pathlib import Path
import re
import tempfile
import unittest

from easysewer.model.values import FileReference
from easysewer.model.resources import SeriesPoint
from test_lid_v2 import lid_model, usage
from test_native_v2_standard_io import direct_library, handles

EVIDENCE = []

def fixture(kind='BC', units='CFS', *, detail='lid detail.txt', multiple=False):
    model = lid_model(kind)
    model.reinterpret_units(units)
    model.update_options(end_date=date(2020, 1, 30), end_time=time(0, 6),
                         wet_step=timedelta(seconds=60),
                         dry_step=timedelta(seconds=60),
                         routing_step=timedelta(seconds=5),
                         report_step=timedelta(seconds=60))
    model.raingages.update('R', interval=timedelta(seconds=60))
    model.timeseries.update('Rain', points=tuple(SeriesPoint(time=timedelta(minutes=n),
        value=.6 if n in (2, 4) else 0) for n in range(7)))
    model.lid_usage.update('lid-usage-1', report_file=FileReference(path=detail, direction='output'))
    if multiple:
        model.lid_usage.add(usage('second', area=50, initial_saturation=0,
            report_file=FileReference(path='lid second.txt', direction='output')))
    return model.to_document().text


def cycle_fixture():
    """Empty barrels with no drain: direct rain controls wet/dry transitions."""
    from easysewer.model import Model
    from easysewer.io.inp import InpDocument
    model = Model.from_document(InpDocument.from_text(fixture('RB', multiple=True)), strict=True)
    model.update_options(end_time=time(0, 12))
    model.timeseries.update('Rain', points=tuple(SeriesPoint(time=timedelta(minutes=n),
        value=.6 if n in (2, 8) else 0) for n in range(13)))
    model.lid_controls.update('L', drain=replace(model.lid_controls['L'].drain, coefficient=0),
                             storage=replace(model.lid_controls['L'].storage, covered=False))
    for key in model.lid_usage:
        model.lid_usage.update(key, initial_saturation=0, from_impervious=0, from_pervious=0)
    return model.to_document().text


def message(lib):
    msg = C.create_string_buffer(1024)
    code = lib.swmm_getError(msg, len(msg))
    return code, msg.value


def run(lib, root, source):
    root.mkdir(parents=True, exist_ok=True)
    inp, rpt, out = [root / ('model' + ext) for ext in ('.inp', '.rpt', '.out')]
    inp.write_text(source, encoding='utf-8')
    old = Path.cwd()
    codes = []
    try:
        os.chdir(root)
        codes.append(lib.swmm_open(os.fsencode(inp), os.fsencode(rpt), os.fsencode(out)))
        if not codes[-1]:
            codes.append(lib.swmm_start(1))
            if not codes[-1]:
                elapsed = C.c_double()
                for _ in range(20000):
                    code = lib.swmm_step(C.byref(elapsed))
                    if code or not elapsed.value:
                        codes.append(code)
                        break
                else:
                    raise AssertionError('Step limit reached')
            codes.append(lib.swmm_end())
    finally:
        codes.append(lib.swmm_close())
        os.chdir(old)
    return codes, message(lib)


def load(family, *, faults=False, installed=False):
    if installed:
        from easysewer.utils import probe_library_path
        path = probe_library_path('swmm5' if family == 'standard' else 'flexible_ponding')
    else:
        key = 'EASYSEWER_LID_REPORT_' + ('FAULT_' if faults else '') + family.upper()
        path = os.environ.get(key)
        if not path:
            raise unittest.SkipTest(key + ' candidate not selected')
    lib, _ = direct_library(path, revision_symbol='swmm_getEasySewerStandardFixes'
                            if family == 'standard' else 'swmm_getEasySewerNativeIOFixes')
    lib.swmm_getError.argtypes = [C.c_char_p, C.c_int]
    if faults:
        lib.es_test_lid_fault.argtypes = [C.c_int, C.c_int]
        lib.es_test_lid_calls.argtypes = [C.c_int]
        lib.es_test_lid_prior_error.argtypes = [C.c_int]
        lib.es_test_lid_secondary_error.argtypes = [C.c_int]
    return lib


def normalized_report(raw):
    return re.sub(rb'(?m)^[ \t]*(?:Analysis (?:begun|ended)|Total elapsed time)[^\r\n]*', b'', raw)


class NativeLidReportIOTests(unittest.TestCase):
    def test_dry_wet_transitions_keep_both_edges_and_previous_row(self):
        with tempfile.TemporaryDirectory() as directory:
            for family in ('standard', 'custom'):
                root = Path(directory) / family
                source = cycle_fixture()
                for label, lib in (('installed', load(family, installed=True)), ('candidate', load(family))):
                    self.assertFalse(any(run(lib, root / label, source)[0]))
                for name in ('lid detail.txt', 'lid second.txt'):
                    raw = (root / 'candidate' / name).read_bytes()
                    self.assertEqual(raw, (root / 'installed' / name).read_bytes())
                    rows = [line.split() for line in raw.decode().splitlines() if re.match(r'\s*01/30/2020', line)]
                    wet = [i for i, row in enumerate(rows) if float(row[3]) > 0]
                    self.assertEqual(len(wet), 2, rows)
                    for index in wet:
                        self.assertGreater(index, 0)
                        self.assertLess(index, len(rows) - 1)
                        self.assertEqual(float(rows[index-1][3]), 0)
                        self.assertEqual(float(rows[index+1][3]), 0)
                    self.assertLess(len(rows), 12, 'Intermediate dry rows were not suppressed')
                EVIDENCE.append(dict(kind='dry-wet-edges', family=family, reports=2))

    def test_normal_full_output_main_and_detail_reports_match_installed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for family in ('standard', 'custom'):
                candidate, installed = load(family), load(family, installed=True)
                for kind in ('BC', 'RG', 'GR', 'IT', 'PP', 'RB', 'RD', 'VS'):
                    for units in ('CFS', 'CMS'):
                        with self.subTest(family=family, kind=kind, units=units):
                            source = fixture(kind, units, multiple=True)
                            results = []
                            for label, lib in (('installed', installed), ('candidate', candidate)):
                                dest = root / family / kind / units / label
                                codes, _ = run(lib, dest, source)
                                self.assertFalse(any(codes), codes)
                                results.append(tuple((dest / name).read_bytes() for name in
                                    ('model.out', 'model.rpt', 'lid detail.txt', 'lid second.txt')))
                            self.assertEqual(results[0][0], results[1][0])
                            self.assertEqual(normalized_report(results[0][1]), normalized_report(results[1][1]))
                            self.assertEqual(results[0][2:], results[1][2:])
                            EVIDENCE.append(dict(kind='normal', family=family, lid=kind, units=units,
                                out_sha256=hashlib.sha256(results[1][0]).hexdigest(),
                                report_sha256=hashlib.sha256(normalized_report(results[1][1])).hexdigest(),
                                detail_sha256=[hashlib.sha256(b).hexdigest() for b in results[1][2:]]))

    @unittest.skipUnless(os.name != 'nt' and Path('/dev/full').exists(), 'POSIX full device')
    def test_real_full_device_fails_at_start_and_next_project_recovers(self):
        with tempfile.TemporaryDirectory() as directory:
            for family in ('standard', 'custom'):
                lib = load(family)
                root = Path(directory) / family
                before = handles()
                for n in range(5):
                    codes, (error, msg) = run(lib, root / str(n), fixture(detail='/dev/full'))
                    self.assertEqual(codes, [0, 306, 306, 306])
                    self.assertEqual(error, 306)
                    self.assertIn(b'LID detailed report: /dev/full', msg)
                    self.assertEqual(lib.swmm_close(), 0)
                    self.assertFalse(any(run(lib, root / ('ok' + str(n)), fixture())[0]))
                self.assertEqual(handles(), before)
                EVIDENCE.append(dict(kind='full-device', family=family, failures=5, recoveries=5))

    def test_headers_and_new_records_are_visible_at_public_boundaries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for family in ('standard', 'custom'):
                lib = load(family)
                detail = root / ('detail-' + family + '.txt')
                inp = root / 'model.inp'
                inp.write_text(fixture(detail=str(detail)), encoding='utf-8')
                try:
                    self.assertEqual(lib.swmm_open(os.fsencode(inp), os.fsencode(root/'model.rpt'),
                                                   os.fsencode(root/'model.out')), 0)
                    self.assertEqual(lib.swmm_start(1), 0)
                    header = detail.read_bytes()
                    self.assertIn(b'SWMM5 LID Report File', header)
                    self.assertIn(b'Storage', header)
                    elapsed = C.c_double()
                    changed = False
                    for _ in range(72):
                        self.assertEqual(lib.swmm_step(C.byref(elapsed)), 0)
                        if len(detail.read_bytes()) > len(header):
                            changed = True
                            break
                    self.assertTrue(changed, 'No visible detail row after stepping')
                finally:
                    self.assertEqual(lib.swmm_close(), 0)

    def test_every_reachable_write_flush_close_format_and_allocation_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            for family in ('standard', 'custom'):
                lib = load(family, faults=True)
                root = Path(directory) / family
                source = cycle_fixture()
                lib.es_test_lid_fault(0, 0)
                self.assertFalse(any(run(lib, root / 'baseline', source)[0]))
                totals = {kind: lib.es_test_lid_calls(kind) for kind in range(1, 7)}
                self.assertTrue(all(totals.values()), totals)
                before = handles()
                for kind, count in totals.items():
                    for hit in range(1, count + 1):
                        with self.subTest(family=family, kind=kind, hit=hit):
                            lib.es_test_lid_fault(kind, hit)
                            codes, (error, msg) = run(lib, root / f'{kind}-{hit}', source)
                            self.assertEqual(lib.es_test_lid_fault_fired(), 1)
                            if kind == 6:
                                self.assertEqual(codes[0], 200)
                                self.assertEqual(error, 200)
                            else:
                                self.assertIn(306, codes)
                                self.assertEqual(error, 306)
                                self.assertIn(b'LID detailed report:', msg)
                                lib.es_test_lid_secondary_error(101)
                                self.assertEqual(message(lib), (error, msg))
                            self.assertEqual(lib.swmm_close(), 0)
                            lib.es_test_lid_fault(0, 0)
                            self.assertFalse(any(run(lib, root / 'recovery', source)[0]))
                self.assertEqual(handles(), before)
                EVIDENCE.append(dict(kind='faults', family=family, calls=totals,
                                     injected=sum(totals.values()), recoveries=sum(totals.values())))

    def test_close_failure_preserves_prior_error_and_releases_all_owners(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for family in ('standard', 'custom'):
                lib = load(family, faults=True)
                detail = root / 'detail.txt'
                inp = root / 'model.inp'
                inp.write_text(fixture(detail=str(detail)), encoding='utf-8')
                lib.es_test_lid_fault(0, 0)
                self.assertEqual(lib.swmm_open(os.fsencode(inp), os.fsencode(root/'model.rpt'),
                                               os.fsencode(root/'model.out')), 0)
                self.assertEqual(lib.swmm_start(1), 0)
                lib.es_test_lid_prior_error(101)
                lib.es_test_lid_fault(3, 1)
                self.assertEqual(lib.swmm_close(), 306)
                self.assertEqual(lib.es_test_lid_fault_fired(), 1)
                self.assertEqual(message(lib), (101, b'prior error 101'))
                self.assertEqual(lib.swmm_close(), 0)
                lib.es_test_lid_fault(0, 0)
                self.assertFalse(any(run(lib, root / ('recovery-' + family), fixture())[0]))


if __name__ == '__main__':
    unittest.main()
