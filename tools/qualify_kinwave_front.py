"""Compare a kinematic dry-start step with an independent shock reference.

The loss-free rectangular reach uses CFS/feet, a constant slope and Manning
roughness, no storage nodes or controls, and no initial water. For the rising
step, conservation gives front speed Q/A and arrival time L*A/Q. This reference
does not apply to DYNWAVE, backwater, surcharging, or the pulse/control cases.
The latter cases only exercise conservation and bounds.
"""
import argparse
import csv
import ctypes as c
import hashlib
import json
import math
from pathlib import Path
import sys


def reference(flow, length=1000., width=3., slope=.002, roughness=.013):
    def rating(depth):
        area=width*depth
        radius=area/(width+2*depth)
        return 1.486/roughness*area*radius**(2/3)*math.sqrt(slope)
    lo,hi=0.,3.
    if not 0 < flow < rating(hi):
        raise ValueError('Reference requires positive flow below full depth')
    for _ in range(100):
        mid=(lo+hi)/2
        if rating(mid)<flow:lo=mid
        else:hi=mid
    depth=(lo+hi)/2
    return dict(depth_ft=depth,area_ft2=width*depth,arrival_seconds=length*width*depth/flow,
                rating_residual_cfs=rating(depth)-flow)


def fixture(divisions,kind,flow,dt):
    options=f'''[OPTIONS]
FLOW_UNITS CFS
FLOW_ROUTING KINWAVE
START_DATE 01/01/2020
START_TIME 00:00:00
END_DATE 01/01/2020
END_TIME 02:00:00
REPORT_STEP 00:00:10
ROUTING_STEP {dt}
RULE_STEP 0
VARIABLE_STEP 0
[JUNCTIONS]
'''
    for i in range(divisions):
        options+=f'N{i} {2-2*i/divisions:.16g} 3 0 0 0\n'
    options+='[OUTFALLS]\nO 0 FREE NO\n[CONDUITS]\n'
    for i in range(divisions):
        end=f'N{i+1}' if i<divisions-1 else 'O'
        options+=f'P{i} N{i} {end} {1000/divisions:.16g} .013 0 0 {flow if kind=="wet" else 0} 0\n'
    options+='[XSECTIONS]\n'
    for i in range(divisions):options+=f'P{i} RECT_OPEN 3 3 0 0 1\n'
    if kind=='pulse':
        options+='[INFLOWS]\nN0 FLOW Pulse FLOW 1 1 0\n[TIMESERIES]\n'
        for time,value in [('00:00:00',flow),('00:19:59',flow),('00:20:00',0),
                           ('00:59:59',0),('01:00:00',flow),('01:19:59',flow),
                           ('01:20:00',0),('02:00:00',0)]:
            options+=f'Pulse {time} {value}\n'
    else:options+=f'[DWF]\nN0 FLOW {flow}\n'
    if kind=='control':
        options+='''[CONTROLS]
RULE Closure
IF SIMULATION TIME >= 00:20:00
AND SIMULATION TIME < 00:40:00
THEN CONDUIT P0 STATUS = CLOSED
ELSE CONDUIT P0 STATUS = OPEN
'''
    return options+'[REPORT]\nNODES ALL\nLINKS ALL\nCONTROLS YES\n'


