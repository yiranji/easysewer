"""Independent circular geometry and Manning rating versus native lookup tables.

This checks the static section law, not the accuracy of a transient solution.
No SWMM shape table or shape function is used to construct the analytic curve.
Trace comparison requires an explicitly supplied full-normal-flow scale.
"""
import argparse
import csv
import ctypes
import hashlib
import json
import math
from pathlib import Path
import sys


def _segment(theta):
    if theta<.01:
        t2=theta*theta
        return theta**3/6*(1-t2/20+t2*t2/840-t2**3/60480)
    return theta-math.sin(theta)


def circular_state(fraction,diameter=3.,slope=.002,roughness=.013):
    if not math.isfinite(fraction) or not 0<=fraction<=1:
        raise ValueError('Area fraction must be finite and in [0,1]')
    if any(not math.isfinite(x) or x<=0 for x in (diameter,slope,roughness)):
        raise ValueError('Geometry and Manning parameters must be positive and finite')
    radius=diameter/2
    target=2*math.pi*fraction
    lo,hi=0.,2*math.pi
    if fraction in (0.,1.):theta=target
    else:
        for _ in range(100):
            theta=(lo+hi)/2
            if _segment(theta)<target:lo=theta
            else:hi=theta
        theta=(lo+hi)/2
    area=math.pi*radius*radius*fraction
    perimeter=radius*theta
    hydraulic_radius=area/perimeter if perimeter else 0.
    section=area*hydraulic_radius**(2/3)
    return dict(area_fraction=fraction,theta_radians=theta,area_ft2=area,
                depth_ft=radius*(1-math.cos(theta/2)),wetted_perimeter_ft=perimeter,
                hydraulic_radius_ft=hydraulic_radius,section_factor=section,
                flow_cfs=1.486/roughness*math.sqrt(slope)*section)


def maximum_rating(diameter=3.,slope=.002,roughness=.013):
    # d log(Q)/d theta = 0 gives 5 theta(1-cos theta)=2(theta-sin theta).
    # The nonzero maximum lies between a half-full and a full circle.
    lo,hi=math.pi,2*math.pi
    for _ in range(100):
        theta=(lo+hi)/2
        derivative=5*theta*(1-math.cos(theta))-2*_segment(theta)
        if derivative>0:lo=theta
        else:hi=theta
    theta=(lo+hi)/2
    result=circular_state(_segment(theta)/(2*math.pi),diameter,slope,roughness)
    result['flow_ratio_to_full']=result['flow_cfs']/circular_state(1.,diameter,slope,roughness)['flow_cfs']
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--package',type=Path,required=True)
    p.add_argument('--library',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--trace',type=Path)
    p.add_argument('--trace-full-flow',type=float)
    a=p.parse_args()
    if (a.trace is None)!=(a.trace_full_flow is None):p.error('trace and trace-full-flow must be supplied together')
    if a.trace_full_flow is not None and (not math.isfinite(a.trace_full_flow) or a.trace_full_flow<=0):
        p.error('trace-full-flow must be positive and finite')
    sys.path.insert(0,str(a.package.resolve()))
    from easysewer.runtime._solver_worker import _configure_error_mode
    _configure_error_mode()
    library=ctypes.CDLL(str(a.library.resolve()))
    probe=library.easysewer_probe_circle
    probe.argtypes=[ctypes.c_int,ctypes.c_double];probe.restype=ctypes.c_double
    full_area=probe(0,1.);full_section=probe(2,1.)
    if full_area<=0 or full_section<=0:raise ValueError('Invalid diagnostic ABI')
    a.output.mkdir(parents=True,exist_ok=False)
    exact_full=circular_state(1.)
    rows=[]
    for i in range(2001):
        fraction=i/2000
        exact=circular_state(fraction)['section_factor']/exact_full['section_factor']
        native=probe(1,fraction)/full_section
        if not math.isfinite(native):raise ValueError('Nonfinite native section factor')
        rows.append(dict(area_fraction=fraction,analytic_flow_ratio=exact,native_flow_ratio=native,
                         difference_as_fraction_of_full=native-exact))
    with (a.output/'rating.csv').open('w',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    comparison=None
    if a.trace:
        with a.trace.open() as stream:trace=list(csv.DictReader(stream))
        if not trace:raise ValueError('Empty trace')
        compared=[]
        for row in trace:
            area=float(row['outlet_area_ft2']);flow=float(row['outlet_cfs'])
            if not math.isfinite(area) or not math.isfinite(flow) or not 0<=area<=full_area+1e-12:
                raise ValueError('Invalid outlet state')
            fraction=min(area/full_area,1.)
            analytic=circular_state(fraction)['section_factor']/exact_full['section_factor']*a.trace_full_flow
            native=probe(1,fraction)/full_section*a.trace_full_flow
            compared.append(dict(seconds=float(row['seconds']),area_fraction=fraction,outflow_cfs=flow,
                                 native_rating_cfs=native,analytic_rating_cfs=analytic,
                                 native_closure_error_cfs=flow-native,analytic_closure_error_cfs=flow-analytic))
        peak=max(compared,key=lambda r:r['outflow_cfs'])
        comparison=dict(trace_sha256=hashlib.sha256(a.trace.read_bytes()).hexdigest(),
                        supplied_full_flow_cfs=a.trace_full_flow,peak=peak,
                        analytic_maximum_flow_cfs=maximum_rating()['flow_ratio_to_full']*a.trace_full_flow,
                        maximum_absolute_native_closure_error_cfs=max(abs(r['native_closure_error_cfs']) for r in compared),
                        maximum_absolute_analytic_closure_error_cfs=max(abs(r['analytic_closure_error_cfs']) for r in compared))
        with (a.output/'trace-rating.csv').open('w',newline='') as stream:
            writer=csv.DictWriter(stream,fieldnames=list(compared[0]));writer.writeheader();writer.writerows(compared)
    record=dict(scope='Static circular section law only; transient and release qualification remain open',
                library_sha256=hashlib.sha256(a.library.read_bytes()).hexdigest(),
                analytic_maximum=maximum_rating(),analytic_full=exact_full,
                native_full_area_ft2=full_area,native_declared_max_ratio=probe(3,1.)/full_section,
                native_declared_max_area_fraction=probe(4,1.)/full_area,
                sampled_native_maximum=max(rows,key=lambda r:r['native_flow_ratio']),
                largest_sampled_difference=max(rows,key=lambda r:abs(r['difference_as_fraction_of_full'])),
                trace_comparison=comparison,release_qualified=False,
                artifacts={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in a.output.iterdir()},
                harness_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    (a.output/'result.json').write_text(json.dumps(record,indent=2)+'\n')
    print(json.dumps(record))


if __name__=='__main__':main()
