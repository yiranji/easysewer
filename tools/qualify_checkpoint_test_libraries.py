"""Run the bounded checkpoint OUT/writes fault profile with checked test builds.

Requires both independently built families. Skips are qualification failures.
Results cover historical owner probes, not public checkpoint/container release
acceptance. Detailed logs, evidence and exact input hashes remain in a new folder.
"""

import argparse
import ctypes
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from build_checkpoint_test_library import (
    ROOT, PROFILE, DEFAULT_PROFILE, LID_PROFILE, PROFILES, profile_details, FAMILIES, digest, fresh_destination, recipe_hashes,
    verify_files, verify_tree_inventory, write_json,
)


TESTS = (
    "test_native_v2_checkpoint_output",
    "test_native_v2_checkpoint_writes",
    "test_native_v2_checkpoint_controls.NativeCheckpointControlTests."
    "test_staged_allocation_failures_leave_original_state_and_allow_retry",
    "test_native_v2_checkpoint_tables.NativeCheckpointTableTests."
    "test_every_stage_fault_preserves_session_and_reports_commit_cleanup",
)
EXPECTED_TESTS = 18
LID_TESTS = (
    "test_native_v2_lid_report_io.NativeLidReportIOTests."
    "test_every_reachable_write_flush_close_format_and_allocation_failure",
    "test_native_v2_lid_report_io.NativeLidReportIOTests."
    "test_close_failure_preserves_prior_error_and_releases_all_owners",
    "test_native_v2_lid_report_lifecycle.LidReportLifecycleTests."
    "test_all_model_allocations_and_report_opens_release_partial_projects",
    "test_native_v2_lid_report_lifecycle.LidReportLifecycleTests."
    "test_run_and_reopen_do_not_replace_live_report_owners",
    "test_native_v2_lid_report_runner.LidReportRunnerTests."
    "test_failure_preserves_published_reports_and_recovery_with_both_retention_policies",
)
LID_EXPECTED_TESTS = 6  # Five unchanged fault tests and one clean-control gate.
LID_MODULES = ("test_native_v2_lid_report_io", "test_native_v2_lid_report_lifecycle",
               "test_native_v2_lid_report_runner")



def qualification_passed(result, *, profile=DEFAULT_PROFILE):
    # unittest's wasSuccessful() permits expected failures. This fixed fault
    # profile must actually pass every assertion, rather than relaxing a gate.
    profile_details(profile)
    expected = LID_EXPECTED_TESTS if profile == LID_PROFILE else EXPECTED_TESTS
    return (result.wasSuccessful() and result.testsRun == expected and
            not result.skipped and not result.expectedFailures and not result.unexpectedSuccesses)


def verify_build(directory, family, *, profile=DEFAULT_PROFILE):
    profile_id, prefix = profile_details(profile)
    directory = Path(directory).resolve()
    record_path = directory / (prefix + "-build.json")
    record = json.loads(record_path.read_bytes())
    if (record.get("profile") != profile_id or record.get("family") != family or
            record.get("test_only") is not True or
            record.get("production_qualified") is not False or
            record.get("integrity_verified") is not True or record.get("returncode") != 0):
        raise ValueError("Requires a successful, integrity-verified test-only " + family + " build")
    output = directory / (prefix + "-" + family + ".so")
    if output.resolve().parent != directory or record.get("output") != str(output):
        raise ValueError("Unexpected test-library output path")
    if digest(output.read_bytes()) != record.get("sha256"):
        raise ValueError("Test library changed: " + family)
    staged = directory / "instrumented-source"
    source_path = staged / (prefix + "-source.json")
    if digest(source_path.read_bytes()) != record.get("source_manifest_sha256"):
        raise ValueError("Test source manifest changed: " + family)
    source = json.loads(source_path.read_bytes())
    if (source.get("profile") != profile_id or source.get("family") != family or
            source.get("test_only") is not True or source.get("production_qualified") is not False):
        raise ValueError("Unexpected test source profile")
    verify_files(staged, source["files"])
    verify_tree_inventory(staged, source["files"], extra=(prefix + "-source.json",))
    manifest = ROOT / "native" / FAMILIES[family] / "source.json"
    if (source["upstream"] != json.loads(manifest.read_bytes()) or
            digest(manifest.read_bytes()) != source["upstream_manifest_sha256"]):
        raise ValueError("Upstream profile changed")
    recipe_hashes(source["baseline"], family)
    for name, expected in source["instrumentation_helpers"].items():
        path = ROOT / name
        if not path.resolve().is_relative_to(ROOT):
            raise ValueError("Instrumentation helper escapes repository")
        if digest(path.read_bytes()) != expected:
            raise ValueError("Instrumentation helper changed: " + name)
    if digest((ROOT / "tools/build_checkpoint_test_library.py").read_bytes()) != source["harness_sha256"]:
        raise ValueError("Build harness changed; rebuild test libraries")
    result = dict(library=str(output), sha256=record["sha256"],
                  build_manifest_sha256=digest(record_path.read_bytes()),
                  source_manifest_sha256=record["source_manifest_sha256"])
    if profile == LID_PROFILE:
        expected_patch = ("easysewer:standard:5.2.4:16" if family == "standard" else
                          "easysewer:flexible-ponding:abi:201")
        prepared = source["baseline"]
        if (prepared["base"] != source["upstream"] or prepared["patch"] != expected_patch or
                prepared.get("lid_report_io") != 1 or
                (family == "custom" and prepared.get("native_io_fixes") != 14)):
            raise ValueError("Unexpected current LID source family/profile")
        baseline = directory / "verified-baseline"
        baseline_manifest = baseline / "prepared-source.json"
        if (digest(baseline_manifest.read_bytes()) != source["baseline_manifest_sha256"] or
                json.loads(baseline_manifest.read_bytes()) != source["baseline"]):
            raise ValueError("Clean control source manifest changed")
        verify_files(baseline, source["baseline"]["files"])
        verify_tree_inventory(baseline, source["baseline"]["files"], extra=("prepared-source.json",))
        control = record.get("control", {})
        control_output = directory / (prefix + "-control-" + family + ".so")
        if (control.get("returncode") != 0 or control.get("output") != str(control_output) or
                control_output.resolve().parent != directory):
            raise ValueError("Requires successful clean control build")
        if digest(control_output.read_bytes()) != control.get("sha256"):
            raise ValueError("Clean control library changed")
        result["control"] = dict(library=str(control_output), sha256=control["sha256"])
    return result



