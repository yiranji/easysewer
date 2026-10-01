"""Check the installed 2.0 entry points and main workflows without source imports."""
import argparse
import importlib
import json
from pathlib import Path
import runpy
import sys
import unittest


def check_test_result(result, summary):
    """Reject empty, skipped and unsuccessful runs, including under python -O."""
    if not result.testsRun or not result.wasSuccessful() or result.skipped:
        raise RuntimeError('Release qualification failed; see tests.log: ' + json.dumps(summary))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--pure', action='store_true')
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
    assert easysewer.__version__ == '2.0.0'
    from importlib.util import find_spec
    retired = ('LegacyModel', 'UrbanDrainageModel', 'JsonHandler', 'ControlList', 'ControlRule',
               'SWMMSolverAPI', 'FlexiblePondingSolverAPI', 'SWMMOutputAPI')
    assert all(not hasattr(easysewer, name) for name in retired)
    assert all(find_spec('easysewer.' + name) is None for name in
               ('ModelAPI', 'UDM', 'Area', 'Control', 'Curve', 'JsonHandler', 'Link', 'Node',
                'Options', 'Rain', 'SolverAPI', 'OutputAPI', 'compat'))
    sys.path.insert(1, str(root / 'tests'))
    sys.path.insert(2, str(root / 'examples'))
    names = ['test_public_api_v2', 'test_edit_workflow_v2', 'test_scenario_v2',
             'test_project_v2', 'test_json_v2', 'test_runner_v2', 'test_output_v2', 'test_report_v2']
    if not args.pure:
        names += ['test_native_platform_diagnostics', 'test_backend_identity_v2',
                  'test_native_edit_workflow_v2', 'test_native_v2_runner',
                  'test_native_v2_flexible', 'test_native_v2_result_archive',
                  'test_native_v2_scenario', 'test_native_v2_project']
        if sys.platform == 'win32':
            names += ['test_native_windows_error_mode']
    suite = unittest.defaultTestLoader.loadTestsFromNames(args.tests or names)
    excluded = []
    if args.pure:
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
                   skips=result.skipped, pure=args.pure, excluded_by_profile=excluded)
    (output / 'tests.json').write_text(json.dumps(summary, indent=2) + '\n', encoding='utf-8')
    check_test_result(result, summary)
    # Execute the shipped example, including native solve and moved archive reads.
    sys.argv = [str(root / 'examples/v2_first_run.py'), str(output / 'first-run')]
    if args.pure:
        sys.argv.append('--build-only')
    runpy.run_path(sys.argv[0], run_name='__main__')
    if args.archive:
        sys.argv = [str(root / 'examples/v2_first_run.py'), str(args.archive.resolve()), '--read-archive']
        runpy.run_path(sys.argv[0], run_name='__main__')
    summary['first_run_passed'] = True
    (output / 'result.json').write_text(json.dumps(summary, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(summary))


if __name__ == '__main__':
    main()
