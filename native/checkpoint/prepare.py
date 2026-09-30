"""Stage internal checkpoint modules on a verified, already prepared engine.

This development recipe is deliberately separate from installed engine builds:
it retains standard12/custom10 as a historical input baseline. Production
preparation uses the same patch.py directly on its verified base contents.
"""
import argparse
import hashlib
import json
import runpy
from pathlib import Path


def prepare(source, destination):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if destination == source or destination.is_relative_to(source):
        raise ValueError("Destination must be outside the source")
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("Destination must be empty")
    record = json.loads((source / "prepared-source.json").read_text(encoding="utf-8"))
    if not ((record.get("patch") == "easysewer:standard:5.2.4:12") or
            (record.get("patch") == "easysewer:flexible-ponding:abi:201" and
             record.get("native_io_fixes") == 10)) or record.get("report_io") != 1 or record.get("lid_report_io") != 1:
        raise ValueError("Requires qualified standard12 or custom10 source with LID report I/O")
    contents = {}
    for name, digest in record["files"].items():
        path = source / name
        if not path.resolve().is_relative_to(source):
            raise ValueError("Source manifest path escapes tree")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError("Prepared source changed: " + name)
        contents[name] = raw
    root = Path(__file__).resolve().parent
    recipe = runpy.run_path(str(root/'patch.py'))
    recipe['patch'](contents, custom=record['patch']=='easysewer:flexible-ponding:abi:201')
    for name, raw in contents.items():
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    evidence = dict(
        kind="easysewer:checkpoint-development", version=26, event_rule_clock_fix=1,
        inactive_routing_clock_fix=1,
        paraboloid_exfil_fix=1,
        inlet_sides_fix=1,
        groundwater_conductivity_fix=1,
        snow_initialization_fix=1,
        status="historical-baseline checkpoint ABI2 development tree; production uses the shared patch in formal preparation",
        base_manifest_sha256=hashlib.sha256((source / "prepared-source.json").read_bytes()).hexdigest(),
        base=record,
        recipes={name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                 for name in ('prepare.py', *recipe['RECIPE_FILES'])},
        files={name: hashlib.sha256(raw).hexdigest() for name, raw in contents.items()},
    )
    (destination / "checkpoint-source.json").write_text(
        json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    return evidence


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--destination", required=True)
    args = parser.parse_args()
    evidence = prepare(args.source, args.destination)
    print(json.dumps(dict(status=evidence["status"], files=len(evidence["files"]))))