def verify_lid_identity(libraries):
    """Check the loaded current family and profile, not only symbol presence."""
    identities = {}
    for family, paths in libraries.items():
        for role, path in (("fault", paths["library"]), ("control", paths["control"]["library"])):
            lib = ctypes.CDLL(path)
            expected = {"swmm_getVersion": 52004, "swmm_getEasySewerLidReportIO": 1,
                ("swmm_getEasySewerStandardFixes" if family == "standard" else
                 "swmm_getEasySewerNativeIOFixes"): 16 if family == "standard" else 14}
            if family == "custom":
                expected["swmm_getFlexiblePondingAbi"] = 201
            if role == "fault":
                expected["es_test_lid_report_profile"] = 1
                for name in ("fault", "fault_fired", "calls", "prior_error", "secondary_error"):
                    getattr(lib, "es_test_lid_" + name)
            elif hasattr(lib, "es_test_lid_report_profile") or hasattr(lib, "es_test_lid_fault"):
                raise ValueError("Clean control contains test instrumentation")
            if hasattr(lib, "es_test_checkpoint_profile"):
                raise ValueError("LID profile contains checkpoint instrumentation")
            if hasattr(lib, "swmm_execRouting") != (family == "custom"):
                raise ValueError("Unexpected native family routing contract")
            actual = {}
            for symbol, value in expected.items():
                function = getattr(lib, symbol)
                function.argtypes = []
                function.restype = ctypes.c_int
                actual[symbol] = function()
                if actual[symbol] != value:
                    raise ValueError("Unexpected native identity: " + symbol)
            identities[family + ":" + role] = actual
    return identities


def lid_no_fault_equivalence(evidence):
    """Compare each fault library to its own unmodified current-source control."""
    io = importlib.import_module("test_native_v2_lid_report_io")
    check = unittest.TestCase()
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        for family in ("standard", "custom"):
            fault, control = io.load(family, faults=True), io.load(family)
            fault.es_test_lid_fault(0, 0)
            for kind in ("BC", "RG", "GR", "IT", "PP", "RB", "RD", "VS"):
                for units in ("CFS", "CMS"):
                    source = io.fixture(kind, units, multiple=True)
                    outputs = []
                    for label, lib in (("control", control), ("fault", fault)):
                        dest = root / family / kind / units / label
                        codes, _ = io.run(lib, dest, source)
                        check.assertFalse(any(codes), (family, kind, units, label, codes))
                        values = [(dest / name).read_bytes() for name in
                            ("model.out", "model.rpt", "lid detail.txt", "lid second.txt")]
                        values[1] = io.normalized_report(values[1])
                        outputs.append(values)
                    check.assertEqual(outputs[0], outputs[1], (family, kind, units))
                    check.assertEqual(fault.es_test_lid_fault_fired(), 0)
                    evidence.append(dict(family=family, lid=kind, units=units,
                        hashes=[digest(value) for value in outputs[1]]))
            fault.es_test_lid_fault(0, 0)


