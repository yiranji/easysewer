"""Validate result archive schemas against codec, directory and historical data."""
import argparse
from collections import Counter
import hashlib
import importlib.metadata
import io
import json
from pathlib import Path
import platform
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--validator-path', type=Path, required=True)
    parser.add_argument('--destination', type=Path, required=True)
    args = parser.parse_args()
    for folder in (ROOT/'src', ROOT/'tests', args.validator_path):
        sys.path.insert(0, str(folder))
    from jsonschema import Draft202012Validator
    from easysewer.runtime import archive, RunResult
    from build_result_schemas import VERSIONS, build
    args.destination.mkdir(parents=True, exist_ok=False)
    sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
    validators = {}
    schemas = {}
    for version in VERSIONS:
        path = ROOT/'docs'/f'result-archive-{version}.schema.json'
        data = json.loads(path.read_bytes())
        assert data == build(version)
        Draft202012Validator.check_schema(data)
        validators[version] = Draft202012Validator(data)
        schemas[path.name] = sha(path)
    seen = []

    def validate(path, origin):
        manifest = path/'result.json'
        raw = manifest.read_bytes(); envelope = json.loads(raw)
        version = envelope['schema_version']
        validators[version].validate(envelope)
        # Save evidence before fixture teardown. Blob integrity is checked by the
        # actual save/load path, not inferred from this structural validation.
        target = args.destination/f'manifest-{len(seen):03d}.json'
        target.write_bytes(raw)
        seen.append(dict(origin=origin, version=version, status=envelope['result']['fields']['status'],
                         manifest=target.name, sha256=sha(target), bytes=len(raw)))

    original = archive.save_result

    def saved(result, directory, **kwargs):
        target = original(result, directory, **kwargs)
        validate(target, 'existing-regression-public-save')
        return target

    names = [
        'test_result_schemas',
        'test_directory_artifact_v2.DirectoryArtifactTests.test_failed_result_archive_relocation_materialization_and_empty_dirs',
        'test_mutable_directory_v2.MutableDirectoryTests.test_failed_archive_roundtrip_preserves_initial_and_current_after_source_deletion',
        'test_optional_mutable_directory_v2.OptionalMutableDirectoryTests.test_result_archive_and_success_contract_preserve_both_absence_transitions',
        'test_optional_mutable_directory_v2.OptionalMutableDirectoryTests.test_failed_absent_archive_materializes_no_directory_and_can_be_resaved',
        'test_hardlink_directory_v2.HardlinkDirectoryTests.test_archive_relocation_preserves_aliases_without_linking_to_deduplicated_blobs',
        'test_directory_group_integration_v2.DirectoryGroupIntegrationTests.test_result_group_archive_restores_layout_and_aliases_without_original_paths',
        'test_mixed_resource_graph_v2.MixedResourceGraphTests.test_readonly_mixed_graph_uses_one_forest_and_archive_restores_file_view',
        'test_mixed_resource_graph_v2.MixedResourceGraphTests.test_current_alias_creation_and_breakage_survive_result_archives',
    ]
    stream = io.StringIO()
    with patch.object(archive, 'save_result', saved):
        outcome = unittest.TextTestRunner(stream=stream, verbosity=2).run(
            unittest.defaultTestLoader.loadTestsFromNames(names))
    (args.destination/'tests.txt').write_text(stream.getvalue(), encoding='utf-8')
    print(stream.getvalue())
    historical = [ROOT/'tests/fixtures/diagnostic_legacy'/f'result-1.{i}' for i in (0, 1)]
    historical += [ROOT/'tests/fixtures/report_archive_1_2/result']
    for path in historical:
        before = sha(path/'result.json')
        loaded = RunResult.load(path)
        validate(path, str(path.relative_to(ROOT)))
        assert sha(path/'result.json') == before
        seen[-1]['actual_loader_status'] = loaded.status
    counts = Counter(row['version'] for row in seen)
    expected = {'1.0','1.1','1.2','1.3','1.4','1.5','1.6','1.7','1.8','1.9'}
    assert set(counts) == expected, counts
    record = dict(tests_run=outcome.testsRun, failures=len(outcome.failures), errors=len(outcome.errors),
                  skipped=len(outcome.skipped), success=outcome.wasSuccessful() and not outcome.skipped,
                  python=sys.version, platform=platform.platform(), schemas=schemas,
                  jsonschema_version=importlib.metadata.version('jsonschema'),
                  versions=counts, manifests=seen, historical_archives_loaded=len(historical),
                  tool_sha256=sha(Path(__file__)), generator_sha256=sha(ROOT/'tools/build_result_schemas.py'),
                  tests_sha256=sha(ROOT/'tests/test_result_schemas.py'),
                  production_changed=False, complete_R03=False,
                  limitations=['Structural schemas do not replace RunResult.load integrity and semantic checks.',
                               'Short archive checks overlap the non-exclusive Linux performance matrix.',
                               'Development validation on Windows Python 3.13; no final package or public API freeze.'])
    (args.destination/'qualification.json').write_text(json.dumps(record, indent=2)+'\n', encoding='utf-8')
    assert record['success'], 'Qualification tests failed; original log retained'
    print(json.dumps(dict(success=True, tests=outcome.testsRun, versions=counts, manifests=len(seen))))


if __name__ == '__main__':
    main()
