"""Exact native hydraulic equivalence across model-requested thread counts.

Fixtures cover an interleaved multi-inlet/outlet hub, reverse flow, evaporation
and seepage, barrels, negative self-loop flow, PUMP2 with both regulator orders,
PUMP4, and storage with two legal outfalls sharing a time-varying stage.
Only the standard library is needed. Native loads occur in a fresh child; all
artifacts live in its parent's TemporaryDirectory. Public float64 traces and
all OUT bytes are compared, not every private Picard-iteration state.
"""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import tempfile
import unittest

import easysewer
from easysewer import get_native_capabilities
from easysewer.utils import probe_library_path

EVIDENCE = []
NODE_PROPERTIES = (303, 304, 305, 306, 307, 308)
LINK_PROPERTIES = (410, 411, 412, 413, 414)

OPTIONS = """[TITLE]
Accelerated hydraulic equivalence regression
[OPTIONS]
FLOW_UNITS CMS
FLOW_ROUTING DYNWAVE
START_DATE 01/01/2020
START_TIME 00:00:00
REPORT_START_DATE 01/01/2020
REPORT_START_TIME 00:00:00
END_DATE 01/01/2020
END_TIME 00:03:00
REPORT_STEP 00:00:01
ROUTING_STEP 1
VARIABLE_STEP 0
THREADS __THREADS__
IGNORE_QUALITY YES
[REPORT]
INPUT NO
CONTINUITY YES
FLOWSTATS YES
SUBCATCHMENTS ALL
NODES ALL
LINKS ALL
"""

def section(name, rows):
    return '[' + name + ']\n' + '\n'.join(rows) + '\n'

def model(*, mixed=None, order='orifice_first', loop=False):
    # Distinct terminal outfalls each have exactly one incident link.
    storage = []
    for i in range(8):
        storage.append(f'U{i:02} {11 + .1*i:.2f} 10 2 FUNCTIONAL 0 1 1200')
        storage.append(f'D{i:02} 9 10 {0 if mixed and i == 7 else 1 + .07*i:.2f} FUNCTIONAL 0 1 1200')
    outfalls = ['O 8 FREE NO', 'HIGH 9 FIXED 15 NO']
    conduits, sections, losses = [], [], []
    # Interleave inbound and outbound links; ID sorting would change sums.
    for i in range(8):
        for name, n1, n2, offset in [(f'UP{i:02}', f'U{i:02}', 'C', 0),
                                     (f'DN{i:02}', 'C', f'D{i:02}', 1)]:
            length = 2000 if name == 'UP03' else 90 + 11*i + offset*7
            conduits.append(f'{name} {n1} {n2} {length} .013 0 0 0 0')
            barrels = 2 if i % 3 == 0 else (3 if i % 3 == 1 else 1)
            shape = 'RECT_OPEN 5 20 0 0' if name == 'UP03' else f'CIRCULAR {1.2 + .13*i:.2f} 0 0 0'
            sections.append(f'{name} {shape} {barrels}')
            losses.append(f'{name} .1 .2 .3 NO 30')
    for name, n1, n2 in [('DRAIN', 'D00', 'O'), ('REV', 'C', 'HIGH')]:
        conduits.append(f'{name} {n1} {n2} 140 .013 0 0 0 0')
        sections.append(f'{name} CIRCULAR 1.6 0 0 0 2')
        losses.append(f'{name} .1 .2 .3 NO 30')
    if loop:
        # A negative initial circulation exercises both endpoint roles at one node.
        conduits.append('LOOP C C 1000 .013 0 0 -0.4 0')
        sections.append('LOOP CIRCULAR 1.2 0 0 0 3')
        losses.append('LOOP .1 .2 .3 NO 30')
    regulator_blocks, curves = '', ''
    if mixed:
        storage.append('REGHIGH 11 10 5 FUNCTIONAL 0 1 1200')
        outfalls.append('PUMPOUT 8 FREE NO')
        orifice = section('ORIFICES', ['ORIF C REGHIGH SIDE 1 .65 NO 0'])
        pump = section('PUMPS', ['PUMP C PUMPOUT PC ON 0 0'])
        regulator_blocks = orifice + pump if order == 'orifice_first' else pump + orifice
        # Demand exceeds incoming water at low head; PUMP2/PUMP4 limiting matters.
        curves = section('CURVES', [f'PC {mixed} 0 {0 if mixed == "PUMP4" else 200}', 'PC 8 250'])
        sections.append('ORIF CIRCULAR 1 0 0 0')
    nodes = section('JUNCTIONS', [f'C 10 10 {0.001 if mixed else 2} 0 0']) + section('STORAGE', storage) + section('OUTFALLS', outfalls)
    pipes = section('CONDUITS', conduits)
    # In orifice_first, nonconduit Link indices precede the true conduits.
    # In pump_first, nonconduits follow them, and pump/orifice order is reversed.
    links = regulator_blocks + pipes if order == 'orifice_first' else pipes + regulator_blocks
    text = OPTIONS + nodes + links + section('XSECTIONS', sections) + section('LOSSES', losses)
    text += section('EVAPORATION', ['CONSTANT 20']) + curves
    expected = {
        'group': 'mixed' if mixed else ('self_loop' if loop else 'hub'),
        'link_count': len(conduits) + (2 if mixed else 0),
        'node_count': 1 + len(storage) + len(outfalls),
        'negative_links': ['REV'] + (['ORIF'] if mixed else []) + (['LOOP'] if loop else []),
        'positive_links': ['UP00', 'DN07'] + (['PUMP'] if mixed else []),
        'nonzero_evaporation': True, 'nonzero_exfiltration': True,
        'barrels': [1, 2, 3], 'mixed_kind': mixed, 'order': order if mixed else None,
        'self_loop': loop, 'duration_seconds': 180,
    }
    return text, expected

