"""Compiler-independent guardrails for the distinct current-source LID profile."""

import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
try:
    import build_checkpoint_test_library as build
    import qualify_checkpoint_test_libraries as qualify
finally:
    sys.path.pop(0)


def synthetic_lid():
    return {"src/solver/lid.c": b'#include "lid.h"\n' + b"\n".join(
                before + b";" for before, _ in build.LID_REPLACEMENTS) + b"\n",
            "src/solver/lidproc.c": b"/* unchanged numerical expressions */\n",
            "src/solver/es_checkpoint_lid.c": b"calloc(n, size); malloc(n);\n"}


def valid_evidence():
    normal = [dict(family=f, lid=k, units=u) for f in build.FAMILIES
              for k in ("BC", "RG", "GR", "IT", "PP", "RB", "RD", "VS") for u in ("CFS", "CMS")]
    io = [dict(family=f, kind="faults", calls={k: 2 for k in range(1, 7)}, injected=12, recoveries=12)
          for f in build.FAMILIES]
    lifecycle = []
    for family in build.FAMILIES:
        lifecycle.extend((dict(family=family, kind="partial-allocation-open", outcomes=[
            dict(kind=k, hit=h, codes=[101 if k == 7 and h <= 3 else 200, 0])
            for k, count in ((7, 8), (8, 2)) for h in range(1, count + 1)]),
            dict(family=family, kind="owner-guards-and-run", cases=3)))
    cases = ((1, 1, 306, "start"), (1, 60, 306, "start"), (1, 119, 306, "step"),
             (2, 1, 306, "start"), (2, 3, 306, "step"), (3, 1, 306, "close"),
             (3, 2, 306, "close"), (4, 1, 306, "step"), (5, 1, 306, "step"),
             (6, 1, 200, "open"), (6, 4, 200, "open"), (7, 1, 101, "open"),
             (7, 4, 200, "open"), (8, 2, 200, "open"), (1, 1, 101, "start"))
    runner = [dict(family=f, keep=keep, kind=k, hit=h, code=c, stage=s, primary=c == 101 and k == 1,
                   old_artifacts_preserved=True, recovered=True)
              for f in build.FAMILIES for keep in (False, True) for k, h, c, s in cases]
    return dict(zip(qualify.LID_MODULES, (io, lifecycle, runner))), normal


