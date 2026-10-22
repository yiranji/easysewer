"""Read-only native diagnostics, with an explicitly requested temporary smoke test.

Exit codes are stable: 0 ready, 2 CLI usage, 10 unsupported ABI, 11 missing or
invalid artifacts, 12 native load/ABI failure, 13 child failure/timeout, and
14 smoke solve/output failure. Optional experimental FlexiblePonding does not
change the standard solver/output readiness exit code.
"""
import argparse
import json
import math
from pathlib import Path
import subprocess
import tempfile

from . import __version__
from ._native_registry import NativeRegistryError, platform_error, runtime_identity, select_library

SCHEMA = 'easysewer:doctor:1'


def _run_worker(request, timeout):
    # Imported only after native discovery succeeds. The pure wheel deliberately
    # omits the executable helper and the native worker launch implementation.
    try:
        from .runtime._worker_launch import command, environment
        invocation = command(str(Path(__file__).resolve().parent.parent), module='easysewer._doctor_worker')
        completed = subprocess.run(invocation, input=json.dumps(request), text=True,
            encoding='utf-8', errors='backslashreplace', stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=timeout, cwd=Path(__file__).resolve().parent,
            env=environment(), creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except subprocess.TimeoutExpired:
        return dict(ok=False, exit_code=13, code='worker_timeout',
                    message=f'Isolated native diagnostic exceeded {timeout:g} seconds')
    except (OSError, RuntimeError, ImportError) as error:
        return dict(ok=False, exit_code=13, code='worker_launch', message=str(error))
    stderr = completed.stderr[-65536:]
    if completed.returncode:
        return dict(ok=False, exit_code=13, code='worker_exit',
                    message=f'Isolated native diagnostic exited with code {completed.returncode}',
                    returncode=completed.returncode, stderr=stderr)
    try:
        result = json.loads(completed.stdout)
        if (type(result) is not dict or type(result.get('ok')) is not bool
                or (result['ok'] and type(result.get('evidence')) is not dict)
                or (not result['ok'] and type(result.get('message')) is not str)):
            raise ValueError('Invalid worker response')
    except (ValueError, TypeError) as error:
        return dict(ok=False, exit_code=13, code='worker_protocol', message=str(error), stderr=stderr)
    if not result['ok']:
        return dict(ok=False, exit_code=14 if request['operation'] == 'smoke' else 12,
                    code='smoke_failed' if request['operation'] == 'smoke' else 'load_failed',
                    message=result.get('message', 'Native diagnostic failed'), stderr=stderr)
    return dict(ok=True, evidence=result.get('evidence', {}), stderr=stderr)


def diagnose(*, smoke_test=False, timeout=30):
    """Return a JSON-compatible report; never repair or modify an installation."""
    if type(smoke_test) is not bool:
        raise TypeError('smoke_test must be bool')
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 3600:
        raise ValueError('timeout must be finite and between 0 and 3600 seconds')
    runtime = runtime_identity()
    report = dict(schema=SCHEMA, package_version=__version__, runtime=runtime,
                  checks=[], smoke_test=dict(status='not_requested'), ready=False, exit_code=0)
    reason = platform_error()
    if reason:
        report['checks'].append(dict(name='platform', required=True, status='error',
                                     code='unsupported_abi', message=reason))
        report['exit_code'] = 10
        return report
    for name in ('swmm5', 'swmm-output', 'flexible_ponding'):
        required = name != 'flexible_ponding'
        check = dict(name=name, required=required, experimental=not required)
        if not required:
            check['note'] = ('Scientifically unvalidated replacement-backend example; '
                             'availability is not scientific validation and no fallback is performed')
        try:
            artifact = select_library(name, verify=True)
            check['artifact'] = dict(path=str(artifact.path), sha256=artifact.record['sha256'],
                size=artifact.record['size'], numerical_policy=artifact.record['numerical_policy'],
                target=artifact.target_key, abi=artifact.target['abi'],
                minimum_os=artifact.target['minimum_os'], libc=artifact.target['libc'],
                dependencies=artifact.record['dependencies'],
                external_dependencies=artifact.record['external_dependencies'])
        except NativeRegistryError as error:
            check.update(status='error' if required else 'absent', code=error.code, message=str(error))
            if required and not report['exit_code']:
                report['exit_code'] = 10 if error.code in ('unsupported_abi', 'runtime_incompatible') else 11
        else:
            result = _run_worker(dict(operation='probe', name=name), timeout)
            check.update(status='ok' if result['ok'] else 'error', **{k: v for k, v in result.items() if k != 'ok'})
            if not result['ok'] and required and not report['exit_code']:
                report['exit_code'] = result['exit_code']
        report['checks'].append(check)
    if smoke_test:
        if report['exit_code']:
            report['smoke_test'] = dict(status='not_run', message='Required native diagnostics did not pass')
        else:
            # Parent owns cleanup, including when a child crashes or times out.
            # No temporary directory is created by ordinary read-only checks.
            try:
                with tempfile.TemporaryDirectory(prefix='easysewer-doctor-') as directory:
                    result = _run_worker(dict(operation='smoke', directory=directory), timeout)
                report['smoke_test'] = dict(status='ok' if result['ok'] else 'error',
                                            **{k: v for k, v in result.items() if k != 'ok'})
                if not result['ok']:
                    report['exit_code'] = result['exit_code']
            except OSError as error:
                report['smoke_test'] = dict(status='error', code='temporary_files', message=str(error))
                report['exit_code'] = 14
    report['ready'] = report['exit_code'] == 0
    return report


def _timeout(value):
    try:
        result = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError('timeout must be a number') from None
    if not math.isfinite(result) or not 0 < result <= 3600:
        raise argparse.ArgumentTypeError('timeout must be between 0 and 3600 seconds')
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(prog='easysewer', description='EasySewer native runtime tools')
    commands = parser.add_subparsers(dest='command', required=True)
    doctor = commands.add_parser('doctor', help='Check native files and isolated load/ABI readiness without solving')
    doctor.add_argument('--json', action='store_true', help='Emit the versioned machine-readable report')
    doctor.add_argument('--smoke-test', action='store_true', help='Also solve and read a minimal model in temporary files')
    doctor.add_argument('--timeout', type=_timeout, default=30, help='Maximum seconds per isolated check (default: 30)')
    args = parser.parse_args(argv)
    report = diagnose(smoke_test=args.smoke_test, timeout=args.timeout)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, allow_nan=False))
    else:
        identity = report['runtime']
        print(f"EasySewer {__version__}: {identity['system']} {identity['architecture']} "
              f"({identity['pointer_bits']}-bit Python)")
        for check in report['checks']:
            optional = ' (optional, experimental)' if not check['required'] else ''
            print(f"{check['name']}{optional}: {check['status']}")
            if check.get('message'):
                print('  ' + check['message'])
            if check.get('artifact', {}).get('external_dependencies'):
                print('  Legacy source artifacts require external dependencies: ' +
                      ', '.join(check['artifact']['external_dependencies']))
        if args.smoke_test:
            print('Smoke test: ' + report['smoke_test']['status'])
            if report['smoke_test'].get('message'):
                print('  ' + report['smoke_test']['message'])
        print('Standard solver/output ready' if report['ready'] else f"Not ready (exit {report['exit_code']})")
    return report['exit_code']