def run(library,directory,divisions,kind,flow,dt,inspect_areas=False):
    from easysewer.runtime._native_solver import NativeSolver
    from easysewer.runtime._solver_worker import _configure_error_mode
    _configure_error_mode()
    directory.mkdir(parents=True,exist_ok=False)
    inp=directory/'model.inp';inp.write_text(fixture(divisions,kind,flow,dt))
    solver=NativeSolver(library)
    probe=getattr(solver.lib,'easysewer_probe',None)
    if probe:
        probe.argtypes=[c.c_int]*3;probe.restype=c.c_double
    if inspect_areas and probe is None:raise ValueError('Area inspection requires the diagnostic ABI')
    ref=reference(flow)
    arrivals={str(fraction):None for fraction in (.1,.5,.9)}
    minimum=float('inf');maximum=0.;l1=0.;previous=0.;steps=0;last=None
    areas=dict(minimum_ft2=float('inf'),maximum_ft2=0.,negative_count=0,above_full_count=0,
               maximum_absolute_system_residual_ft3=0.,maximum_balance_threshold_excess_ft3=0.) if inspect_areas else None
    try:
        solver.open((inp,directory/'model.rpt',directory/'model.out'))
        outlet=solver.lib.swmm_getIndex(2,b'O')
        solver.start(True)
        with (directory/'trace.csv').open('w',newline='') as stream:
            writer=csv.writer(stream)
            writer.writerow(['seconds','outflow_cfs','expected_step_cfs','initial_ft3','inflow_ft3','outflow_losses_ft3','storage_ft3','residual_ft3'])
            while True:
                result=solver.step();steps+=1
                # swmm_step resets ElapsedTime to zero on its terminal call.
                # This fixed-duration fixture ends at exactly 7200 seconds.
                time=7200. if result['finished'] else solver.lib.swmm_getValue(2,0)*86400
                # swmm_ELAPSEDTIME is days; the probe independently reads the ms routing clock.
                if probe:
                    time=probe(0,0,0)
                if not previous<time<=7200.:raise ValueError('Invalid routing clock')
                q=solver.lib.swmm_getValue(307,outlet)
                minimum=min(minimum,q);maximum=max(maximum,q)
                for fraction in arrivals:
                    if arrivals[fraction] is None and q>=float(fraction)*flow:arrivals[fraction]=time
                expected=flow if time>=ref['arrival_seconds'] else 0.
                # Integrate the ideal step exactly over each interval.
                if kind=='step':
                    before=max(0.,min(time,ref['arrival_seconds'])-previous)
                    after=(time-previous)-before
                    l1+=abs(q)*before+abs(q-flow)*after
                if probe:
                    initial,inflow,outflow,storage=[probe(0,0,i) for i in (1,2,3,4)]
                    last=[inflow,outflow,storage,initial+inflow-outflow-storage]
                    if areas is not None:
                        areas['maximum_absolute_system_residual_ft3']=max(
                            areas['maximum_absolute_system_residual_ft3'],abs(last[-1]))
                        areas['maximum_balance_threshold_excess_ft3']=max(
                            areas['maximum_balance_threshold_excess_ft3'],abs(last[-1])-max(1.,.01*(initial+inflow)))
                        for link in range(divisions):
                            for field in (3,4):
                                area=probe(2,link,field)
                                if not math.isfinite(area):raise ValueError('Nonfinite conduit area')
                                areas['minimum_ft2']=min(areas['minimum_ft2'],area)
                                areas['maximum_ft2']=max(areas['maximum_ft2'],area)
                                areas['negative_count']+=area < -1e-12
                                areas['above_full_count']+=area > 9.+1e-12
                writer.writerow([time,q,expected if kind=='step' else '',initial if probe else '',*(last or ['']*4)])
                previous=time
                if result['finished']:break
        balance=solver.end();solver.report()
    finally:
        issues=solver.cleanup()
        if issues:raise RuntimeError(issues)
    record=dict(kind=kind,divisions=divisions,flow_cfs=flow,step_seconds=dt,library=solver.metadata,
                reference=ref if kind=='step' else None,arrival_seconds=arrivals,
                minimum_outflow_cfs=minimum,maximum_outflow_cfs=maximum,
                normalized_l1=l1/(flow*ref['arrival_seconds']) if kind=='step' else None,
                balance=balance,final_volumes_ft3=last,steps=steps,
                internal_area_checks=areas,
                harness_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                artifact_sha256={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in directory.iterdir() if p.is_file()})
    (directory/'result.json').write_text(json.dumps(record,indent=2)+'\n')
    return {k:record[k] for k in ('kind','divisions','flow_cfs','balance','arrival_seconds','normalized_l1','maximum_outflow_cfs')}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--package',type=Path,required=True)
    p.add_argument('--library',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--divisions',nargs='+',type=int,default=[1,8,32,128])
    p.add_argument('--kinds',nargs='+',choices=['step','pulse','control','wet'],default=['step'])
    p.add_argument('--flow',type=float,default=.3)
    p.add_argument('--dt',type=float,default=.5)
    p.add_argument('--inspect-areas',action='store_true')
    a=p.parse_args()
    if any(n<1 for n in a.divisions) or not 0<a.dt<=10:p.error('Positive divisions and step <=10 seconds required')
    reference(a.flow)
    sys.path.insert(0,str(a.package.resolve()))
    rows=[]
    for kind in a.kinds:
        for n in a.divisions:
            row=run(a.library.resolve(),a.output.resolve()/f'{kind}-{n}',n,kind,a.flow,a.dt,a.inspect_areas)
            rows.append(row);print(json.dumps(row),flush=True)
    (a.output/'summary.json').write_text(json.dumps(rows,indent=2)+'\n')


if __name__=='__main__':main()
