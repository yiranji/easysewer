"""Exercise the isolated Horton correction through whole SWMM projects.

The one-catchment fixtures use literal INP records and native C entry points.
Unchanged methods must preserve complete OUT bytes. Modified Horton finite-cap
cases deliberately change results; bounds and state persistence are checked
separately. HOTSTART transfers physical state, not the complete simulation clock.
"""
import argparse
import ctypes as c
import hashlib
import json
import math
from pathlib import Path
import platform
import struct
import sys

from qualify_horton_capacity import Trial


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def fixture(units, case):
    metric = units in ('CMS', 'LPS', 'MLD')
    depth = 25.4 if metric else 1.
    method = {
        'horton': 'HORTON', 'green-ampt': 'GREEN_AMPT',
        'modified-green-ampt': 'MODIFIED_GREEN_AMPT', 'curve-number': 'CURVE_NUMBER',
    }.get(case, 'MODIFIED_HORTON')
    cap = 0. if case == 'unlimited' else (.01 if case == 'small-cap' else .5)
    maximum = .2 if case == 'constant-rate' else 2.
    decay = 0. if case == 'zero-decay' else 4.
    if 'GREEN_AMPT' in method:
        soil = f'{3*depth:.17g} {.2*depth:.17g} .3'
    elif method == 'CURVE_NUMBER':
        soil = '75 0 2'
    else:
        soil = f'{maximum*depth:.17g} {.2*depth:.17g} {decay} 2 {cap*depth:.17g}'
    # All six flow-unit choices represent the same physical catchment.
    area = .40468564224 if metric else 1.
    width = 30.48 if metric else 100.
    text = f'''[OPTIONS]
FLOW_UNITS {units}
INFILTRATION {method}
FLOW_ROUTING KINWAVE
START_DATE 01/01/2020
END_DATE 01/01/2020
END_TIME 01:00:00
REPORT_STEP 00:00:01
WET_STEP 00:00:10
DRY_STEP 00:01:00
ROUTING_STEP 1
VARIABLE_STEP 0
[RAINGAGES]
R INTENSITY 00:01:00 1 TIMESERIES Rain
[OUTFALLS]
O 0 FREE
[SUBCATCHMENTS]
S R O {area:.17g} 0 {width:.17g} 1 0
[SUBAREAS]
S .015 .2 0 0 25 OUTLET
[INFILTRATION]
S {soil}
[REPORT]
SUBCATCHMENTS ALL
NODES ALL
[TIMESERIES]
'''
    for minute in range(61):
        # Separate pulses allow actual dry-state recovery and subsequent rewet.
        rain = (1. if 5 <= minute < 15 or 40 <= minute < 50 else 0.) * depth
        text += f'Rain {minute//60:02d}:{minute%60:02d}:00 {rain:.17g}\n'
    return text