def shared_stage_model():
    text, expected = model()
    text = text.replace('O 8 FREE NO', 'O 8 TIMESERIES STAGE NO', 1)
    text = text.replace('HIGH 9 FIXED 15 NO', 'HIGH 9 TIMESERIES STAGE NO', 1)
    text += section('TIMESERIES', ['STAGE 00:00:00 14', 'STAGE 00:00:45 13',
                                  'STAGE 00:01:30 15', 'STAGE 00:03:00 14'])
    expected['group'] = 'shared_stage'
    expected['shared_stage_outfalls'] = ['O', 'HIGH']
    return text, expected

def definitions():
    return {
        'hub_losses_reverse': model(),
        'shared_stage_outfalls': shared_stage_model(),
        'mixed_pump2_orifice_first': model(mixed='PUMP2', order='orifice_first'),
        'mixed_pump2_pump_first': model(mixed='PUMP2', order='pump_first'),
        'mixed_pump4_orifice_first': model(mixed='PUMP4', order='orifice_first'),
        'self_loop_losses_reverse': model(loop=True),
    }


def _require(condition, message):
    if not condition:
        raise AssertionError(message)


def _available_cpus():
    if hasattr(os, 'sched_getaffinity'):
        return max(1, len(os.sched_getaffinity(0)))
    if os.name == 'nt':
        import ctypes as c
        kernel = c.WinDLL('kernel32', use_last_error=True)
        kernel.GetCurrentProcess.restype = c.c_void_p
        kernel.GetProcessAffinityMask.argtypes = [c.c_void_p, c.POINTER(c.c_size_t), c.POINTER(c.c_size_t)]
        process, system = c.c_size_t(), c.c_size_t()
        if kernel.GetProcessAffinityMask(kernel.GetCurrentProcess(), c.byref(process), c.byref(system)):
            return max(1, process.value.bit_count())
    count = getattr(os, 'process_cpu_count', os.cpu_count)()
    return max(1, count or 1)


