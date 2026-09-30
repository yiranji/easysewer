"""Record dry-start mass balance without changing the solver state.

Uses the captured Case 2 no-catchment input. Subdivision preserves total length,
end elevations, slope, section and roughness; added junctions have no inflows.
Results are diagnostic evidence, not a numerical acceptance or native fix.
"""
import argparse
import csv
import ctypes as c
import hashlib
import heapq
import json
from pathlib import Path
import sys


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_input(text, routing, divisions):
    sections = {}
    section = None
    for line in text.splitlines():
        if line.startswith('['):
            section = line
            sections[section] = []
        elif line.strip() and not line.startswith('TEMPDIR '):
            sections[section].append(line)
    sections['[OPTIONS]'] = [f'FLOW_ROUTING {routing}' if x.startswith('FLOW_ROUTING ')
                             else x for x in sections['[OPTIONS]']]
    if divisions > 1:
        nodes = {r.split()[0]: float(r.split()[1]) for s in ('[JUNCTIONS]', '[OUTFALLS]')
                 for r in sections[s]}
        shapes = {r.split()[0]: r.split()[1:] for r in sections['[XSECTIONS]']}
        links, xsections = [], []
        for line in sections['[CONDUITS]']:
            name, n1, n2, length, roughness, off1, off2, initial, maximum = line.split()
            assert float(off1) == float(off2) == float(initial) == 0
            chain = [n1] + [f'{name}_n{i}' for i in range(1, divisions)] + [n2]
            for i in range(1, divisions):
                elevation = nodes[n1] + (nodes[n2]-nodes[n1])*i/divisions
                sections['[JUNCTIONS]'].append(f'{chain[i]} {elevation:.16g} 3 0 0 0')
            for i in range(divisions):
                key = f'{name}_{i}'
                links.append(f'{key} {chain[i]} {chain[i+1]} {float(length)/divisions:.16g} '
                             f'{roughness} 0 0 0 {maximum}')
                xsections.append(' '.join([key, *shapes[name]]))
        sections['[CONDUITS]'] = links
        sections['[XSECTIONS]'] = xsections
    return ''.join(s+'\n'+'\n'.join(rows)+'\n' for s, rows in sections.items())