def run(trial, folder, source, *, stop=None, save=None, use=None):
    from easysewer.runtime._output_api import SWMMOutputAPI
    folder.mkdir(parents=True, exist_ok=False)
    if save is None:
        save = folder/'final.hsf'
    if save is not None:
        source += f'[FILES]\nSAVE HOTSTART "{save}"\n'
    if use is not None:
        source += f'[FILES]\nUSE HOTSTART "{use}"\n'
    inp, rpt, out = [folder / ('model.' + suffix) for suffix in ('inp', 'rpt', 'out')]
    inp.write_text(source, encoding='ascii')
    dll = trial.dll
    for name, args in {
        'swmm_open': [c.c_char_p]*3, 'swmm_start': [c.c_int],
        'swmm_step': [c.POINTER(c.c_double)], 'swmm_end': [],
        'swmm_report': [], 'swmm_close': [],
        'easysewer_horton_project': [c.c_int, c.POINTER(c.c_double), c.c_int],
        'swmm_getMassBalErr': [c.POINTER(c.c_float)]*3,
    }.items():
        getattr(dll, name).argtypes = args
        getattr(dll, name).restype = c.c_int

    def checked(name, *args):
        code = getattr(dll, name)(*args)
        if code:
            raise RuntimeError(f'{name}: {code}; report: {rpt}')

    def state():
        values = (c.c_double*8)()
        return list(values) if dll.easysewer_horton_project(0, values, 8) == 1 else None

    trace = []
    started = False
    try:
        checked('swmm_open', *(str(p).encode('ascii') for p in (inp, rpt, out)))
        checked('swmm_start', 1)
        started = True
        initial = state()
        if initial is not None:
            trace.append(initial)
        elapsed = c.c_double()
        for count in range(1, 10001):
            checked('swmm_step', c.byref(elapsed))
            current = state()
            if current is not None and (not trace or current[0] != trace[-1][0]):
                trace.append(current)
            if elapsed.value == 0 or (stop is not None and elapsed.value*86400 >= stop):
                break
        else:
            raise RuntimeError('Native project exceeded step bound')
        final = state()
        checked('swmm_end')
        started = False
        balance = [c.c_float() for _ in range(3)]
        checked('swmm_getMassBalErr', *(c.byref(x) for x in balance))
        checked('swmm_report')
    finally:
        if started:
            checked('swmm_end')
        checked('swmm_close')
    reader = SWMMOutputAPI()
    try:
        reader.open(str(out))
        periods = reader.get_times(1)
        series = {name: list(reader.get_subcatch_series(0, index, 0, periods))
                  for name, index in [('rain', 0), ('infiltration', 3), ('runoff', 4)]}
    finally:
        if reader.lib.SMO_close(c.byref(reader.handle)):
            raise RuntimeError('Output reader close failed')
    raw_state = save.read_bytes()
    if raw_state[:15] != b'SWMM5-HOTSTART4':
        raise ValueError('Unexpected native HOTSTART version')
    result = dict(initial=initial, final=final, trace=trace, periods=periods,
                  final_ponded_depths_ft=struct.unpack_from('<3d', raw_state, 39),
                  steps=count, balance_percent=[x.value for x in balance],
                  series=series, artifact_sha256={p.name: digest(p) for p in (inp, rpt, out, save)})
    (folder/'result.json').write_text(json.dumps(result, indent=2)+'\n', encoding='utf-8')
    return result