def _read_output(path):
    from easysewer.io.output_metadata import OutputMetadata
    metadata = OutputMetadata.read(path)
    raw = path.read_bytes()
    _require(metadata.periods > 0, 'Native output contains no saved periods')
    layout, offset = {}, 8
    for collection, codes in metadata.variable_codes:
        names = ('system',) if collection == 'swmm:system' else metadata.names(collection)
        layout[collection] = (names, codes, offset)
        offset += len(names) * len(codes) * 4
    _require(offset == metadata.period_bytes, 'OUT group layout does not cover each record')
    link_names, link_codes, link_offset = layout['swmm:links']
    node_names, node_codes, node_offset = layout['swmm:nodes']
    flows = {name: [math.inf, -math.inf] for name in link_names}
    heads = {name: [math.inf, -math.inf] for name in node_names if name in ('O', 'HIGH')}
    previous = -math.inf
    pump2_limited = False
    for period in range(metadata.periods):
        base = metadata.output_offset + period * metadata.period_bytes
        stamp, = struct.unpack_from('<d', raw, base)
        _require(math.isfinite(stamp) and stamp > previous, 'Invalid or unordered OUT timestamp')
        previous = stamp
        data = memoryview(raw)[base + 8:base + metadata.period_bytes]
        _require(all(math.isfinite(value) for (value,) in struct.iter_unpack('<f', data)),
                 'Nonfinite saved hydraulic result')
        for index, name in enumerate(link_names):
            value, = struct.unpack_from('<f', raw, base + link_offset +
                                        (index * len(link_codes) + link_codes.index(0)) * 4)
            flows[name][0] = min(flows[name][0], value)
            flows[name][1] = max(flows[name][1], value)
            if name == 'PUMP' and 0 < value < 200:
                pump2_limited = True
        for name in heads:
            value, = struct.unpack_from('<f', raw, base + node_offset +
                                        (node_names.index(name) * len(node_codes) + node_codes.index(1)) * 4)
            heads[name][0] = min(heads[name][0], value)
            heads[name][1] = max(heads[name][1], value)
    return metadata, flows, heads, pump2_limited


def _coverage(folder, expected, effective, self_loop):
    report = (folder/'model.rpt').read_text(encoding='utf-8', errors='replace')
    thread_match = re.search(r'Number of Threads\s*\.*\s*(\d+)', report)
    _require(thread_match is not None, 'RPT does not report actual thread count')
    actual = int(thread_match[1])
    _require(actual == effective, f'RPT threads {actual}, expected {effective}')
    _require(not re.search(r'\bERROR\s+\d+', report), 'Native report contains an error')
    metadata, flows, heads, pump2_limited = _read_output(folder/'model.out')
    _require(len(metadata.names('swmm:nodes')) == expected['node_count'], 'Missing reported nodes')
    _require(len(metadata.names('swmm:links')) == expected['link_count'], 'Missing reported links')
    for name in expected['negative_links']:
        _require(flows[name][0] < 0, f'{name} never exercised negative flow')
    for name in expected['positive_links']:
        _require(flows[name][1] > 0, f'{name} never exercised positive flow')
    if expected['mixed_kind'] == 'PUMP2':
        _require(pump2_limited, 'PUMP2 supply limiting was not exercised')
    if expected['self_loop']:
        _require(self_loop, 'LOOP native endpoint indices differ')
    routing = report.split('Flow Routing Continuity', 1)[-1]
    for label in ('Evaporation Loss', 'Exfiltration Loss'):
        match = re.search(re.escape(label) + r'\s*\.+\s*([+\-\d.eE]+)\s+([+\-\d.eE]+)', routing)
        _require(match is not None and any(float(value) > 0 for value in match.groups()),
                 f'{label} is absent or rounds to zero')
    for name in expected.get('shared_stage_outfalls', ()):
        _require(heads[name][1] > heads[name][0], f'{name} stage did not vary')
    return dict(periods=metadata.periods, actual_threads=actual, flow_ranges=flows)


