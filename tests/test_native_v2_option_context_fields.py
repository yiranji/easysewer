"""Independent native sweeping calendar and temporary-directory observations."""
from dataclasses import replace
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from easysewer import get_native_capabilities
from easysewer.io.hotstart import HotstartData, HotstartLayout
from easysewer.model import Model
from easysewer.model.options import MonthDay
from easysewer.model.values import FileReference
from test_hydrology_fields_v2 import UNITS
from test_landuse_fields_v2 import fixture as landuse_fixture
from test_option_context_fields_v2 import load, queries
from test_native_v2_regulator_fields import FAMILIES, library
from test_native_v2_control_fields import observe

EVIDENCE = []

SCRATCH_CHILD = r'''
import ctypes, json, os, sys
from pathlib import Path
package_root, library_path = sys.argv[1:]
sys.path.insert(0, package_root)
import easysewer
assert Path(easysewer.__file__).resolve().parent.parent == Path(package_root).resolve()
lib = ctypes.CDLL(library_path)
lib.swmm_open.argtypes = [ctypes.c_char_p] * 3
lib.swmm_start.argtypes = [ctypes.c_int]
lib.swmm_step.argtypes = [ctypes.POINTER(ctypes.c_double)]
root = Path.cwd()
files = lambda: sorted(p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file())
codes = []; before = files(); during = []
try:
    codes.append(lib.swmm_open(b'model.inp', b'model.rpt', b''))
    if codes[-1] == 0:
        codes.append(lib.swmm_start(1)); during = files()
        if codes[-1] == 0:
            for _ in range(20000):
                elapsed = ctypes.c_double(); code = lib.swmm_step(ctypes.byref(elapsed))
                if code or not elapsed.value:
                    codes.append(code); break
            else: raise AssertionError('Simulation exceeded bounded steps')
        codes.append(lib.swmm_end())
finally:
    codes.append(lib.swmm_close())
print(json.dumps(dict(codes=codes, before=before, during=during, after=files(),
    package_file=str(Path(easysewer.__file__).resolve()))))
'''

# Explicit expected activity, independent of the model's effective-field rules.
# The native season uses non-leap ordinals but execution uses actual year days.
SEASONS = (
    ((3, 1), (3, 3), '2019-02-28', False),
    ((3, 1), (3, 3), '2019-03-01', True),
    ((3, 1), (3, 3), '2019-03-03', True),
    ((3, 1), (3, 3), '2019-03-04', False),
    ((3, 1), (3, 3), '2020-02-28', False),
    ((3, 1), (3, 3), '2020-02-29', True),
    ((3, 1), (3, 3), '2020-03-02', True),
    ((3, 1), (3, 3), '2020-03-03', False),
    ((3, 1), (3, 1), '2020-02-29', True),
    ((3, 1), (3, 1), '2020-03-01', False),
    ((12, 30), (1, 2), '2019-12-29', False),
    ((12, 30), (1, 2), '2019-12-30', True),
    ((12, 30), (1, 2), '2020-01-02', True),
    ((12, 30), (1, 2), '2020-01-03', False),
    ((12, 30), (1, 2), '2020-12-29', True),
    ((12, 30), (1, 2), '2020-12-31', True),
    ((1, 1), (12, 31), '2019-12-31', True),
    ((1, 1), (12, 31), '2020-12-31', False),
)


def sweeping_model(units, start, end, day):
    model = load(landuse_fixture(units=units, build='NONE'))
    model.update_options(start_date=date.fromisoformat(day), end_date=date.fromisoformat(day),
        sweep_start=MonthDay(month=start[0], day=start[1]), sweep_end=MonthDay(month=end[0], day=end[1]))
    model.timeseries.update('Rain', points=tuple(replace(p, value=0) for p in model.timeseries['Rain'].points))
    model.landuses.update('Land', days_since_sweeping=2)
    model.pollutants.update('Q0', rainfall_concentration=0, groundwater_concentration=0,
        rdii_concentration=0, decay_rate=0)
    return model


