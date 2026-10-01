"""Run the bounded checkpoint OUT/writes fault profile with checked test builds.

Requires both independently built families. Skips are qualification failures.
Results cover historical owner probes, not public checkpoint/container release
acceptance. Detailed logs, evidence and exact input hashes remain in a new folder.
"""

import argparse
import importlib
import json
import os
from pathlib import Path
import sys
import unittest

from build_checkpoint_test_library import (
    ROOT, PROFILE, FAMILIES, digest, fresh_destination, recipe_hashes,
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


def qualification_passed(result):
    # unittest's wasSuccessful() permits expected failures. This fixed fault
    # profile must actually pass every assertion, rather than relaxing a gate.
    return (result.wasSuccessful() and result.testsRun == EXPECTED_TESTS and
            not result.skipped and not result.expectedFailures and not result.unexpectedSuccesses)


def verify_build(directory, family):
    directory = Path(directory).resolve()
    record_path = directory / "checkpoint-test-build.json"
    record = json.loads(record_path.read_bytes())
    if (record.get("profile") != PROFILE or record.get("family") != family or
            record.get("test_only") is not True or
            record.get("production_qualified") is not False or
            record.get("integrity_verified") is not True or record.get("returncode") != 0):
        raise ValueError("Requires a successful, integrity-verified test-only " + family + " build")
    output = directory / ("checkpoint-test-" + family + ".so")
    if output.resolve().parent != directory or record.get("output") != str(output):
        raise ValueError("Unexpected test-library output path")
    if digest(output.read_bytes()) != record.get("sha256"):
        raise ValueError("Test library changed: " + family)
    staged = directory / "instrumented-source"
    source_path = staged / "checkpoint-test-source.json"
    if digest(source_path.read_bytes()) != record.get("source_manifest_sha256"):
        raise ValueError("Test source manifest changed: " + family)
    source = json.loads(source_path.read_bytes())
    if (source.get("profile") != PROFILE or source.get("family") != family or
            source.get("test_only") is not True or source.get("production_qualified") is not False):
        raise ValueError("Unexpected test source profile")
    verify_files(staged, source["files"])
    verify_tree_inventory(staged, source["files"], extra=("checkpoint-test-source.json",))
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
    return dict(library=str(output), sha256=record["sha256"],
                build_manifest_sha256=digest(record_path.read_bytes()),
                source_manifest_sha256=record["source_manifest_sha256"])


def python_inventory(root, *, repo=ROOT):
    return {path.relative_to(repo).as_posix(): digest(path.read_bytes())
            for path in root.rglob("*.py")}


def qualify(standard, custom, destination):
    if not __debug__:
        raise RuntimeError("Do not run checkpoint qualification with optimized Python")
    if sys.platform != "linux" or not Path("/proc/self/fd").is_dir():
        raise RuntimeError("Linux descriptor-count qualification requires /proc/self/fd")
    directories = dict(standard=Path(standard).resolve(), custom=Path(custom).resolve())
    libraries = {family: verify_build(directory, family) for family, directory in directories.items()}
    _, destination = fresh_destination(ROOT, destination)
    for directory in directories.values():
        if destination.is_relative_to(directory) or directory.is_relative_to(destination):
            raise ValueError("Qualification destination must be separate from builds")
    destination.mkdir(parents=True, exist_ok=False)
    for family, library in libraries.items():
        os.environ["EASYSEWER_CHECKPOINT_" + family.upper()] = library["library"]
    os.environ["OMP_NUM_THREADS"] = "1"
    os.environ.pop("PYTHONOPTIMIZE", None)
    os.environ["PYTHONPATH"] = os.pathsep.join((str(ROOT / "src"), str(ROOT / "tests")))
    sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]
    # Fixtures import other test modules and the source Python runtime. Record
    # that complete local Python input inventory, not only the two entry files.
    test_inputs = python_inventory(ROOT / "tests")
    runtime_inputs = python_inventory(ROOT / "src/easysewer")
    recipe_hash = digest(Path(__file__).read_bytes())
    suite = unittest.defaultTestLoader.loadTestsFromNames(TESTS)
    if suite.countTestCases() != EXPECTED_TESTS:
        raise RuntimeError("Qualification test inventory changed; review the profile")
    with (destination / "tests.log").open("w", encoding="utf-8") as log:
        result = unittest.TextTestRunner(stream=log, verbosity=2).run(suite)
    record = dict(profile=PROFILE, scope="Historical OUT and output-write owners plus two hook-activation tests",
        production_qualified=False, tests=list(TESTS), libraries=libraries,
        environment={key: os.environ[key] for key in (
            "OMP_NUM_THREADS", "PYTHONPATH", "EASYSEWER_CHECKPOINT_STANDARD", "EASYSEWER_CHECKPOINT_CUSTOM")},
        qualification_recipe_sha256=recipe_hash, test_inputs=test_inputs,
        runtime_inputs=runtime_inputs, python=dict(executable=sys.executable, version=sys.version),
        tests_run=result.testsRun, failures=len(result.failures), errors=len(result.errors),
        expected_failures=len(result.expectedFailures), unexpected_successes=len(result.unexpectedSuccesses),
        skipped=[dict(test=str(test), reason=reason) for test, reason in result.skipped],
        evidence={name: getattr(importlib.import_module(name), "EVIDENCE", [])
                  for name in ("test_native_v2_checkpoint_output", "test_native_v2_checkpoint_writes")})
    try:
        for family, directory in directories.items():
            if verify_build(directory, family) != libraries[family]:
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
    record["passed"] = qualification_passed(result)
    write_json(destination / "qualification.json", record)
    if not record["passed"]:
        raise RuntimeError("Checkpoint qualification failed; inspect tests.log and qualification.json")
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--standard-build", type=Path, required=True)
    parser.add_argument("--custom-build", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    record = qualify(args.standard_build, args.custom_build, args.destination)
    print(json.dumps({key: record[key] for key in ("profile", "passed", "tests_run", "failures", "errors",
                                                  "expected_failures", "unexpected_successes", "skipped")}, indent=2))


if __name__ == "__main__":
    main()