def _run_case(solver_type, library, folder, text, expected, requested, effective):
    folder.mkdir(parents=True)
    inp, rpt, out = (folder/('model' + suffix) for suffix in ('.inp', '.rpt', '.out'))
    inp.write_text(text.replace('__THREADS__', str(requested)), encoding='utf-8')
    solver = solver_type(library)
    steps, self_loop = 0, False
    try:
        groups = dict(solver.open((inp, rpt, out))['groups'])
        nodes, links = groups['swmm:nodes'], groups['swmm:links']
        _require(len(links) >= 16, 'Fixture is too small to exercise four threads')
        if expected['self_loop']:
            index = links.index('LOOP')
            self_loop = solver.lib.swmm_getValue(401, index) == solver.lib.swmm_getValue(402, index)
        schema = dict(nodes=nodes, links=links, node_properties=NODE_PROPERTIES,
                      link_properties=LINK_PROPERTIES)
        solver.start(True)
        with (folder/'trace.bin').open('wb') as trace:
            while True:
                tick = solver.step(1)
                steps += tick['steps']
                values = [tick['elapsed_days']]
                for index in range(len(nodes)):
                    values.extend(solver.lib.swmm_getValue(code, index) for code in NODE_PROPERTIES)
                for index in range(len(links)):
                    values.extend(solver.lib.swmm_getValue(code, index) for code in LINK_PROPERTIES)
                _require(all(math.isfinite(value) for value in values), 'Nonfinite public double state')
                trace.write(struct.pack('<' + 'd' * len(values), *values))
                if tick['finished']:
                    break
                _require(steps < 1000, 'Short fixed-step fixture failed to finish')
        balance = solver.end()
        balance_bits = struct.pack('<3f', *(balance[key] for key in
            ('runoff_percent', 'flow_percent', 'quality_percent'))).hex()
        solver.report()
    finally:
        issues = solver.cleanup()
        _require(not issues, f'Native cleanup failed: {issues}')
    return dict(steps=steps, schema=schema, mass_balance_bits=balance_bits,
                coverage=_coverage(folder, expected, effective, self_loop))


def _exact_file(reference, candidate):
    left, right = reference.read_bytes(), candidate.read_bytes()
    if left != right:
        first = next((index for index, (a, b) in enumerate(zip(left, right)) if a != b),
                     min(len(left), len(right)))
        raise AssertionError(f'Bitwise difference in {reference.name}: first byte {first}, '
                             f'lengths {len(left)}/{len(right)}')
    return hashlib.sha256(left).hexdigest()


def _compare(reference, candidate, left, right):
    for field in ('steps', 'schema', 'mass_balance_bits'):
        _require(left[field] == right[field], f'Native {field} differs')
    _require(left['coverage']['periods'] == right['coverage']['periods'], 'Saved period count differs')
    return dict(out_sha256=_exact_file(reference/'model.out', candidate/'model.out'),
                public_double_trace_sha256=_exact_file(reference/'trace.bin', candidate/'trace.bin'))


