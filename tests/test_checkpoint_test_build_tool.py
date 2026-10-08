"""Fast, compiler-independent regression checks for test-only native preparation."""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "checkpoint_test_build", ROOT / "tools/build_checkpoint_test_library.py")
build = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build)
sys.path.insert(0, str(ROOT / "tools"))
try:
    import qualify_checkpoint_test_libraries as qualify
finally:
    sys.path.pop(0)


def synthetic_baseline(family="standard"):
    """Exact owner blocks, with a minimal untouched sentinel for other modules."""
    contents = {"src/solver/" + name: b"/* untouched numerical sentinel */\n"
                for name in (*build.OWNERS.values(), "swmm5.c", "es_checkpoint_writes.c")}
    for filename, fragment in (("controls.c", "controls.inc"), ("table.c", "tables.inc")):
        contents["src/solver/" + filename] += (ROOT / "native/checkpoint" / fragment).read_bytes()
    contents["src/solver/odesolve.c"] += b"    if (nmax < n) return 1;"
    contents["src/solver/es_checkpoint_writes.c"] += (
        b"static int file_identity(FILE *file, EsCkFileIdentity *id)\n{\n")
    if family == "custom":
        contents["src/solver/easysewer_ponding.c"] = b"/* custom numerical sentinel */\n"
    return contents


