"""Climate static field facts against both packaged solvers and resumed results."""
from dataclasses import fields
import hashlib
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from easysewer import get_native_capabilities
from easysewer.model import climate as c
from test_climate_fields_v2 import fixture, load, queries, OWNER, UNITS
from test_native_v2_climate import user_weather, ghcnd_weather
import test_native_v2_node_fields as nodes
from test_native_v2_regulator_fields import FAMILIES, library

EVIDENCE = []


@unittest.skipUnless(get_native_capabilities()['swmm_solver'] and get_native_capabilities()['flexible_ponding'], 'Native solvers unavailable')
class NativeClimateFieldTests(unittest.TestCase):
    observe = nodes.NativeNodeFieldTests.observe

    def test_static_defaults_and_adjustments_preserve_complete_results(self):
        cases = [(u, e, t, None) for u in UNITS for e in ('omitted', 'constant', 'monthly', 'series') for t in ('none', 'series')]
        cases += [(u, e, t, fmt) for u in UNITS for e, t in (('file', 'file'), ('file', 'series'), ('derived', 'file')) for fmt in ('user', 'ghcnd')]
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            for family, name, symbol in FAMILIES:
                lib, path = library(name, symbol); rows = []
                for units, evap, temp, fmt in cases:
                    with self.subTest(family=family, units=units, evaporation=evap, temperature=temp, format=fmt):
                        weather = base/'weather.dat' if fmt else None
                        if fmt == 'user': user_weather(weather, units=units)
                        if fmt == 'ghcnd': ghcnd_weather(weather, 'C10')
                        source = fixture(units, evap, temp, weather, 'C10' if fmt == 'ghcnd' else None)
                        model = load(source)
                        values = {f.name: model.inspect_field(OWNER, f.name).semantics.effective for f in fields(c.Climate)}
                        self.assertTrue(all(v.status == 'known' for v in values.values()))
                        changes = {k: v.value for k, v in values.items() if not isinstance(v.value, c.ConstantTemperature)}
                        model.update_climate(**changes)
                        expected = self.observe(lib, base, source, full=True)
                        actual = self.observe(lib, base, model.to_document(normalize=True).text, full=True)
                        self.assertEqual(actual, expected)
                        rows.append(dict(units=units, evaporation=evap, temperature=temp, format=fmt,
                                         steps=len(actual['history']), out_sha256=actual['out_sha256']))
                EVIDENCE.append(dict(kind='climate-field-native-results', family=family, cases=len(rows), rows=rows,
                                     library_sha256=hashlib.sha256(Path(path).read_bytes()).hexdigest()))

    def test_climate_field_history_survives_relocated_checkpoint(self):
        from easysewer.runtime import RunResult
        import test_native_v2_runner_checkpoint as checkpoint
        helper = checkpoint.NativeRunnerCheckpointTests()
        with patch.dict(os.environ, EASYSEWER_CHECKPOINT_PACKAGED='1'):
            for family in ('standard', 'custom'):
                for mode in ('monthly', 'file', 'derived'):
                    with self.subTest(family=family, mode=mode), tempfile.TemporaryDirectory() as directory:
                        root = Path(directory); assets = root/'inputs'; assets.mkdir()
                        weather = assets/'weather.dat' if mode != 'monthly' else None
                        if weather: user_weather(weather)
                        model = load(fixture(evaporation=mode, temperature='file' if weather else 'series', weather=weather))
                        before = queries(model)
                        original, saved = helper.original(root, family, model=model)
                        helper.success(original); self.assertTrue(saved)
                        self.assertEqual(queries(original.snapshot.model()), before)
                        original.save(root/'expected'); expected = RunResult.load(root/'expected')
                        self.assertEqual(queries(expected.snapshot.model()), before)
                        shutil.rmtree(root/'first'); shutil.rmtree(assets)
                        (root/'original.hsf').unlink(); (root/'saved').rename(root/'moved')
                        actual = checkpoint.runner(family).resume(root/'moved'/saved[0].directory.name, checkpoint.resume_config(root/'resumed'))
                        helper.equivalent(expected, actual)
                        self.assertEqual(queries(actual.snapshot.model()), before)
                        EVIDENCE.append(dict(kind='climate-fields-checkpoint', family=family, mode=mode, queries=len(before),
                            out_sha256=actual.output.sha256, original_workspace_removed=True, original_climate_file_removed=bool(weather)))


if __name__ == '__main__': unittest.main()