class LidReportTestBuildTests(unittest.TestCase):
    def test_profile_is_distinct_and_checkpoint_default_is_unchanged(self):
        self.assertEqual(build.profile_details(build.DEFAULT_PROFILE),
                         (build.PROFILE, "checkpoint-test"))
        self.assertEqual(build.profile_details(build.LID_PROFILE),
                         ("easysewer:test-only:lid-report-faults:1", "lid-report-test"))
        for operation in (lambda: build.build("missing", "missing", "missing", "standard", profile="wrong"),
                          lambda: qualify.verify_build("missing", "standard", profile="wrong"),
                          lambda: qualify.qualify("missing", "missing", "missing", profile="wrong")):
            with self.assertRaisesRegex(ValueError, "Unknown test-only profile"):
                operation()

    def test_only_exact_lid_calls_change_and_input_remains_untouched(self):
        for family in build.FAMILIES:
            with self.subTest(family=family):
                original = synthetic_lid()
                before = dict(original)
                result, helpers = build.instrument_lid(original, family)
                self.assertEqual(original, before)
                self.assertEqual(set(result), set(before))
                for name in before:
                    if name != "src/solver/lid.c":
                        self.assertEqual(result[name], before[name])
                instrumented = result["src/solver/lid.c"]
                restored = instrumented.split(b"/* Test-only failure injection;")[0]
                restored = restored.replace(build.LID_PROTOTYPES, b"").replace(
                    b'#include "lid.h"\n\n', b'#include "lid.h"\n', 1)
                for old, new in build.LID_REPLACEMENTS:
                    self.assertIn(new, instrumented)
                    restored = restored.replace(new, old)
                self.assertEqual(restored, before["src/solver/lid.c"] + b"\n")
                self.assertIn(b"es_test_lid_report_profile(void)", instrumented)
                self.assertNotIn(b"es_test_checkpoint_profile", instrumented)
                self.assertNotIn(b"#define", instrumented)
                helper = (ROOT / "tests/native_lid_report_faults.inc").read_bytes()
                self.assertIn(helper, instrumented)
                self.assertIn(b"int result = fclose(f); return es_lid_fail(3) ? EOF : result;", helper)
                self.assertEqual(helpers, {"tests/native_lid_report_faults.inc": build.digest(helper)})

    def test_stale_duplicate_repeated_and_wrong_family_instrumentation_fail_closed(self):
        for anchor, _ in build.LID_REPLACEMENTS:
            for replacement in (b"stale", anchor + b"; " + anchor):
                with self.subTest(anchor=anchor, replacement=replacement):
                    contents = synthetic_lid()
                    contents["src/solver/lid.c"] = contents["src/solver/lid.c"].replace(anchor, replacement)
                    with self.assertRaisesRegex(ValueError, "anchor"):
                        build.instrument_lid(contents, "standard")
        result, _ = build.instrument_lid(synthetic_lid(), "standard")
        with self.assertRaisesRegex(ValueError, "already contain"):
            build.instrument_lid(result, "standard")
        with self.assertRaisesRegex(ValueError, "Unknown family"):
            build.instrument_lid(synthetic_lid(), "wrong")

    def test_lid_gate_requires_six_real_passes_without_relaxed_outcomes(self):
        result = unittest.TestResult(); result.testsRun = 6
        self.assertTrue(qualify.qualification_passed(result, profile=build.LID_PROFILE))
        self.assertFalse(qualify.qualification_passed(result))
        for attribute in ("failures", "errors", "skipped", "expectedFailures", "unexpectedSuccesses"):
            with self.subTest(attribute=attribute):
                changed = unittest.TestResult(); changed.testsRun = 6
                getattr(changed, attribute).append(("test", "reason"))
                self.assertFalse(qualify.qualification_passed(changed, profile=build.LID_PROFILE))
        for count in (0, 5, 7, 18):
            result.testsRun = count
            self.assertFalse(qualify.qualification_passed(result, profile=build.LID_PROFILE))

    def test_evidence_accepts_only_complete_unique_verified_matrix(self):
        evidence, normal = valid_evidence()
        qualify.verify_lid_evidence(evidence, normal)
        for index in range(3):
            for action in ("remove", "duplicate"):
                with self.subTest(module=index, action=action):
                    changed = copy.deepcopy(evidence)
                    records = changed[qualify.LID_MODULES[index]]
                    if action == "remove":
                        records.pop()
                    else:
                        records[-1] = copy.deepcopy(records[0])
                    with self.assertRaises(ValueError):
                        qualify.verify_lid_evidence(changed, normal)
        for changed in (normal[:-1], normal + normal[:1], normal[:-1] + normal[:1]):
            with self.assertRaisesRegex(ValueError, "equivalence"):
                qualify.verify_lid_evidence(evidence, changed)
        for field, value in (("recovered", False), ("old_artifacts_preserved", False),
                             ("primary", True), ("code", 999), ("stage", "wrong")):
            changed = copy.deepcopy(evidence)
            changed[qualify.LID_MODULES[2]][0][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "Runner"):
                qualify.verify_lid_evidence(changed, normal)
        for field in ("injected", "recoveries"):
            changed = copy.deepcopy(evidence)
            changed[qualify.LID_MODULES[0]][0][field] = 0
            with self.assertRaisesRegex(ValueError, "reachable-call"):
                qualify.verify_lid_evidence(changed, normal)

    def test_lid_qualification_rejects_checkpoint_profile_before_library_access(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "lid-report-test-build.json"
            path.write_text(json.dumps(dict(profile=build.PROFILE, family="standard",
                test_only=True, production_qualified=False, integrity_verified=True, returncode=0)))
            with self.assertRaisesRegex(ValueError, "successful, integrity-verified"):
                qualify.verify_build(folder, "standard", profile=build.LID_PROFILE)


    def test_clean_control_failure_or_compiler_tampering_cannot_qualify(self):
        # Exercise the real shared build/evidence flow with a fake compiler;
        # no private source or installed native toolchain is needed by CI.
        import subprocess
        for case in ("control-mutated", "control-deleted", "control-failed", "valid"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                repo, upstream, destination = root / "repo", root / "upstream", root / "result"
                native = repo / "native/standard"; native.mkdir(parents=True)
                source_name = "src/solver/unit.c"
                source = upstream / source_name; source.parent.mkdir(parents=True)
                raw = b"int unchanged(void) { return 1; }\n"; source.write_bytes(raw)
                original = dict(files={source_name: build.digest(raw)})
                (native / "source.json").write_text(json.dumps(original))
                for name in ("horton.py", "horton_outfall.py"):
                    (native / name).write_bytes(b"recipe\n")
                helper = repo / "tests/helper.inc"; helper.parent.mkdir()
                helper.write_bytes(b"helper\n")
                harness = repo / "tools/build_checkpoint_test_library.py"; harness.parent.mkdir()
                harness.write_bytes(b"test harness")
                compiler = root / "gcc"; compiler.write_bytes(b"fake compiler")
                prepared = dict(base=original, patch="easysewer:standard:5.2.4:16", recipe={},
                    checkpoint={"abi": 2, "recipes": {}}, path_io={"recipes": {}}, lid_report_io=1,
                    horton_capacity={"recipe_sha256": build.digest(b"recipe\n")},
                    horton_outfall={"recipe_sha256": build.digest(b"recipe\n")}, files=original["files"])

                def run(command, **kwargs):
                    code = 0
                    if "--destination" in command:
                        baseline = destination / "verified-baseline"
                        target = baseline / source_name; target.parent.mkdir(parents=True)
                        target.write_bytes(raw)
                        (baseline / "prepared-source.json").write_text(json.dumps(prepared))
                    else:
                        output = Path(command[command.index("-o") + 1])
                        output.write_bytes(b"library")
                        if "-control-" in output.name:
                            code = 9 if case == "control-failed" else 0
                        else:
                            control = destination / "lid-report-test-control-standard.so"
                            if case == "control-mutated":
                                control.write_bytes(b"tampered")
                            elif case == "control-deleted":
                                control.unlink()
                    return subprocess.CompletedProcess(command, code, "", "")

                original_recipe_hashes = build.recipe_hashes
                recipes = lambda r, f: original_recipe_hashes(r, f, repo=repo)
                with mock.patch.object(build, "ROOT", repo), \
                        mock.patch.object(build, "__file__", str(harness)), \
                        mock.patch.object(build.sys, "platform", "linux"), \
                        mock.patch.object(build, "recipe_hashes", side_effect=recipes), \
                        mock.patch.object(build, "instrument_lid", return_value=(
                            {source_name: raw}, {"tests/helper.inc": build.digest(b"helper\n")})), \
                        mock.patch.object(build.subprocess, "check_output", return_value="fake GCC\n"), \
                        mock.patch.object(build.subprocess, "run", side_effect=run):
                    if case == "valid":
                        build.build(upstream, destination, compiler, "standard", profile=build.LID_PROFILE)
                    else:
                        with self.assertRaises((OSError, ValueError, subprocess.CalledProcessError)):
                            build.build(upstream, destination, compiler, "standard", profile=build.LID_PROFILE)
                record = json.loads((destination / "lid-report-test-build.json").read_bytes())
                if case in ("control-mutated", "control-deleted"):
                    self.assertFalse(record["integrity_verified"])
                    self.assertNotIn("sha256", record)
                elif case == "control-failed":
                    self.assertEqual(record["control"]["returncode"], 9)
                with mock.patch.object(qualify, "ROOT", repo), \
                        mock.patch.object(qualify, "recipe_hashes", side_effect=recipes):
                    if case != "valid":
                        with self.assertRaises(ValueError):
                            qualify.verify_build(destination, "standard", profile=build.LID_PROFILE)
                    else:
                        verified = qualify.verify_build(destination, "standard", profile=build.LID_PROFILE)
                        self.assertEqual(verified["control"]["sha256"], build.digest(b"library"))
                        baseline = destination / "verified-baseline"
                        for target in (baseline / source_name, baseline / "prepared-source.json",
                                       baseline / "extra.h",
                                       destination / "lid-report-test-control-standard.so"):
                            existed = target.exists()
                            saved = target.read_bytes() if existed else b""
                            target.write_bytes(saved + b"changed")
                            with self.subTest(target=target.name), self.assertRaises(ValueError):
                                qualify.verify_build(destination, "standard", profile=build.LID_PROFILE)
                            if existed:
                                target.write_bytes(saved)
                            else:
                                target.unlink()


if __name__ == "__main__":
    unittest.main()