class CheckpointTestBuildTests(unittest.TestCase):
    def symlink(self, path, target):
        try:
            path.symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError) as error:
            self.skipTest("Host cannot create test symlinks: " + str(error))

    def test_instrumentation_preserves_input_and_orders_bundle_dependencies(self):
        original = synthetic_baseline()
        saved = dict(original)
        result, helpers = build.instrument(original, "standard")
        self.assertEqual(original, saved)
        self.assertEqual(set(result), set(original))
        engine = result["src/solver/swmm5.c"]
        positions = [engine.index((ROOT / "tests" / ("native_checkpoint_" + name + ".inc")).read_bytes())
                     for name in build.BUNDLES]
        self.assertEqual(positions, sorted(positions))
        self.assertIn(b"es_test_checkpoint_profile(void)", engine)
        self.assertNotIn("src/solver/easysewer_ponding.c", result)
        for name, expected in helpers.items():
            self.assertEqual(build.digest((ROOT / name).read_bytes()), expected)

    def test_custom_profile_has_ponding_hook(self):
        result, helpers = build.instrument(synthetic_baseline("custom"), "custom")
        self.assertIn(b"es_test_ponding_change", result["src/solver/easysewer_ponding.c"])
        self.assertIn("tests/native_checkpoint_ponding.inc", helpers)

    def test_fault_hooks_do_not_intercept_their_own_wrappers(self):
        result, _ = build.instrument(synthetic_baseline(), "standard")
        controls = result["src/solver/controls.c"]
        self.assertIn(b"item = es_test_stage_calloc(1, sizeof(*item));", controls)
        self.assertIn(b"return calloc(n, size);", controls)
        table = result["src/solver/table.c"]
        self.assertIn(b"return es_test_table_fault(1) ? NULL : calloc(n, size);", table)
        self.assertIn(b"retired && es_test_table_close(retired)", table)
        writes = result["src/solver/es_checkpoint_writes.c"]
        self.assertIn(b"if (es_test_write_fault()) return ES_CK_IO;", writes)
        self.assertLess(writes.index(b"#define fflush"), writes.index(b"static int file_identity"))

    def test_repeated_or_stale_instrumentation_fails_closed(self):
        result, _ = build.instrument(synthetic_baseline(), "standard")
        with self.assertRaisesRegex(ValueError, "already contain"):
            build.instrument(result, "standard")
        for name in ("controls.c", "table.c", "odesolve.c", "es_checkpoint_writes.c"):
            with self.subTest(name=name):
                contents = synthetic_baseline()
                contents["src/solver/" + name] = b"stale content"
                with self.assertRaisesRegex(ValueError, "anchor"):
                    build.instrument(contents, "standard")

    def test_unknown_family_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unknown family"):
            build.instrument(synthetic_baseline(), "typo")

    def test_file_digests_and_crlf_normalization(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "x.c").write_bytes(b"line\r\n")
            expected = {"x.c": build.digest(b"line\n")}
            self.assertEqual(build.verify_files(root, expected, normalize=True), {"x.c": b"line\n"})
            with self.assertRaisesRegex(ValueError, "digest differs"):
                build.verify_files(root, expected)
            with self.assertRaisesRegex(ValueError, "Empty"):
                build.verify_files(root, {})

    def test_manifest_paths_cannot_escape_or_use_host_syntax(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ("../escape", "/absolute", "a/../b", "a//b", "./a", "C:/a", "a\\b", ""):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    build.source_path(root, name)

    def test_manifest_symlink_cannot_escape_tree(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.symlink(root / "escape", root.parent)
            with self.assertRaisesRegex(ValueError, "escapes"):
                build.source_path(root, "escape/file")

    def test_destination_is_fresh_and_disjoint(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, repo = root / "upstream", root / "repository"
            source.mkdir(); repo.mkdir()
            for destination in (source, source / "child", repo, repo / "src/easysewer/libs/new",
                                root):
                with self.subTest(destination=destination), self.assertRaises(ValueError):
                    build.fresh_destination(source, destination, repo=repo)
            existing = root / "existing"; existing.mkdir()
            with self.assertRaisesRegex(ValueError, "already exist"):
                build.fresh_destination(source, existing, repo=repo)
            self.assertEqual(build.fresh_destination(source, root / "new", repo=repo), (source, root / "new"))

    def test_destination_rejects_live_and_dangling_symlinks(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, repo = root / "upstream", root / "repository"
            source.mkdir(); repo.mkdir()
            alias = root / "alias"; self.symlink(alias, repo)
            with self.assertRaises(ValueError):
                build.fresh_destination(source, alias / "child", repo=repo)
            dangling = root / "dangling"; self.symlink(dangling, root / "missing")
            with self.assertRaisesRegex(ValueError, "existing symlink"):
                build.fresh_destination(source, dangling, repo=repo)
            self.assertFalse((root / "missing").exists())

    def test_recipe_changes_and_escaping_recipe_paths_are_rejected(self):
        checkpoint = ROOT / "native/checkpoint/patch.py"
        path_io = ROOT / "native/path_io/patch.py"
        record = dict(recipe={"prepare.py": build.digest((ROOT / "native/standard/prepare.py").read_bytes())},
            checkpoint={"recipes": {"patch.py": build.digest(checkpoint.read_bytes())}},
            path_io={"recipes": {"patch.py": build.digest(path_io.read_bytes())}},
            horton_capacity={"recipe_sha256": build.digest((ROOT / "native/standard/horton.py").read_bytes())},
            horton_outfall={"recipe_sha256": build.digest((ROOT / "native/standard/horton_outfall.py").read_bytes())})
        self.assertEqual(len(build.recipe_hashes(record, "standard")), 5)
        record["checkpoint"]["recipes"]["patch.py"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "recipe differs"):
            build.recipe_hashes(record, "standard")
        record["recipe"] = {"../../README.md": "0" * 64}
        with self.assertRaisesRegex(ValueError, "escapes"):
            build.recipe_hashes(record, "standard")

    def test_optimized_python_is_rejected_before_source_or_compiler_access(self):
        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder) / "new"
            process = subprocess.run([sys.executable, "-O", "-B", str(ROOT / "tools/build_checkpoint_test_library.py"),
                "--source", "missing", "--compiler", "missing", "--family", "standard",
                "--destination", str(destination)], capture_output=True, text=True)
            self.assertNotEqual(process.returncode, 0)
            self.assertIn("optimized Python", process.stderr)
            self.assertFalse(destination.exists())

    def test_post_compiler_input_mutation_invalidates_build_evidence(self):
        # Simulated preparation/compiler avoid downloading or compiling during
        # public CI, but execute the real build integrity and evidence logic.
        for mutation in ("upstream", "baseline", "staged", "compiler", "helper", "manifest",
                         "recipe", "harness", "baseline-manifest", "upstream-manifest", "extra-header"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as folder:
                base = Path(folder)
                repo, upstream = base / "repo", base / "upstream"
                native = repo / "native/standard"; native.mkdir(parents=True)
                source_name = "src/solver/unit.c"
                path = upstream / source_name; path.parent.mkdir(parents=True)
                raw = b"int unchanged(void) { return 1; }\n"; path.write_bytes(raw)
                original = dict(files={source_name: build.digest(raw)})
                (native / "source.json").write_text(json.dumps(original))
                for name in ("horton.py", "horton_outfall.py"):
                    (native / name).write_bytes(b"recipe\n")
                helper = repo / "tests/helper.inc"; helper.parent.mkdir()
                helper.write_bytes(b"helper\n")
                compiler = base / "gcc"; compiler.write_bytes(b"fake compiler")
                harness = base / "harness.py"; harness.write_bytes(b"test harness")
                destination = base / "result"
                record = dict(base=original, patch="easysewer:standard:5.2.4:16", recipe={},
                    checkpoint={"abi": 2, "recipes": {}}, path_io={"recipes": {}},
                    horton_capacity={"recipe_sha256": build.digest(b"recipe\n")},
                    horton_outfall={"recipe_sha256": build.digest(b"recipe\n")},
                    files=original["files"])

                def subprocess_run(command, **kwargs):
                    if "--destination" in command:
                        baseline = destination / "verified-baseline"
                        target = baseline / source_name; target.parent.mkdir(parents=True)
                        target.write_bytes(raw)
                        (baseline / "prepared-source.json").write_text(json.dumps(record))
                    else:
                        output = Path(command[command.index("-o") + 1]); output.write_bytes(b"library")
                        paths = dict(upstream=path, baseline=destination / "verified-baseline" / source_name,
                            staged=destination / "instrumented-source" / source_name, compiler=compiler,
                            helper=helper, manifest=destination / "instrumented-source/checkpoint-test-source.json",
                            recipe=native / "horton.py", harness=harness,
                            **{"baseline-manifest": destination / "verified-baseline/prepared-source.json",
                               "upstream-manifest": native / "source.json",
                               "extra-header": destination / "instrumented-source/src/solver/stdio.h"})
                        changed = paths[mutation]
                        changed.write_bytes((changed.read_bytes() if changed.exists() else b"") + b"changed")
                    return subprocess.CompletedProcess(command, 0, "", "")

                original_recipe_hashes = build.recipe_hashes
                with mock.patch.object(build, "ROOT", repo), \
                        mock.patch.object(build, "__file__", str(harness)), \
                        mock.patch.object(build.sys, "platform", "linux"), \
                        mock.patch.object(build, "recipe_hashes", side_effect=lambda r, f:
                            original_recipe_hashes(r, f, repo=repo)), \
                        mock.patch.object(build, "instrument", return_value=(
                            {source_name: raw}, {"tests/helper.inc": build.digest(b"helper\n")})), \
                        mock.patch.object(build.subprocess, "check_output", return_value="fake GCC\n"), \
                        mock.patch.object(build.subprocess, "run", side_effect=subprocess_run):
                    with self.assertRaisesRegex(ValueError, "differs|changed"):
                        build.build(upstream, destination, compiler, "standard")
                evidence = json.loads((destination / "checkpoint-test-build.json").read_bytes())
                self.assertFalse(evidence["integrity_verified"])
                self.assertIn("integrity_error", evidence)
                self.assertNotIn("sha256", evidence)

    def test_tree_inventory_rejects_additional_headers(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "unit.c").write_bytes(b"source")
            (root / "record.json").write_bytes(b"{}")
            build.verify_tree_inventory(root, {"unit.c": "unused"}, extra=("record.json",))
            (root / "stdio.h").write_bytes(b"unexpected header")
            with self.assertRaisesRegex(ValueError, "inventory differs"):
                build.verify_tree_inventory(root, {"unit.c": "unused"}, extra=("record.json",))

    def test_python_inventory_detects_added_removed_and_changed_inputs(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "one.py").write_bytes(b"original")
            original = qualify.python_inventory(root, repo=root)
            (root / "two.py").write_bytes(b"added")
            self.assertNotEqual(qualify.python_inventory(root, repo=root), original)
            (root / "two.py").unlink(); (root / "one.py").write_bytes(b"changed")
            self.assertNotEqual(qualify.python_inventory(root, repo=root), original)
            (root / "one.py").unlink()
            self.assertNotEqual(qualify.python_inventory(root, repo=root), original)

    def test_unsupported_build_platform_fails_before_input_access(self):
        with mock.patch.object(build.sys, "platform", "win32"):
            with self.assertRaisesRegex(ValueError, "Linux only"):
                build.build("missing", "missing", "missing", "standard")

    def test_qualification_rejects_missing_failed_or_wrong_family_build(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            record = dict(profile=build.PROFILE, family="standard", test_only=True,
                          production_qualified=False, integrity_verified=True, returncode=0)
            for field, value in (("family", "custom"), ("profile", "production"),
                                 ("test_only", False), ("production_qualified", True),
                                 ("integrity_verified", False), ("returncode", 1)):
                with self.subTest(field=field):
                    changed = dict(record, **{field: value})
                    (directory / "checkpoint-test-build.json").write_text(json.dumps(changed))
                    with self.assertRaisesRegex(ValueError, "successful, integrity-verified"):
                        qualify.verify_build(directory, "standard")

    def test_qualification_rejects_library_or_manifest_tampering(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            output = directory / "checkpoint-test-standard.so"; output.write_bytes(b"library")
            record = dict(profile=build.PROFILE, family="standard", test_only=True,
                production_qualified=False, integrity_verified=True, returncode=0,
                output=str(output), sha256=build.digest(b"original"))
            path = directory / "checkpoint-test-build.json"
            path.write_text(json.dumps(record))
            with self.assertRaisesRegex(ValueError, "library changed"):
                qualify.verify_build(directory, "standard")
            record["sha256"] = build.digest(output.read_bytes())
            record["source_manifest_sha256"] = "0" * 64
            path.write_text(json.dumps(record))
            staged = directory / "instrumented-source"; staged.mkdir()
            (staged / "checkpoint-test-source.json").write_bytes(b"{}")
            with self.assertRaisesRegex(ValueError, "source manifest changed"):
                qualify.verify_build(directory, "standard")

    def test_qualification_optimized_python_is_rejected_before_build_access(self):
        process = subprocess.run([sys.executable, "-O", "-B",
            str(ROOT / "tools/qualify_checkpoint_test_libraries.py"),
            "--standard-build", "missing", "--custom-build", "missing",
            "--destination", "missing"], capture_output=True, text=True)
        self.assertNotEqual(process.returncode, 0)
        self.assertIn("optimized Python", process.stderr)

    def test_qualification_requires_every_expected_assertion_to_pass(self):
        result = unittest.TestResult()
        result.testsRun = qualify.EXPECTED_TESTS
        self.assertTrue(qualify.qualification_passed(result))
        for attribute in ("failures", "errors", "skipped", "expectedFailures", "unexpectedSuccesses"):
            with self.subTest(attribute=attribute):
                result = unittest.TestResult(); result.testsRun = qualify.EXPECTED_TESTS
                getattr(result, attribute).append(("test", "reason"))
                self.assertFalse(qualify.qualification_passed(result))
        for count in (0, qualify.EXPECTED_TESTS - 1, qualify.EXPECTED_TESTS + 1):
            result = unittest.TestResult(); result.testsRun = count
            self.assertFalse(qualify.qualification_passed(result))


if __name__ == "__main__":
    unittest.main()
