"""Assess the rectangular-reach benchmark, including known failing cases.

Criteria are specific to this benchmark: final continuity <=1%, outflow
nonnegative and <=101% of imposed flow; dry-step normalized L1 and median-front
timing error <=5%. Diagnostic traces also require areas in [0,9] ft2 (1e-12
tolerance) and instantaneous volume residual <=max(1 ft3,1% of initial storage
plus accumulated inflow). They are not a general SWMM accuracy guarantee.
--require-pass makes a failing numerical assessment return a nonzero exit code.
"""
import argparse
import csv
import hashlib
import json
import math
from pathlib import Path

from qualify_kinwave_front import reference


def assess(directory):
    data=json.loads((directory/'result.json').read_text())
    for name,digest in data['artifact_sha256'].items():
        if hashlib.sha256((directory/name).read_bytes()).hexdigest()!=digest:
            raise ValueError('Artifact changed: '+str(directory/name))
    flow=data['flow_cfs'];ref=reference(flow)
    with (directory/'trace.csv').open() as stream:
        rows=list(csv.DictReader(stream))
    times=[float(row['seconds']) for row in rows]
    values=[float(row['outflow_cfs']) for row in rows]
    if not rows or times[-1]!=7200 or any(b<=a for a,b in zip([0.,*times[:-1]],times)):
        raise ValueError('Incomplete or non-monotone routing clock')
    if not all(math.isfinite(v) for v in values):raise ValueError('Nonfinite outflow')
    maximum=max(values);minimum=min(values)
    if maximum!=data['maximum_outflow_cfs'] or minimum!=data['minimum_outflow_cfs']:
        raise ValueError('Summary does not match native trace')
    median=next((t for t,q in zip(times,values) if q>=.5*flow),None)
    l1=None;timing=None
    if data['kind']=='step':
        integral=0.;previous=0.
        for time,q in zip(times,values):
            before=max(0.,min(time,ref['arrival_seconds'])-previous)
            integral+=abs(q)*before+abs(q-flow)*(time-previous-before)
            previous=time
        l1=integral/(flow*ref['arrival_seconds'])
        if not math.isclose(l1,data['normalized_l1'],rel_tol=1e-12,abs_tol=1e-12):
            raise ValueError('L1 summary differs from trace')
        timing=abs(median-ref['arrival_seconds'])/ref['arrival_seconds'] if median is not None else None
    criteria=dict(continuity=abs(data['balance']['flow_percent'])<=1,
                  nonnegative_outflow=minimum>=-flow*1e-12,
                  peak_bound=maximum<=flow*1.01)
    area=data.get('internal_area_checks')
    if area is not None:
        criteria.update(nonnegative_area=area['negative_count']==0 and area['minimum_ft2']>=-1e-12,
                        area_below_full=area['above_full_count']==0 and area['maximum_ft2']<=9.+1e-12)
    maximum_balance_excess=None
    if rows[0].get('residual_ft3') not in (None,''):
        inferred=(float(rows[0]['residual_ft3'])+float(rows[0]['outflow_losses_ft3'])+
                  float(rows[0]['storage_ft3'])-float(rows[0]['inflow_ft3']))
        explicit_initial=rows[0].get('initial_ft3') not in (None,'')
        initial=float(rows[0]['initial_ft3']) if explicit_initial else inferred
        if not math.isfinite(initial):raise ValueError('Nonfinite initial storage')
        maximum_balance_excess=0.
        for row in rows:
            inflow,outflow,storage,residual=[float(row[k]) for k in
                                           ('inflow_ft3','outflow_losses_ft3','storage_ft3','residual_ft3')]
            if not all(math.isfinite(v) for v in (inflow,outflow,storage,residual)):
                raise ValueError('Nonfinite volume accounting')
            if explicit_initial and float(row['initial_ft3'])!=initial:
                raise ValueError('Initial storage changed during routing')
            if not math.isclose(initial+inflow-outflow-storage,residual,rel_tol=0.,abs_tol=1e-7):
                raise ValueError('Inconsistent volume accounting')
            maximum_balance_excess=max(maximum_balance_excess,abs(residual)-max(1.,.01*(initial+inflow)))
        criteria['instantaneous_balance']=maximum_balance_excess<=1e-7
    if data['kind']=='step':
        criteria.update(step_shape=l1<=.05,step_timing=timing is not None and timing<=.05)
    if data['kind']=='wet':
        criteria['steady_flow']=max(abs(v-flow) for v in values)<=flow*.01
    return dict(kind=data['kind'],divisions=data['divisions'],flow_cfs=flow,
                library_sha256=data['library']['sha256'],criteria=criteria,passed=all(criteria.values()),
                flow_continuity_percent=data['balance']['flow_percent'],
                normalized_l1=l1,median_arrival_seconds=median,relative_timing_error=timing,
                peak_ratio=maximum/flow,final_residual_ft3=data['final_volumes_ft3'][-1] if data['final_volumes_ft3'] else None,
                maximum_balance_threshold_excess_ft3=maximum_balance_excess,
                result_sha256=hashlib.sha256((directory/'result.json').read_bytes()).hexdigest())


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('directory',type=Path)
    p.add_argument('--require-pass',action='store_true')
    a=p.parse_args()
    cases=sorted(a.directory.glob('*/result.json'))
    if not cases:p.error('No result.json files found')
    results=[assess(path.parent) for path in cases]
    record=dict(scope='Rectangular-reach benchmark only',passed=all(r['passed'] for r in results),
                thresholds=dict(continuity_percent=1,peak_ratio=1.01,normalized_l1=.05,relative_timing_error=.05,
                                area_tolerance_ft2=1e-12,full_area_ft2=9.,
                                instantaneous_balance_absolute_ft3=1.,instantaneous_balance_fraction=.01),
                results=results)
    (a.directory/'assessment.json').write_text(json.dumps(record,indent=2)+'\n')
    print(json.dumps(dict(passed=record['passed'],cases=len(results),
                         failures=[dict(kind=r['kind'],divisions=r['divisions'],criteria=r['criteria'])
                                   for r in results if not r['passed']])))
    if a.require_pass and not record['passed']:raise SystemExit(1)


if __name__=='__main__':main()
