"""Check the installed 2.0 entry points and main workflows without source imports."""
import argparse
import importlib
import json
from pathlib import Path
import runpy
import sys
import unittest


# Select families before importing tests. Missing required libraries must still
# fail a selected check; skips are never accepted as release qualification.
_SHARED_NATIVE_CLASSES = (
    ('test_native_v2_result_archive', 'NativeResultArchiveTests'),
    ('test_native_output_containment_v2', 'NativeOutputContainmentTests'),
    ('test_native_public_directory_checkpoint_v2', 'NativePublicDirectoryCheckpointTests'),
    ('test_native_output_directory_checkpoint_v2', 'NativeOutputDirectoryCheckpointTests'),
)
_STANDARD_IO_PORTABLE_METHODS = (
    'test_multiple_catchments_landuses_and_eight_pollutants_have_independent_mass_oracle',
    'test_invalid_headers_short_payloads_nonfinite_and_trailing_bytes_fail_in_c',
    'test_readonly_hotstart_and_repeated_failed_start_release_handles',
    'test_repeated_scratch_rain_runoff_and_output_do_not_leak_descriptors',
    'test_repeated_close_after_failed_open_or_running_project_is_safe',
    'test_climate_file_success_and_failure_close_in_same_process',
    'test_packaged_build_record_matches_binary_and_fixed_official_source',
)


def core_io_test_names(*, platform=None):
    """Choose platform-relevant I/O checks, not a suite with tolerated skips."""
    platform = sys.platform if platform is None else platform
    names = ['test_native_v2_output_io']
    if platform == 'win32':
        names += ['test_native_v2_standard_io.NativeStandardIOTests.' + method
                  for method in _STANDARD_IO_PORTABLE_METHODS]
    else:
        names.append('test_native_v2_standard_io.NativeStandardIOTests')
    return names


def default_test_names(*, pure=False, profile='all', platform=None):
    """Select all historical checks, core-only, experimental-only, or pure."""
    if pure:
        if profile != 'all':
            raise ValueError('pure cannot be combined with another profile')
        profile = 'pure'
    if profile not in ('all', 'core', 'experimental', 'pure'):
        raise ValueError('Unknown release qualification profile: ' + str(profile))
    platform = sys.platform if platform is None else platform
    if profile == 'experimental':
        names = ['test_native_v2_flexible'] + [
            module + '.Experimental' + cls for module, cls in _SHARED_NATIVE_CLASSES]
        if platform == 'win32':
            names += ['test_native_windows_error_mode.ExperimentalWindowsErrorModeTests',
                      'test_native_windows_paths.ExperimentalWindowsNativePathTests']
        return names
    names = ['test_public_api_v2', 'test_edit_workflow_v2', 'test_scenario_v2',
             'test_project_v2', 'test_json_v2', 'test_runner_v2', 'test_output_v2', 'test_report_v2']
    if profile != 'pure':
        names += ['test_native_platform_diagnostics', 'test_native_runtime_registry', 'test_doctor',
                  'test_native_path_aliases', 'test_backend_identity_v2',
                  'test_native_edit_workflow_v2', 'test_native_v2_runner',
                  'test_native_v2_scenario', 'test_native_v2_project', 'test_native_core_platform']
        if profile == 'all':
            names += ['test_native_v2_flexible'] + [module for module, _ in _SHARED_NATIVE_CLASSES]
        else:
            names += [module + '.' + cls for module, cls in _SHARED_NATIVE_CLASSES]
        names += core_io_test_names(platform=platform)
        if platform == 'win32' and profile == 'all':
            names += ['test_native_windows_error_mode', 'test_native_windows_paths']
        else:
            names.append('test_native_windows_error_mode.WorkerCommandHookTests')
            if platform == 'win32':
                names += ['test_native_windows_error_mode.StandardWindowsErrorModeTests',
                          'test_native_windows_paths.StandardWindowsNativePathTests']
    return names