def qualify(pristine, candidate, destination):
    from easysewer.runtime._solver_worker import _configure_error_mode
    from easysewer.io.hotstart import HotstartData, HotstartLayout
    from easysewer.io.inp import InpDocument
    from easysewer.model import Model
    _configure_error_mode()
    original, fixed = Trial(pristine, 'pristine'), Trial(candidate, 'minimum-cap')
    changed = [key for key in original.build['source_digests']
               if original.build['source_digests'][key] != fixed.build['source_digests'].get(key)]
    if changed != ['src/solver/infil.c'] or original.build['probe_sha256'] != fixed.build['probe_sha256']:
        raise ValueError('Unexpected changes between experiment libraries')
    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=False)
    checks, cases = [], []
    # EPA 5.2.4 project_readInput floors the double date difference to seconds.
    # 2020-01-01 is serial day 43831; this one-hour literal therefore ends at
    # 3599 s in BOTH pristine builds. Do not change that unrelated equation here.
    expected_periods = math.floor(((43831. + 1./24.) - 43831.) * 86400.)

    def check(name, passed, **details):
        checks.append(dict(name=name, passed=bool(passed), **details))

    for units in ('CFS', 'GPM', 'MGD', 'CMS', 'LPS', 'MLD'):
        for case in ('horton', 'green-ampt', 'modified-green-ampt', 'curve-number',
                     'unlimited', 'constant-rate', 'zero-decay', 'finite-cap', 'small-cap'):
            folder = destination/(units+'-'+case)
            source = fixture(units, case)
            a = run(original, folder/'pristine', source)
            b = run(fixed, folder/'candidate', source)
            affected = case in ('finite-cap', 'small-cap')
            same = a['artifact_sha256']['model.out'] == b['artifact_sha256']['model.out']
            check('changed_output' if affected else 'unchanged_complete_output',
                  same != affected, units=units, case=case)
            check('full_periods_and_active_rain_infiltration', b['periods'] == a['periods'] == expected_periods
                  and all(max(b['series'][key]) > 0 for key in ('rain', 'infiltration')), units=units, case=case)
            if affected:
                trace = b['trace']
                check('finite_capacity_state_bound', all(0 <= row[7] <= row[5] for row in trace), units=units, case=case)
                if case == 'finite-cap':
                    check('dry_state_recovery', any(right[7] < left[7] and 1500 <= right[0] <= 2300
                          for left, right in zip(trace, trace[1:])), units=units, case=case)
                    check('second_pulse_infiltration', max(b['series']['infiltration'][2400:3000]) > 0, units=units, case=case)
                else:
                    check('saturated_ponded_state_has_no_dry_recovery', b['final_ponded_depths_ft'][2] > 0
                          and all(row[7] == row[5] for row in trace if 1500 <= row[0] <= 3000)
                          and max(b['series']['runoff']) > 0, units=units, case=case)
                    check('saturated_second_pulse_has_zero_infiltration',
                          max(b['series']['infiltration'][2400:3000]) == 0, units=units, case=case)
                check('more_infiltration_than_false_saturation', sum(b['series']['infiltration']) >
                      sum(a['series']['infiltration']), units=units, case=case)
            cases.append(dict(units=units, case=case, pristine_out=a['artifact_sha256']['model.out'],
                              candidate_out=b['artifact_sha256']['model.out'],
                              pristine_balance=a['balance_percent'], candidate_balance=b['balance_percent'],
                              pristine_infiltration_sum=sum(a['series']['infiltration']),
                              candidate_infiltration_sum=sum(b['series']['infiltration'])))

    hotstarts = []
    for units in ('CFS', 'CMS'):
        source = fixture(units, 'finite-cap')
        layout = HotstartLayout.from_model(Model.from_document(InpDocument.from_text(source), strict=True))
        for name, producer in [('pristine', original), ('candidate', fixed)]:
            folder = destination/('hotstart-'+units+'-'+name)
            folder.mkdir()
            path, rewritten = folder/'state.hsf', folder/'rewritten.hsf'
            produced = run(producer, folder/'producer', source, stop=720, save=path)
            raw = path.read_bytes()
            parsed = HotstartData.from_bytes(raw, layout=layout)
            parsed.write(rewritten)
            # Independent v4 fixture byte layout: header+six counts, four doubles,
            # then Horton tp and Fe. Remaining infiltration slots are not asserted.
            expected = tuple(produced['final'][6:8])
            check('hotstart_native_binary_state', raw[:15] == b'SWMM5-HOTSTART4' and
                  struct.unpack_from('<2d', raw, 39+32) == expected,
                  units=units, producer=name)
            check('typed_hotstart_exact_bytes', raw == rewritten.read_bytes(), units=units, producer=name)
            loaded = run(fixed, folder/'load-original', source, use=path)
            rewritten_loaded = run(fixed, folder/'load-rewritten', source, use=rewritten)
            check('candidate_loads_existing_state_exactly', tuple(loaded['initial'][6:8]) == expected,
                  units=units, producer=name)
            check('rewritten_hotstart_output_identical', loaded['artifact_sha256']['model.out'] ==
                  rewritten_loaded['artifact_sha256']['model.out'], units=units, producer=name)
            hotstarts.append(dict(units=units, producer=name, state=list(expected), sha256=digest(path),
                                 consumer_out=loaded['artifact_sha256']['model.out']))
    record = dict(passed=all(x['passed'] for x in checks), checks=checks, cases=cases, hotstarts=hotstarts,
                  native_runs=120, python=sys.version, platform=platform.platform(),
                  pristine_sha256=digest(pristine), candidate_sha256=digest(candidate),
                  harness_sha256=digest(__file__), adopted=False,
                  scope='Whole-project results, native state bounds, dry recovery, wet pulses, and HOTSTART physical-state persistence.',
                  expected_report_periods=expected_periods,
                  remaining=['Full worker checkpoint compatibility', 'Constant-rate and zero-decay finite-cap semantics',
                             'Formal standard/custom integration and numerical identity change'])
    (destination/'result.json').write_text(json.dumps(record, indent=2)+'\n', encoding='utf-8')
    return record


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pristine', type=Path, required=True)
    parser.add_argument('--candidate', type=Path, required=True)
    parser.add_argument('--destination', type=Path, required=True)
    args = parser.parse_args()
    result = qualify(args.pristine, args.candidate, args.destination)
    print(json.dumps(dict(passed=result['passed'], checks=len(result['checks']),
                         failed=[x for x in result['checks'] if not x['passed']], native_runs=result['native_runs'])))
    raise SystemExit(not result['passed'])