def run(library, source, directory, routing, divisions, kinwave_weight=.6):
    if not .5 <= kinwave_weight <= 1.:
        raise ValueError('Diagnostic KINWAVE weight must be between .5 and 1')
    from easysewer.runtime._native_solver import NativeSolver
    from easysewer.runtime._solver_worker import _configure_error_mode
    _configure_error_mode()
    directory.mkdir(parents=True, exist_ok=False)
    inp, rpt, out = [directory/('model.'+ext) for ext in ('inp', 'rpt', 'out')]
    inp.write_text(make_input(source.read_text(), routing, divisions), encoding='utf-8')
    solver = NativeSolver(library)
    probe = getattr(solver.lib, 'easysewer_probe', None)
    if probe:
        probe.argtypes = [c.c_int]*3
        probe.restype = c.c_double
    largest = []
    snapshots = []
    previous = 0.0
    steps = 0
    kw_equation = None
    def state():
        totals = [probe(0, 0, i) for i in range(5)]
        # All accumulated quantities converted from ft3 to m3.
        return [totals[0], *[v*0.3048**3 for v in totals[1:]],
                (totals[1]+totals[2]-totals[3]-totals[4])*0.3048**3]
    def details():
        return dict(nodes=[[probe(1, j, f) for f in range(4)]
                           for j in range(solver.lib.swmm_getCount(2))],
                    links=[[probe(2, j, f) for f in range(10)]
                           for j in range(solver.lib.swmm_getCount(3))])
    try:
        metadata = solver.open((inp, rpt, out))
        solver.start(True)
        if probe and routing == 'KINWAVE':
            lengths = [solver.lib.swmm_getValue(403, j)/0.3048
                       for j in range(solver.lib.swmm_getCount(3))]
            kw_previous = details()['links']
            kw_time = 0.0
            kw_equation = dict(assumed_wx=kinwave_weight,assumed_wt=kinwave_weight,
                               weighted_equation_excess_ft3=0.0,
                               zero_outlet_excess_ft3=0.0,
                               other_steps_excess_ft3=0.0,
                               zero_outlet_positive_count=0)
        with (directory/'balance.csv').open('w', newline='') as stream:
            writer = csv.writer(stream)
            writer.writerow(['seconds', 'initial_m3', 'inflow_m3', 'outflow_losses_m3',
                             'storage_m3', 'residual_m3'])
            if probe:
                row = state(); writer.writerow(row)
                snapshots.append(dict(step=0, balance=row, **details()))
                previous = row[-1]
            while True:
                result = solver.step()
                steps += 1
                if probe:
                    row = state(); writer.writerow(row)
                    if kw_equation is not None:
                        current = details()['links']
                        dt = row[0]-kw_time
                        for length, old, new in zip(lengths, kw_previous, current):
                            # Caller must match WX=WT to the inspected build.
                            # This diagnostic is for loss-free, one-barrel fixtures.
                            w=kinwave_weight
                            excess = length*((1-w)*(new[3]-old[3])+w*(new[4]-old[4]))
                            excess -= dt*(w*(new[5]-new[6])+(1-w)*(old[5]-old[6]))
                            kw_equation['weighted_equation_excess_ft3'] += excess
                            if new[4] == 0 and excess > 0:
                                kw_equation['zero_outlet_excess_ft3'] += excess
                                kw_equation['zero_outlet_positive_count'] += 1
                            else:
                                kw_equation['other_steps_excess_ft3'] += excess
                        kw_previous, kw_time = current, row[0]
                    delta = row[-1]-previous
                    previous = row[-1]
                    if steps <= 10:
                        snapshots.append(dict(step=steps, balance=row, **details()))
                    if len(largest) < 20 or abs(delta) > largest[0][0]:
                        item = (abs(delta), steps, dict(step=steps, change_m3=delta,
                                                      balance=row, **details()))
                        heapq.heappush(largest, item)
                        if len(largest) > 20:
                            heapq.heappop(largest)
                if result['finished']:
                    break
            final = state() if probe else None
            if kw_equation is not None:
                kw_equation['final_storage_weight_bias_ft3'] = sum(
                    (kinwave_weight-.5)*length*(link[3]-link[4]) for length, link in zip(lengths, kw_previous))
        balance = solver.end()
        solver.report()
    finally:
        issues = solver.cleanup()
        if issues:
            raise RuntimeError(issues)
    record = dict(routing=routing, divisions=divisions, library=solver.metadata,
                  source_sha256=sha(source), input_sha256=sha(inp), output_sha256=sha(out),
                  report_sha256=sha(rpt), trace_sha256=sha(directory/'balance.csv'),
                  balance=balance, steps=steps, final=final, first_steps=snapshots,
                  largest_changes=[x[2] for x in sorted(largest, reverse=True)],
                  details_units='ft, ft2, ft3, cfs; group field order in native_dry_start_probe.c',
                  metadata=metadata, kinwave_equation=kw_equation)
    (directory/'result.json').write_text(json.dumps(record, indent=2)+'\n')
    return {k: record[k] for k in ('routing','divisions','balance','steps','final','output_sha256')}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--package', type=Path, required=True)
    p.add_argument('--library', type=Path, required=True)
    p.add_argument('--input', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--divisions', type=int, nargs='+', default=[1, 4, 16])
    p.add_argument('--kinwave-weight',type=float,default=.6,
                   help='Assumed WX=WT for diagnostics only; must match the inspected library')
    a = p.parse_args()
    sys.path.insert(0, str(a.package.resolve()))
    if any(n < 1 for n in a.divisions):
        p.error('divisions must be positive')
    if not .5<=a.kinwave_weight<=1.:p.error('kinwave-weight must be between .5 and 1')
    rows = []
    for routing in ('KINWAVE', 'DYNWAVE'):
        for n in a.divisions:
            row = run(a.library.resolve(), a.input.resolve(),
                      a.output.resolve()/f'{routing}-{n}', routing, n,a.kinwave_weight)
            rows.append(row)
            print(json.dumps(row), flush=True)
    (a.output/'summary.json').write_text(json.dumps(rows, indent=2)+'\n')


if __name__ == '__main__':
    main()