def verify_lid_evidence(evidence, no_fault):
    """Reject missing, duplicated or incomplete subprocess/fault evidence."""
    kinds = ("BC", "RG", "GR", "IT", "PP", "RB", "RD", "VS")
    expected_normal = {(family, kind, units) for family in FAMILIES
                       for kind in kinds for units in ("CFS", "CMS")}
    actual_normal = [(item["family"], item["lid"], item["units"]) for item in no_fault]
    if len(actual_normal) != 32 or set(actual_normal) != expected_normal:
        raise ValueError("Incomplete clean-control equivalence evidence")
    io = evidence[LID_MODULES[0]]
    if len(io) != 2 or {item["family"] for item in io} != set(FAMILIES):
        raise ValueError("Incomplete LID direct fault evidence")
    for item in io:
        calls = item["calls"]
        if (set(calls) != set(range(1, 7)) or any(value <= 0 for value in calls.values()) or
                item["injected"] != sum(calls.values()) or item["recoveries"] != item["injected"]):
            raise ValueError("Incomplete LID reachable-call evidence")
    lifecycle = evidence[LID_MODULES[1]]
    if len(lifecycle) != 4:
        raise ValueError("Incomplete LID lifecycle evidence")
    for family in FAMILIES:
        partial = [item for item in lifecycle if item["family"] == family and
                   item["kind"] == "partial-allocation-open"]
        guards = [item for item in lifecycle if item["family"] == family and
                  item["kind"] == "owner-guards-and-run"]
        if len(partial) != 1 or len(guards) != 1 or guards[0]["cases"] != 3:
            raise ValueError("Incomplete LID owner-guard evidence")
        expected = {(kind, hit, 101 if kind == 7 and hit <= 3 else 200)
                    for kind, count in ((7, 8), (8, 2)) for hit in range(1, count + 1)}
        outcomes = partial[0]["outcomes"]
        actual = {(item["kind"], item["hit"], item["codes"][0]) for item in outcomes}
        if len(outcomes) != 10 or actual != expected or any(item["codes"][1:] != [0] for item in outcomes):
            raise ValueError("Incomplete LID partial-initialization evidence")
    cases = ((1, 1, 306, "start"), (1, 60, 306, "start"), (1, 119, 306, "step"),
             (2, 1, 306, "start"), (2, 3, 306, "step"), (3, 1, 306, "close"),
             (3, 2, 306, "close"), (4, 1, 306, "step"), (5, 1, 306, "step"),
             (6, 1, 200, "open"), (6, 4, 200, "open"), (7, 1, 101, "open"),
             (7, 4, 200, "open"), (8, 2, 200, "open"), (1, 1, 101, "start"))
    expected = {(family, keep, kind, hit, code, stage, code == 101 and kind == 1)
                for family in FAMILIES for keep in (False, True) for kind, hit, code, stage in cases}
    runner = evidence[LID_MODULES[2]]
    actual = {(item["family"], item["keep"], item["kind"], item["hit"], item["code"],
               item["stage"], item["primary"]) for item in runner}
    if (len(runner) != 60 or actual != expected or
            any(item["old_artifacts_preserved"] is not True or item["recovered"] is not True for item in runner)):
        raise ValueError("Incomplete LID Runner fault/recovery matrix")


def python_inventory(root, *, repo=ROOT):
    return {path.relative_to(repo).as_posix(): digest(path.read_bytes())
            for path in root.rglob("*.py")}


