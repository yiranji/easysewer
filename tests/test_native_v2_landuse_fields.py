"""Every buildup/washoff formula and contextual unit against both solvers."""
from dataclasses import fields, is_dataclass, replace
import hashlib
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from easysewer import get_native_capabilities
from easysewer.model import Ref
from test_landuse_fields_v2 import fixture, load, queries, UNITS, POLLUTANT_UNITS, BUILDS, WASHES, NAMES
import test_native_v2_node_fields as nodes
from test_native_v2_regulator_fields import FAMILIES, library

EVIDENCE = []


@unittest.skipUnless(get_native_capabilities()['swmm_solver'] and get_native_capabilities()['flexible_ponding'], 'Native solvers unavailable')
class NativeLandUseFieldTests(unittest.TestCase):
    observe = nodes.NativeNodeFieldTests.observe

    def test_every_formula_and_unit_preserves_complete_native_results(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            for family, name, symbol in FAMILIES:
                lib, path = library(name, symbol); rows = []
                for units in UNITS:
                    for pu in POLLUTANT_UNITS:
                        for normalizer in ('AREA', 'CURBLENGTH'):
                            for build in BUILDS:
                                for wash in WASHES:
                                    with self.subTest(family=family, units=units, pu=pu, normalizer=normalizer, build=build, wash=wash):
                                        source = fixture(units, pu, build, wash, normalizer, 'defaults'); m = load(source)
                                        def materialize(owner, row, prefix=()):
                                            changes = {}
                                            for f in fields(row):
                                                if f.name in ('id', 'landuse', 'subcatchment', 'pollutant', 'series'):
                                                    continue
                                                p = prefix + (f.name,); fact = m.inspect_field(owner, p).semantics.effective
                                                self.assertIn(fact.status, ('known', 'not_applicable'), p)
                                                if fact.status == 'known':
                                                    value = getattr(row, f.name)
                                                    changes[f.name] = materialize(owner, value, p) if is_dataclass(value) else fact.value
                                            return replace(row, **changes)
                                        for namespace in NAMES:
                                            collection = m.collection('swmm:' + namespace)
                                            for key, row in tuple(collection.items()):
                                                collection.replace(key, materialize(Ref(collection='swmm:' + namespace, key=key), row))
                                        expected = self.observe(lib, base, source, full=True)
                                        actual = self.observe(lib, base, m.to_document(normalize=True).text, full=True)
                                        self.assertEqual(actual, expected)
                                        rows.append(dict(units=units, pollutant_units=pu, normalizer=normalizer, buildup=build,
                                            washoff=wash, steps=len(actual['history']), out_sha256=actual['out_sha256']))
                EVIDENCE.append(dict(kind='landuse-field-native-results', family=family, cases=len(rows), rows=rows,
                    library_sha256=hashlib.sha256(Path(path).read_bytes()).hexdigest()))

    def test_formulas_and_sources_survive_relocated_checkpoint(self):
        from easysewer.runtime import RunResult
        import test_native_v2_runner_checkpoint as checkpoint
        helper = checkpoint.NativeRunnerCheckpointTests()
        with patch.dict(os.environ, EASYSEWER_CHECKPOINT_PACKAGED='1'):
            for family in ('standard', 'custom'):
                for mode in ('defaults', 'explicit', 'curb'):
                    with self.subTest(family=family, mode=mode), tempfile.TemporaryDirectory() as directory:
                        root = Path(directory)
                        m = load(fixture(build='EXT' if mode == 'curb' else 'SAT', wash='RC' if mode == 'curb' else 'EXP',
                            normalizer='CURBLENGTH' if mode == 'curb' else 'AREA', mode=mode))
                        before = queries(m)
                        original, saved = helper.original(root, family, model=m); helper.success(original); self.assertTrue(saved)
                        self.assertEqual(queries(original.snapshot.model()), before)
                        original.save(root / 'expected'); expected = RunResult.load(root / 'expected')
                        self.assertEqual(queries(expected.snapshot.model()), before)
                        shutil.rmtree(root / 'first'); (root / 'original.hsf').unlink(); (root / 'saved').rename(root / 'moved')
                        actual = checkpoint.runner(family).resume(root / 'moved' / saved[0].directory.name, checkpoint.resume_config(root / 'resumed'))
                        helper.equivalent(expected, actual); self.assertEqual(queries(actual.snapshot.model()), before)
                        EVIDENCE.append(dict(kind='landuse-fields-checkpoint', family=family, mode=mode, queries=len(before),
                            out_sha256=actual.output.sha256, original_workspace_removed=True))


if __name__ == '__main__': unittest.main()