@unittest.skipUnless(get_native_capabilities()['swmm_solver'] and get_native_capabilities()['flexible_ponding'],
                     'Both native families unavailable')
class NativeOptionContextFieldTests(unittest.TestCase):
    def test_native_scratch_location_and_cleanup_in_isolated_process(self):
        import easysewer
        from test_options_v2 import network
        package_root = str(Path(easysewer.__file__).resolve().parent.parent)
        environment = dict(os.environ)
        # MSVCRT's TMP override is separate from the explicit TEMPDIR argument.
        environment.pop('TMP', None)
        for family, name, symbol in FAMILIES:
            _, path = library(name, symbol)
            for existing in (False, True):
                with self.subTest(family=family, existing=existing), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory); chosen = root/'chosen scratch'
                    if existing: chosen.mkdir()
                    marker = root/'unrelated.txt'; marker.write_bytes(b'keep')
                    model = network()
                    model.update_options(temp_directory=FileReference(path='chosen scratch',
                        base_directory=str(root), direction='output'))
                    (root/'model.inp').write_text(model.to_document().text, encoding='utf-8')
                    result = subprocess.run([sys.executable, '-B', '-c', SCRATCH_CHILD, package_root, str(path)],
                        cwd=root, env=environment, capture_output=True, text=True, timeout=60)
                    self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
                    row = json.loads(result.stdout)
                    self.assertFalse(any(row['codes']), row)
                    scratch = set(row['during']) - set(row['before']) - {'model.rpt'}
                    self.assertTrue(scratch, row)
                    for item in scratch:
                        self.assertEqual(Path(item).parent, Path('chosen scratch'))
                    self.assertEqual(set(row['after']), set(row['before']) | {'model.rpt'})
                    self.assertEqual(marker.read_bytes(), b'keep')
                    EVIDENCE.append(dict(kind='native-temp-directory', family=family, existing=existing, **row))

    def test_runner_unicode_temp_directory_rejects_file_and_preserves_ownership(self):
        from easysewer.runtime import Runner, FlexiblePondingBackend
        from test_runner_v2 import config
        for family in ('standard', 'custom'):
            with self.subTest(family=family), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); chosen = root/'scratch 中文 😀'/'nested'
                model = sweeping_model('CFS', (3, 1), (3, 1), '2020-02-29')
                model.update_options(allow_ponding=True, temp_directory=FileReference(path=str(chosen), direction='output'))
                key = 'swmm:standard' if family == 'standard' else FlexiblePondingBackend().key
                runner = Runner() if family == 'standard' else Runner(backends={key: FlexiblePondingBackend()})
                before = queries(model); rows = []
                for attempt in range(2):
                    result = runner.run(model, config(root/str(attempt), backend=key))
                    self.assertTrue(result.succeeded, (result.failure, result.diagnostics))
                    self.assertEqual(Path(result.snapshot.execution_directory).parent, chosen)
                    self.assertFalse(Path(result.snapshot.execution_directory).exists())
                    self.assertEqual(queries(result.snapshot.model()), before)
                    rows.append(result.output.sha256)
                    if attempt == 0:
                        self.assertFalse(chosen.exists())
                        chosen.mkdir(parents=True); (chosen/'external.txt').write_bytes(b'keep')
                self.assertEqual(rows[0], rows[1])
                self.assertEqual(sorted(p.name for p in chosen.iterdir()), ['external.txt'])
                blocked = root/'not-a-directory'; blocked.write_bytes(b'keep')
                model.update_options(temp_directory=FileReference(path=str(blocked), direction='output'))
                result = runner.run(model, config(root/'blocked', backend=key, keep_failed_artifacts=False))
                self.assertFalse(result.succeeded)
                self.assertFalse(result.native_completed)
                self.assertEqual(blocked.read_bytes(), b'keep')
                EVIDENCE.append(dict(kind='runner-temp-directory', family=family, out_sha256=rows[0],
                    invalid_status=result.status, invalid_codes=[d.code for d in result.diagnostics.errors]))

    def test_sweeping_mass_at_inclusive_wrapping_and_leap_boundaries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); state = root/'state.hsf'
            for family, name, symbol in FAMILIES:
                lib, path = library(name, symbol); rows = []
                for units in UNITS:
                    for start, end, day, active in SEASONS:
                        with self.subTest(family=family, units=units, start=start, end=end, day=day):
                            model = sweeping_model(units, start, end, day)
                            text = model.to_document().text + f'[FILES]\nSAVE HOTSTART "{state}"\n'
                            model = load(text, source=str(root/'model.inp')); before = queries(model)
                            expected = observe(self, lib, root, text); raw = state.read_bytes()
                            result = HotstartData.from_bytes(raw, layout=HotstartLayout.from_model(model))
                            land = result.subcatchments[0].landuses[0]
                            # Full land coverage, initial 4 mass/area, availability
                            # .5 and removal 40%: exactly 20% removed once if active.
                            initial = 4 * model.subcatchments['S'].area
                            self.assertAlmostEqual(land.buildup[0], initial * (.8 if active else 1), delta=1e-10)
                            epoch = date(1899, 12, 30)
                            expected_swept = (date.fromisoformat(day) - epoch).days
                            if active:
                                self.assertGreaterEqual(land.last_swept, expected_swept)
                                self.assertLess(land.last_swept, expected_swept + 1)
                            else:
                                self.assertEqual(land.last_swept, expected_swept - 2)
                            restored = Model.from_json_document(model.to_json_document(), strict=True)
                            self.assertEqual(queries(restored), before)
                            self.assertEqual(observe(self, lib, root, restored.to_document(normalize=True).text), expected)
                            self.assertEqual(state.read_bytes(), raw)
                            rows.append(dict(units=units, start=start, end=end, date=day, active=active,
                                final_mass=land.buildup[0], last_swept=land.last_swept,
                                out_sha256=expected['out_sha256'], report_sha256=expected['report_sha256']))
                EVIDENCE.append(dict(kind='sweeping-calendar-mass-oracle', family=family, rows=rows,
                    library_sha256=hashlib.sha256(Path(path).read_bytes()).hexdigest()))

    def test_option_sources_survive_moved_checkpoint_and_result_archive(self):
        from easysewer.runtime import RunResult
        import test_native_v2_runner_checkpoint as checkpoint
        helper = checkpoint.NativeRunnerCheckpointTests()
        with patch.dict(os.environ, EASYSEWER_CHECKPOINT_PACKAGED='1'):
            for family in ('standard', 'custom'):
                with self.subTest(family=family), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory); chosen = root/'scratch original'
                    model = sweeping_model('CMS', (3, 1), (3, 1), '2020-02-29')
                    model.update_options(temp_directory=FileReference(path=str(chosen), direction='output'))
                    model = load(model.to_document().text, source=str(root/'model.inp'))
                    original, saved = helper.original(root, family, model=model); helper.success(original)
                    self.assertTrue(saved); before = queries(original.snapshot.model())
                    original.save(root/'expected'); expected = RunResult.load(root/'expected')
                    self.assertEqual(queries(expected.snapshot.model()), before)
                    shutil.rmtree(root/'first'); self.assertFalse(chosen.exists())
                    (root/'original.hsf').unlink(); (root/'saved').rename(root/'moved')
                    actual = checkpoint.runner(family).resume(root/'moved'/saved[0].directory.name,
                        checkpoint.resume_config(root/'resumed'))
                    helper.equivalent(expected, actual)
                    self.assertEqual(queries(actual.snapshot.model()), before)
                    EVIDENCE.append(dict(kind='option-context-moved-checkpoint', family=family,
                        queries=len(before), out_sha256=actual.output.sha256, original_workspace_removed=True))


if __name__ == '__main__': unittest.main()