def _native_child(spec_path):
    from easysewer.runtime._native_solver import NativeSolver
    from easysewer.runtime._native_accelerated import NativeAcceleratedSolver
    from easysewer.runtime._solver_worker import _configure_error_mode
    spec = json.loads(Path(spec_path).read_text(encoding='utf-8'))
    _require(Path(easysewer.__file__).resolve() == Path(spec['package_file']).resolve(),
             'Child imported a different easysewer package')
    _configure_error_mode()
    root, cases, evidence = Path(spec['work']), definitions(), []
    for requested in (1, 2, 4):
        effective = min(requested, spec['cpu_cap'])
        results = {}
        sequence = list(cases) + ['hub_losses_reverse_repeat']
        for backend, solver_type in (('standard', NativeSolver), ('accelerated', NativeAcceleratedSolver)):
            for key in sequence:
                name = key.removesuffix('_repeat')
                text, expected = cases[name]
                folder = root/str(requested)/backend/key
                results[backend, key] = _run_case(solver_type, spec[backend], folder,
                                                 text, expected, requested, effective)
        for key in sequence:
            left, right = results['standard', key], results['accelerated', key]
            paths = [root/str(requested)/backend/key for backend in ('standard', 'accelerated')]
            exact = _compare(*paths, left, right)
            evidence.append(dict(case=key, requested_threads=requested,
                cpu_cap=spec['cpu_cap'], effective_threads=effective,
                standard_threads=left['coverage']['actual_threads'],
                accelerated_threads=right['coverage']['actual_threads'],
                periods=left['coverage']['periods'], steps=left['steps'], **exact))
        for backend in ('standard', 'accelerated'):
            first, repeat = 'hub_losses_reverse', 'hub_losses_reverse_repeat'
            _compare(root/str(requested)/backend/first, root/str(requested)/backend/repeat,
                     results[backend, first], results[backend, repeat])
    (root/'evidence.json').write_text(json.dumps(evidence, indent=2)+'\n', encoding='utf-8')


class NativeAcceleratedHydraulicsTests(unittest.TestCase):
    def test_complete_output_public_doubles_thread_counts_and_reopen(self):
        capabilities = get_native_capabilities()
        standard = os.environ.get('EASYSEWER_ACCELERATED_TEST_STANDARD')
        accelerated = os.environ.get('EASYSEWER_ACCELERATED_TEST_LIBRARY')
        if not standard and not capabilities.get('swmm_solver'):
            self.skipTest('Packaged Standard or an explicit Standard test library is required')
        if not accelerated and not capabilities.get('swmm_accelerated'):
            self.skipTest('Packaged Accelerated or an explicit Accelerated test library is required')
        # Explicit invalid overrides fail in the child instead of silently skipping.
        standard = standard or probe_library_path('swmm5')
        accelerated = accelerated or probe_library_path('swmm-accelerated')
        capacity = min(4, _available_cpus())
        with tempfile.TemporaryDirectory(prefix='easysewer-accelerated-hydraulics-') as temporary:
            work = Path(temporary)
            spec = dict(standard=str(Path(standard).resolve()),
                        accelerated=str(Path(accelerated).resolve()),
                        cpu_cap=capacity, work=str(work), package_file=easysewer.__file__)
            spec_path = work/'spec.json'
            spec_path.write_text(json.dumps(spec), encoding='utf-8')
            environment = dict(os.environ, OMP_NUM_THREADS=str(capacity),
                OMP_THREAD_LIMIT=str(capacity), OMP_DYNAMIC='FALSE', PYTHONDONTWRITEBYTECODE='1',
                TEMP=str(work), TMP=str(work), TMPDIR=str(work))
            # Preserve the tested installation, including when the parent added
            # a src-layout package to sys.path rather than PYTHONPATH.
            environment['PYTHONPATH'] = os.pathsep.join((
                str(Path(easysewer.__file__).resolve().parent.parent),
                *filter(None, os.environ.get('PYTHONPATH', '').split(os.pathsep))))
            run = subprocess.run([sys.executable, '-B', str(Path(__file__).resolve()),
                                  '--native-child', str(spec_path)], cwd=work, env=environment,
                                 capture_output=True, text=True, encoding='utf-8', timeout=120)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            rows = json.loads((work/'evidence.json').read_text(encoding='utf-8'))
            self.assertEqual(len(rows), 21)
            self.assertEqual({row['requested_threads'] for row in rows}, {1, 2, 4})
            self.assertTrue(all(row['standard_threads'] == row['accelerated_threads'] ==
                                min(row['requested_threads'], capacity) for row in rows))
            EVIDENCE.extend(rows)


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--native-child':
        _native_child(sys.argv[2])
    else:
        unittest.main()