def qualify(standard, custom, destination, *, profile=DEFAULT_PROFILE):
    profile_id, _ = profile_details(profile)
    if not __debug__:
        raise RuntimeError("Do not run checkpoint qualification with optimized Python")
    if sys.platform != "linux" or not Path("/proc/self/fd").is_dir():
        raise RuntimeError("Linux descriptor-count qualification requires /proc/self/fd")
    directories = dict(standard=Path(standard).resolve(), custom=Path(custom).resolve())
    libraries = {family: verify_build(directory, family, profile=profile) for family, directory in directories.items()}
    _, destination = fresh_destination(ROOT, destination)
    for directory in directories.values():
        if destination.is_relative_to(directory) or directory.is_relative_to(destination):
            raise ValueError("Qualification destination must be separate from builds")
    destination.mkdir(parents=True, exist_ok=False)
    environment_keys = ["OMP_NUM_THREADS", "PYTHONPATH"]
    for family, library in libraries.items():
        key = ("EASYSEWER_LID_REPORT_FAULT_" if profile == LID_PROFILE else
               "EASYSEWER_CHECKPOINT_") + family.upper()
        os.environ[key] = library["library"]
        environment_keys.append(key)
        if profile == LID_PROFILE:
            key = "EASYSEWER_LID_REPORT_" + family.upper()
            os.environ[key] = library["control"]["library"]
            environment_keys.append(key)
    if profile == LID_PROFILE:
        for key in ("ES_LID_IO_FAULT", "ES_LID_IO_HIT", "ES_LID_IO_PRIMARY"):
            os.environ.pop(key, None)
    os.environ["OMP_NUM_THREADS"] = "1"
    os.environ.pop("PYTHONOPTIMIZE", None)
    os.environ["PYTHONPATH"] = os.pathsep.join((str(ROOT / "src"), str(ROOT / "tests")))
    sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]
    # Fixtures import other test modules and the source Python runtime. Record
    # that complete local Python input inventory, not only the two entry files.
    test_inputs = python_inventory(ROOT / "tests")
    runtime_inputs = python_inventory(ROOT / "src/easysewer")
    recipe_hash = digest(Path(__file__).read_bytes())
    tests = LID_TESTS if profile == LID_PROFILE else TESTS
    modules = LID_MODULES if profile == LID_PROFILE else (
        "test_native_v2_checkpoint_output", "test_native_v2_checkpoint_writes")
    for name in modules:
        importlib.import_module(name).EVIDENCE.clear()
    no_fault = []
    identities = verify_lid_identity(libraries) if profile == LID_PROFILE else {}
    suite = unittest.TestSuite()
    if profile == LID_PROFILE:
        suite.addTest(unittest.FunctionTestCase(lambda: lid_no_fault_equivalence(no_fault),
            description="32 no-fault comparisons against clean current prepared controls"))
    suite.addTests(unittest.defaultTestLoader.loadTestsFromNames(tests))
    expected = LID_EXPECTED_TESTS if profile == LID_PROFILE else EXPECTED_TESTS
    if suite.countTestCases() != expected:
        raise RuntimeError("Qualification test inventory changed; review the profile")
    with (destination / "tests.log").open("w", encoding="utf-8") as log:
        result = unittest.TextTestRunner(stream=log, verbosity=2).run(suite)
    record = dict(profile=profile_id, scope=("Current-source LID report faults and clean-control equivalence"
        if profile == LID_PROFILE else "Historical OUT and output-write owners plus two hook-activation tests"),
        production_qualified=False, tests=list(tests), libraries=libraries,
        environment={key: os.environ[key] for key in environment_keys},
        qualification_recipe_sha256=recipe_hash, test_inputs=test_inputs,
        runtime_inputs=runtime_inputs, python=dict(executable=sys.executable, version=sys.version),
        tests_run=result.testsRun, failures=len(result.failures), errors=len(result.errors),
        expected_failures=len(result.expectedFailures), unexpected_successes=len(result.unexpectedSuccesses),
        skipped=[dict(test=str(test), reason=reason) for test, reason in result.skipped],
        evidence={name: getattr(importlib.import_module(name), "EVIDENCE", [])
                  for name in modules})
    if profile == LID_PROFILE:
        record.update(no_fault_equivalence=no_fault, identities=identities,
                      cleared_environment=["ES_LID_IO_FAULT", "ES_LID_IO_HIT", "ES_LID_IO_PRIMARY"])
    try:
        for family, directory in directories.items():
            if verify_build(directory, family, profile=profile) != libraries[family]:
                raise ValueError("Test build changed during qualification")
        if (python_inventory(ROOT / "tests") != test_inputs or
                python_inventory(ROOT / "src/easysewer") != runtime_inputs):
            raise ValueError("Python input inventory changed during qualification")
        if digest(Path(__file__).read_bytes()) != recipe_hash:
            raise ValueError("Qualification recipe changed during tests")
    except (OSError, ValueError) as error:
        record.update(passed=False, integrity_error=str(error))
        write_json(destination / "qualification.json", record)
        raise
    record["passed"] = qualification_passed(result, profile=profile)
    if profile == LID_PROFILE:
        try:
            verify_lid_evidence(record["evidence"], no_fault)
            record["evidence_verified"] = True
        except (IndexError, KeyError, TypeError, ValueError) as error:
            record.update(passed=False, evidence_verified=False, evidence_error=str(error))
    write_json(destination / "qualification.json", record)
    if not record["passed"]:
        raise RuntimeError("Test-only qualification failed; inspect tests.log and qualification.json")
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--standard-build", type=Path, required=True)
    parser.add_argument("--custom-build", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--profile", choices=tuple(PROFILES), default=DEFAULT_PROFILE)
    args = parser.parse_args()
    record = qualify(args.standard_build, args.custom_build, args.destination, profile=args.profile)
    print(json.dumps({key: record[key] for key in ("profile", "passed", "tests_run", "failures", "errors",
                                                  "expected_failures", "unexpected_successes", "skipped")}, indent=2))


if __name__ == "__main__":
    main()
