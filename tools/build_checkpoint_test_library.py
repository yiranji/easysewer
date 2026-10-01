"""Build isolated, destructive checkpoint test hooks from pinned upstream bytes.

This tool never produces a production prepared-source.json for instrumented
sources, qualifies a release binary, downloads sources, or installs anything.
The output-writes-v1 profile supports the two historical OUT/writes test suites;
their inherited Engine APIs require the preceding owner test fragments too.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
PROFILE = "easysewer:test-only:checkpoint-output-writes:1"
FAMILIES = {"standard": "standard", "custom": "flexible_ponding"}
BUNDLES = (
    "clocks", "hydraulics", "network_bundle", "inlet_bundle",
    "catchment_bundle", "gage_bundle", "runoff_bundle", "climate_bundle",
    "tables_bundle", "streams_bundle", "frames_bundle", "groundwater_bundle",
    "snow_bundle", "lid_bundle", "balance_bundle", "output_bundle", "writes_bundle",
)
OWNERS = {
    "controls": "controls.c", "routing": "routing.c", "dynwave": "dynwave.c",
    "network": "flowrout.c", "inlet": "inlet.c", "infiltration": "infil.c",
    "subcatchment": "subcatch.c", "gage": "gage.c", "runoff": "runoff.c",
    "climate": "climate.c", "tables": "table.c", "rdii": "rdii.c",
    "iface": "iface.c", "groundwater": "gwater.c", "snow": "snow.c",
    "lid": "lid.c", "lidproc": "lidproc.c", "massbal": "massbal.c",
    "stats": "stats.c", "output": "output.c", "ode": "odesolve.c",
    "treatment": "treatmnt.c",
}
TABLE_PROTOTYPES = b"""
#include <sys/types.h>
void *es_test_table_calloc(size_t, size_t);
int es_test_table_close(FILE *);
#ifdef _WIN32
int es_test_table_seek(FILE *, __int64, int);
__int64 es_test_table_tell(FILE *);
#else
int es_test_table_seek(FILE *, off_t, int);
off_t es_test_table_tell(FILE *);
#endif
"""


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def source_path(root, name):
    """Manifest names are portable relative paths, never host/parent paths."""
    relative = PurePosixPath(name)
    if (not name or "\\" in name or relative.is_absolute() or
            any(part in ("", ".", "..") for part in name.split("/")) or
            ":" in name):
        raise ValueError("Invalid source manifest path: " + name)
    path = root / name
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Source manifest path escapes tree: " + name)
    return path


def verify_files(root, files, *, normalize=False):
    contents = {}
    if not files:
        raise ValueError("Empty source manifest")
    for name, expected in files.items():
        raw = source_path(root, name).read_bytes()
        if normalize:
            raw = raw.replace(b"\r\n", b"\n")
        if digest(raw) != expected:
            raise ValueError("Source digest differs: " + name)
        contents[name] = raw
    return contents


def verify_tree_inventory(root, files, *, extra=()):
    """Additional headers can influence GCC even if no recorded file changed."""
    expected = set(files) | set(extra)
    actual = {path.relative_to(root).as_posix() for path in root.rglob("*")
              if path.is_file() or path.is_symlink()}
    if actual != expected:
        raise ValueError("Source inventory differs: missing=" + repr(sorted(expected - actual)) +
                         "; extra=" + repr(sorted(actual - expected)))


def fresh_destination(source, destination, *, repo=ROOT):
    # exists() is false for dangling symlinks, and resolve() erases that fact.
    if Path(destination).is_symlink():
        raise ValueError("Destination must not be an existing symlink")
    source, destination, repo = (Path(p).resolve() for p in (source, destination, repo))
    # Keeping all generated artifacts outside the repository also protects the
    # installed libraries when callers spell a destination through a symlink.
    for protected in (source, repo):
        if destination == protected or destination.is_relative_to(protected):
            raise ValueError("Destination must be outside source and repository")
        if protected.is_relative_to(destination):
            raise ValueError("Destination cannot contain source or repository")
    if destination.exists():
        raise ValueError("Destination must not already exist")
    return source, destination


def replace_once(raw, before, after):
    if raw.count(before) != 1:
        raise ValueError("Unexpected instrumentation anchor: " + repr(before[:100]))
    return raw.replace(before, after)


def instrument(contents, family, *, repo=ROOT):
    """Only test hooks/counters change; never reapply production checkpoint code."""
    if family not in FAMILIES:
        raise ValueError("Unknown family")
    contents = dict(contents)
    if any(b"es_test_" in raw for raw in contents.values()):
        raise ValueError("Sources already contain test instrumentation")
    helpers = {}

    def read(relative):
        raw = (repo / relative).read_bytes()
        helpers[relative] = digest(raw)
        return raw

    def fragment(name):
        return read("tests/native_checkpoint_" + name + ".inc")

    name = "src/solver/controls.c"
    original = read("native/checkpoint/controls.inc")
    modified = replace_once(original, b"item = calloc(1, sizeof(*item));",
                            b"item = es_test_stage_calloc(1, sizeof(*item));")
    contents[name] = replace_once(contents[name], original,
        b"void *es_test_stage_calloc(size_t, size_t);\n" + modified)

    name = "src/solver/table.c"
    original = read("native/checkpoint/tables.inc")
    modified = original
    for before, after, count in (
        (b"calloc(", b"es_test_table_calloc(", 2),
        (b"fclose(", b"es_test_table_close(", 1),
        (b"fseeko(", b"es_test_table_seek(", 2),
        (b"ftello(", b"es_test_table_tell(", 1),
        (b"_fseeki64(", b"es_test_table_seek(", 2),
        (b"_ftelli64(", b"es_test_table_tell(", 1),
    ):
        if modified.count(before) != count:
            raise ValueError("Unexpected table fault-hook count: " + repr(before))
        modified = modified.replace(before, after)
    contents[name] = replace_once(contents[name], original, TABLE_PROTOTYPES + modified)

    name = "src/solver/odesolve.c"
    contents[name] = b"static int es_test_ode_counts[3];\n" + replace_once(
        contents[name], b"    if (nmax < n) return 1;",
        b"    if (n >= 0 && n < 3) es_test_ode_counts[n]++;\n"
        b"    if (nmax < n) return 1;")

    name = "src/solver/es_checkpoint_writes.c"
    anchor = b"static int file_identity(FILE *file, EsCkFileIdentity *id)\n{\n"
    contents[name] = fragment("writes_faults") + b"\n" + replace_once(
        contents[name], anchor, anchor + b"    if (es_test_write_fault()) return ES_CK_IO;\n")

    owners = dict(OWNERS)
    if family == "custom":
        owners["ponding"] = "easysewer_ponding.c"
    for owner, target in owners.items():
        contents["src/solver/" + target] += b"\n" + fragment(owner)
    for bundle in BUNDLES:
        contents["src/solver/swmm5.c"] += b"\n" + fragment(bundle)
    contents["src/solver/swmm5.c"] += (
        b"\n/* Explicit test-only identity; never register this as a backend. */\n"
        b"int DLLEXPORT es_test_checkpoint_profile(void) { return 1; }\n")
    return contents, helpers


def recipe_hashes(record, family, *, repo=ROOT):
    """Check every recorded preparation input, including shared nested recipes."""
    recipes = {}

    def verify(base, name, expected):
        path = (base / name).resolve()
        if not path.is_relative_to((repo / "native").resolve()):
            raise ValueError("Recipe path escapes native tree")
        actual = digest(path.read_bytes())
        if actual != expected:
            raise ValueError("Preparation recipe differs: " + str(path))
        recipes[path.relative_to(repo).as_posix()] = actual

    base = repo / "native" / FAMILIES[family]
    for name, expected in record["recipe"].items():
        verify(base, name, expected)
    for key, directory in (("checkpoint", "checkpoint"), ("path_io", "path_io")):
        for name, expected in record[key]["recipes"].items():
            verify(repo / "native" / directory, name, expected)
    for key, filename in (("horton_capacity", "horton.py"),
                          ("horton_outfall", "horton_outfall.py")):
        verify(repo / "native/standard", filename, record[key]["recipe_sha256"])
    return recipes


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def build(source, destination, compiler, family):
    if not __debug__:
        raise RuntimeError("Do not run checkpoint qualification with optimized Python")
    if family not in FAMILIES:
        raise ValueError("Unknown family")
    if sys.platform != "linux":
        raise ValueError("This test build profile currently supports Linux only")
    harness_hash = digest(Path(__file__).read_bytes())
    # Snapshot potential native recipe dependencies before running preparation.
    # Only the actual recorded dependencies enter the artifact manifest below.
    recipe_snapshot = {path.relative_to(ROOT).as_posix(): digest(path.read_bytes())
        for path in (ROOT / "native").rglob("*")
        if path.is_file() and path.suffix in (".py", ".c", ".h", ".inc", ".json")}
    compiler = Path(compiler)
    if not compiler.is_absolute() or not compiler.is_file():
        raise ValueError("Select an existing compiler with an absolute path")
    source, destination = fresh_destination(source, destination)
    recipe = ROOT / "native" / FAMILIES[family]
    upstream_path = recipe / "source.json"
    upstream_hash = digest(upstream_path.read_bytes())
    upstream = json.loads(upstream_path.read_bytes())
    verify_files(source, upstream["files"], normalize=True)
    compiler_hash = digest(compiler.read_bytes())
    compiler_version = subprocess.check_output([str(compiler), "--version"], text=True)
    destination.mkdir(parents=True, exist_ok=False)
    baseline = destination / "verified-baseline"
    prepare_command = [sys.executable, "-B", str(recipe / "prepare.py"),
                       "--source", str(source), "--destination", str(baseline)]
    env = os.environ.copy()
    env.pop("PYTHONOPTIMIZE", None)
    completed = subprocess.run(prepare_command, env=env, capture_output=True, text=True)
    (destination / "prepare.log").write_text(completed.stdout + completed.stderr, encoding="utf-8")
    completed.check_returncode()
    record = json.loads((baseline / "prepared-source.json").read_bytes())
    expected_patch = ("easysewer:standard:5.2.4:16" if family == "standard" else
                      "easysewer:flexible-ponding:abi:201")
    if (record["base"] != upstream or record["patch"] != expected_patch or
            record["checkpoint"]["abi"] != 2 or
            (family == "custom" and record["native_io_fixes"] != 14)):
        raise ValueError("Unexpected prepared checkpoint family/profile")
    recipes = recipe_hashes(record, family)
    if any(recipe_snapshot.get(name) != expected for name, expected in recipes.items()):
        raise ValueError("Native recipe changed during preparation")
    baseline_hash = digest((baseline / "prepared-source.json").read_bytes())
    contents = verify_files(baseline, record["files"])
    verify_tree_inventory(baseline, record["files"], extra=("prepared-source.json",))
    contents, helpers = instrument(contents, family)
    staged = destination / "instrumented-source"
    for name, raw in contents.items():
        path = source_path(staged, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    evidence = dict(profile=PROFILE, test_only=True, production_qualified=False,
        family=family, upstream=upstream,
        upstream_manifest_sha256=upstream_hash,
        baseline=record, prepare_command=prepare_command,
        baseline_manifest_sha256=baseline_hash,
        preparation_recipes=recipes, instrumentation_helpers=helpers,
        harness_sha256=harness_hash,
        files={name: digest(raw) for name, raw in contents.items()})
    write_json(staged / "checkpoint-test-source.json", evidence)
    # Recheck all bytes and helper/recipe inputs immediately before compilation.
    verify_files(staged, evidence["files"])
    verify_tree_inventory(staged, evidence["files"], extra=("checkpoint-test-source.json",))
    recipe_hashes(record, family)
    for name, expected in helpers.items():
        if digest((ROOT / name).read_bytes()) != expected:
            raise ValueError("Instrumentation helper changed: " + name)
    if digest(compiler.read_bytes()) != compiler_hash:
        raise ValueError("Compiler changed before build")
    output = destination / ("checkpoint-test-" + family + ".so")
    flags = ["-shared", "-O2", "-fno-fast-math", "-ffp-contract=off", "-fopenmp",
             "-fPIC", "-Wall", "-Wextra", "-Werror=implicit-function-declaration",
             "-Wl,--no-undefined"]
    files = sorted(name for name in contents if name.startswith("src/solver/") and name.endswith(".c"))
    command = [str(compiler), *flags, "-I" + str(staged / "src/solver/include"),
               *(str(staged / name) for name in files), "-o", str(output), "-lm"]
    env["SOURCE_DATE_EPOCH"] = str(upstream.get("commit_timestamp", 0))
    source_manifest_hash = digest((staged / "checkpoint-test-source.json").read_bytes())
    completed = subprocess.run(command, cwd=destination, env=env, capture_output=True, text=True)
    (destination / "build.log").write_text(completed.stdout + completed.stderr, encoding="utf-8")
    build_record = dict(profile=PROFILE, test_only=True, production_qualified=False,
        family=family, source_manifest_sha256=source_manifest_hash,
        compiler=dict(path=str(compiler), resolved_path=str(compiler.resolve()),
                      sha256=compiler_hash, version=compiler_version),
        command=command, environment={"SOURCE_DATE_EPOCH": env["SOURCE_DATE_EPOCH"]},
        returncode=completed.returncode, output=str(output))
    # A concurrent edit while GCC reads the files must invalidate provenance,
    # even if compilation itself returned zero. Leave evidence, never success.
    try:
        verify_files(source, upstream["files"], normalize=True)
        verify_files(baseline, record["files"])
        verify_files(staged, evidence["files"])
        verify_tree_inventory(baseline, record["files"], extra=("prepared-source.json",))
        verify_tree_inventory(staged, evidence["files"], extra=("checkpoint-test-source.json",))
        recipe_hashes(record, family)
        for name, expected in helpers.items():
            if digest((ROOT / name).read_bytes()) != expected:
                raise ValueError("Instrumentation helper changed: " + name)
        for path, expected in ((Path(__file__), harness_hash), (compiler, compiler_hash),
                (upstream_path, upstream_hash),
                (baseline / "prepared-source.json", baseline_hash),
                (staged / "checkpoint-test-source.json", source_manifest_hash)):
            if digest(path.read_bytes()) != expected:
                raise ValueError("Build input changed: " + str(path))
    except (OSError, ValueError) as error:
        build_record.update(integrity_verified=False, integrity_error=str(error))
        write_json(destination / "checkpoint-test-build.json", build_record)
        raise
    build_record["integrity_verified"] = True
    if completed.returncode == 0:
        build_record.update(sha256=digest(output.read_bytes()), size=output.stat().st_size)
    write_json(destination / "checkpoint-test-build.json", build_record)
    completed.check_returncode()
    return build_record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path, help="Unmodified pinned upstream tree")
    parser.add_argument("--destination", required=True, type=Path, help="New directory outside repo/source")
    parser.add_argument("--compiler", required=True, help="Absolute GCC-compatible compiler path")
    parser.add_argument("--family", choices=tuple(FAMILIES), required=True)
    args = parser.parse_args()
    record = build(args.source, args.destination, args.compiler, args.family)
    print(json.dumps({key: record[key] for key in ("profile", "family", "output", "sha256")}, indent=2))


if __name__ == "__main__":
    main()
