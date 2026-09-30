"""Check existing ordinary Horton dry recovery against the documented capacity curve.

Independent two-stage capacity recovery and inverse-curve arithmetic, not a new
production solver. Records printed-manual inconsistencies without adopting them.
"""
from pathlib import Path
import argparse,ctypes,hashlib,importlib.util,itertools,json,math,os,platform,sys

ROOT=Path(__file__).resolve().parents[2]
HELPER=ROOT/'tools/qualify_horton_capacity.py'
spec=importlib.util.spec_from_file_location('capacity',HELPER)
helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(helper)
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()


def main(args):
    args.destination.mkdir(parents=True,exist_ok=False)
    suffix='dll' if args.family=='windows' else 'so'
    lib=helper.Trial(ROOT/f'build/horton-capacity-20260930/minimum-{args.family}/horton.{suffix}','minimum-cap')
    rows=[]
    for tp,dt,kd,kr in itertools.product((0.,1.,10.,40.),(1.,10.,60.),(.001,.01,.1),(.001,.01,.1)):
        f0,fmin=.002,.001
        # Eq. 4-3 cumulative curve. Recovery is computed through capacities
        # (4-6/4-9), followed by inverse 4-10, independently of native tp update.
        integral=lambda t:fmin*t+(f0-fmin)/kd*(-math.expm1(-kd*t))
        prior_capacity=fmin+(f0-fmin)*math.exp(-kd*tp)
        recovered_capacity=f0-(f0-prior_capacity)*math.exp(-kr*dt)
        expected_tp=math.log((f0-fmin)/(recovered_capacity-fmin))/kd
        expected_Fe=integral(expected_tp)
        native=lib.step([f0,fmin,kd,kr,1000.],[tp,integral(tp)],dt,0.,method=0)
        passed=native['flux']==0 and math.isclose(native['state'][0],expected_tp,rel_tol=1e-10,abs_tol=2e-12) and math.isclose(native['state'][1],expected_Fe,rel_tol=1e-10,abs_tol=2e-14)
        rows.append(dict(tp=tp,dt=dt,kd=kd,kr=kr,prior_capacity=prior_capacity,
                         recovered_capacity=recovered_capacity,expected=[expected_tp,expected_Fe],native=native,passed=passed))
    # Four time positions * three steps * three decay * three recovery = 108.
    assert len(rows)==108 and all(r['passed'] for r in rows)
    sample=next(r for r in rows if (r['tp'],r['dt'],r['kd'],r['kr'])==(10.,10.,.01,.01))
    literal_printed_tp=math.log(1-math.exp(-.01*10)*(1-math.exp(-.01*10)))/.01
    assert literal_printed_tp<0<sample['native']['state'][0]
    parameters=[.002,.001,.01,.01,1000.]
    tp=1600.;dt=10.
    integral=lambda t:.001*t+.001/.01*(-math.expm1(-.01*t))
    flat=lib.step(parameters,[tp,integral(tp)],dt,.003,method=0)
    exact_average=(integral(tp+dt)-integral(tp))/dt
    printed_flat_average=.002
    assert math.isclose(flat['flux'],.001,rel_tol=1e-12,abs_tol=1e-15) and abs(flat['flux']-exact_average)<2e-10
    assert printed_flat_average>1.9*exact_average
    result=dict(reference_arithmetic_checked=True,ordinary_repair_qualified=False,production_changed=False,
                python=sys.version,platform=platform.platform(),native_calls=109,rows=rows,
                printed_sign_example=dict(printed_tp=literal_printed_tp,native=sample['native'],
                                          independently_composed_tp=sample['expected'][0]),
                printed_flat_example=dict(native=flat,integral_average=exact_average,
                                          printed_flat_average=printed_flat_average),
                library_sha256=sha(lib.path),library=str(lib.path),tool_sha256=sha(Path(__file__)),
                helper_sha256=sha(HELPER),
                tolerance=dict(relative=1e-10,tp_absolute_seconds=2e-12,Fe_absolute_feet=2e-14,
                               scope='Independent float64 dry-recovery composition; not full project/release accuracy.'),
                scope='Positive nondegenerate constant-parameter ordinary Horton; virtual cumulative state and full monthly/cap semantics are not validated here.')
    (args.destination/'result.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(dict(native_calls=109,reference_arithmetic_checked=True,
                         printed_sign_example=result['printed_sign_example'],printed_flat_example=result['printed_flat_example'])))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--family',choices=('windows','linux'),required=True)
    p.add_argument('--destination',type=Path,required=True);args=p.parse_args()
    kernel=None
    if os.name=='nt':
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.GetErrorMode.restype=ctypes.c_uint;kernel.SetErrorMode.argtypes=[ctypes.c_uint]
        kernel.SetErrorMode.restype=ctypes.c_uint;previous=kernel.GetErrorMode();kernel.SetErrorMode(previous|1)
    try:main(args)
    finally:
        if kernel is not None:kernel.SetErrorMode(previous)