def check_test_result(result, summary):
    """Every selected release check must actually pass, including under -O."""
    if (not result.testsRun or not result.wasSuccessful() or result.skipped or
            result.expectedFailures):
        raise RuntimeError('Release qualification failed; see tests.log: ' + json.dumps(summary))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    profiles = parser.add_mutually_exclusive_group()
    profiles.add_argument('--pure', dest='profile', action='store_const', const='pure',
                          help='Qualify the solver-free package')
    profiles.add_argument('--core', dest='profile', action='store_const', const='core',
                          help='Require standard solver and output, without experimental FlexiblePonding')
    profiles.add_argument('--experimental', dest='profile', action='store_const', const='experimental',
                          help='Require all selected experimental FlexiblePonding checks')
    parser.set_defaults(profile='all')
    parser.add_argument('--archive', type=Path)
    parser.add_argument('--tests', nargs='+', help='Explicit test names for a targeted recheck')
    args = parser.parse_args()
    # The entry-point checks and shipped example contain assertions, too.
    if sys.flags.optimize:
        parser.error('Release validation requires assertions; run without -O, -OO or PYTHONOPTIMIZE.')
    root = Path(__file__).resolve().parents[1]
    package, output = args.package.resolve(), args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    sys.path.insert(0, str(package))
    import easysewer
    assert Path(easysewer.__file__).resolve() == package / 'easysewer/__init__.py'
    from easysewer import Model
    assert Model is importlib.import_module('easysewer.model').Model
    assert 'easysewer.ModelAPI' not in sys.modules
    assert 'ctypes' not in sys.modules
    assert easysewer.__version__ == '2.0.1'
    from importlib.util import find_spec
    retired = ('LegacyModel', 'UrbanDrainageModel', 'JsonHandler', 'ControlList', 'ControlRule',
               'SWMMSolverAPI', 'FlexiblePondingSolverAPI', 'SWMMOutputAPI')
    assert all(not hasattr(easysewer, name) for name in retired)
    assert all(find_spec('easysewer.' + name) is None for name in
               ('ModelAPI', 'UDM', 'Area', 'Control', 'Curve', 'JsonHandler', 'Link', 'Node',
                'Options', 'Rain', 'SolverAPI', 'OutputAPI', 'compat'))
    sys.path.insert(1, str(root / 'tests'))
    sys.path.insert(2, str(root / 'examples'))
    selected = args.tests or default_test_names(profile=args.profile)
    suite = unittest.defaultTestLoader.loadTestsFromNames(selected)
    excluded = []
    if args.profile == 'pure':
        native_transport_test = 'test_runner_v2.RunnerPolicyTests.test_blocked_step_obeys_whole_run_deadline_and_external_cancellation'
        def profile_tests(suite):
            for item in suite:
                if isinstance(item, unittest.TestSuite):
                    yield from profile_tests(item)
                elif item.id() == native_transport_test:
                    excluded.append(dict(test=item.id(), reason='Requires native process transport, excluded from the pure package'))
                else:
                    yield item
        suite = unittest.TestSuite(profile_tests(suite))
    with (output / 'tests.log').open('x', encoding='utf-8') as stream:
        result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    summary = dict(version=easysewer.__version__, package=str(package),
                   tests=result.testsRun, failures=len(result.failures), errors=len(result.errors),
                   skips=[(str(test), reason) for test, reason in result.skipped],
                   expected_failures=[str(test) for test, _ in result.expectedFailures],
                   unexpected_successes=[str(test) for test in result.unexpectedSuccesses],
                   pure=args.profile == 'pure', profile=args.profile, selected_tests=selected,
                   targeted=bool(args.tests), excluded_by_profile=excluded,
                   native_core_evidence=getattr(sys.modules.get('test_native_core_platform'), 'EVIDENCE', []))
    (output / 'tests.json').write_text(json.dumps(summary, indent=2) + '\n', encoding='utf-8')
    check_test_result(result, summary)
    # Core/all execute the shipped solve and moved archive reads. Experimental
    # qualification must not implicitly require the separate standard backend.
    sys.argv = [str(root / 'examples/v2_first_run.py'), str(output / 'first-run')]
    if args.profile in ('pure', 'experimental'):
        sys.argv.append('--build-only')
    summary['first_run_mode'] = 'build-only' if '--build-only' in sys.argv else 'native'
    runpy.run_path(sys.argv[0], run_name='__main__')
    if args.archive:
        sys.argv = [str(root / 'examples/v2_first_run.py'), str(args.archive.resolve()), '--read-archive']
        runpy.run_path(sys.argv[0], run_name='__main__')
    summary['first_run_passed'] = True
    (output / 'result.json').write_text(json.dumps(summary, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(summary))


if __name__ == '__main__':
    main()
