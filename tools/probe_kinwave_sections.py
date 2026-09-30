"""Diagnostic section/loss stress cases; not independent hydraulic validation.

Uses the rectangular benchmark's network and forcing but varies the conduit
section, capacity and seepage. Requires the read-only native diagnostic ABI.
No shock-reference comparison is valid for this collection. Raw volumes and
areas are retained even when the solver reports near-zero final continuity.
"""
import argparse
import csv
import ctypes as c
import hashlib
import json
import math
from pathlib import Path
import sys

from qualify_kinwave_front import fixture


SECTIONS = {
    # Match pinned EPA consts.h PI; math.pi would falsely flag full circular
    # areas by about 9e-10 ft2 against the 1e-12 area tolerance.
    'circular': ('CIRCULAR 3 0 0 0 1', 3.141592654*9/4),
    'closed': ('RECT_CLOSED 3 3 0 0 1', 9.),
    'triangle': ('TRIANGULAR 3 6 0 0 1', 9.),
    'trapezoid': ('TRAPEZOIDAL 3 1 1 1 1', 12.),
    'custom': ('CUSTOM 3 Box 0 0 1', 9.),
    'seepage': ('RECT_OPEN 3 3 0 0 1', 9.),
}


def run(library, directory, section, flow, divisions, seepage=0.):
    from easysewer.runtime._native_solver import NativeSolver
    from easysewer.runtime._solver_worker import _configure_error_mode
    _configure_error_mode()
    directory.mkdir(parents=True, exist_ok=False)
    shape, full_area = SECTIONS[section]
    text=fixture(divisions, 'pulse', flow, .5).replace('RECT_OPEN 3 3 0 0 1',shape)
    if section=='custom':text+='[CURVES]\nBox SHAPE 0 1\nBox 1 1\n'
    if seepage:
        text+='[LOSSES]\n'+''.join(f'P{i} 0 0 0 NO {seepage}\n' for i in range(divisions))
    inp=directory/'model.inp';inp.write_text(text)
    solver=NativeSolver(library)
    probe=solver.lib.easysewer_probe;probe.argtypes=[c.c_int]*3;probe.restype=c.c_double
    minimum=float('inf');maximum=0.;peak=0.;negative=0;above_full=0
    max_residual=0.;max_excess=0.;final=None;balance=None;error=None;steps=0;previous=0.
    try:
        solver.open((inp,directory/'model.rpt',directory/'model.out'))
        outlet=solver.lib.swmm_getIndex(2,b'O');solver.start(True)
        with (directory/'trace.csv').open('w',newline='') as stream:
            writer=csv.writer(stream)
            writer.writerow(['seconds','outflow_cfs','initial_ft3','inflow_ft3','outflow_losses_ft3',
                             'storage_ft3','residual_ft3','minimum_area_ft2','maximum_area_ft2'])
            while True:
                step=solver.step();steps+=1
                clock,initial,inflow,outflow,storage=[probe(0,0,i) for i in range(5)]
                if not previous<clock<=7200.:raise ValueError('Invalid routing clock')
                previous=clock
                q=solver.lib.swmm_getValue(307,outlet)
                areas=[probe(2,j,k) for j in range(divisions) for k in (3,4)]
                if not all(math.isfinite(v) for v in [q,initial,inflow,outflow,storage,*areas]):
                    raise ValueError('Nonfinite diagnostic state')
                minimum=min(minimum,*areas);maximum=max(maximum,*areas)
                negative+=sum(v < -1e-12 for v in areas)
                above_full+=sum(v>full_area+1e-12 for v in areas)
                residual=initial+inflow-outflow-storage
                max_residual=max(max_residual,abs(residual))
                max_excess=max(max_excess,abs(residual)-max(1.,.01*(initial+inflow)))
                peak=max(peak,q)
                final=[clock,q,initial,inflow,outflow,storage,residual,min(areas),max(areas)]
                writer.writerow(final)
                if step['finished']:break
        balance=solver.end();solver.report()
    except Exception as exc:
        error=f'{type(exc).__name__}: {exc}'
    finally:
        cleanup=solver.cleanup()
        if cleanup:error=f'{error or ""}; cleanup: {cleanup}'
    checks=dict(completed=error is None and previous==7200.,nonnegative_area=negative==0,
                area_below_full=above_full==0,peak_bound=peak<=1.01*flow,
                instantaneous_balance=max_excess<=1e-7,
                final_balance=balance is not None and abs(balance['flow_percent'])<=1.)
    record=dict(section=section,flow_cfs=flow,divisions=divisions,seepage_inches_hour=seepage,
                scope='Conservation and bounds only; no independent hydraulic-accuracy claim',
                library=solver.metadata,error=error,steps=steps,balance=balance,final=final,
                full_area_ft2=full_area,minimum_area_ft2=minimum if steps else None,
                maximum_area_ft2=maximum,negative_count=negative,above_full_count=above_full,
                peak_cfs=peak,maximum_absolute_residual_ft3=max_residual,
                maximum_balance_threshold_excess_ft3=max_excess,checks=checks,passed=all(checks.values()),
                harness_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                fixture_harness_sha256=hashlib.sha256(Path(__file__).with_name('qualify_kinwave_front.py').read_bytes()).hexdigest(),
                artifacts={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in directory.iterdir() if p.is_file()})
    (directory/'result.json').write_text(json.dumps(record,indent=2)+'\n')
    return record


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--package',type=Path,required=True)
    p.add_argument('--library',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--sections',choices=list(SECTIONS),nargs='+',default=list(SECTIONS))
    p.add_argument('--flows',type=float,nargs='+')
    p.add_argument('--divisions',type=int,nargs='+',default=[1,32])
    a=p.parse_args();sys.path.insert(0,str(a.package.resolve()))
    if any(n<1 for n in a.divisions):p.error('divisions must be positive')
    if a.flows and any(not math.isfinite(q) or q<=0 for q in a.flows):p.error('flows must be finite and positive')
    records=[]
    for section in a.sections:
        # 60 CFS additionally exercises capacity exceedance for closed sections.
        for flow in a.flows or ((.3,30.,60.) if section in ('circular','closed') else (.3,30.)):
            for n in a.divisions:
                name=f'{section}-{flow:g}-{n}'
                row=run(a.library.resolve(),a.output.resolve()/name,section,flow,n,
                        seepage=24. if section=='seepage' else 0.)
                records.append(dict(case=name,**row))
                print(json.dumps(dict(case=name,passed=row['passed'],error=row['error'],checks=row['checks'])),flush=True)
    (a.output/'summary.json').write_text(json.dumps(records,indent=2)+'\n')
    if not all(r['passed'] for r in records):raise SystemExit(1)


if __name__=='__main__':main()
