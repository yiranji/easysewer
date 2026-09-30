"""Build unadopted KINWAVE boundary and numerical-weight experiments.

This is not a standard build profile. lower-area changes the -3 boundary
response; bounded-flow adds an outflow maximum; positive-bounds also constrains
the reconstructed inlet area. These modes retain WX=WT=.6. centered-bounds
uses the same boundary changes with WX=WT=.5, changing the numerical weighting.
capacity-bounds also applies the full-flow admission limit before the equation
and preserves stored water when the outlet area reaches its upper bound.
capacity-tight additionally tightens the existing root stopping tolerance.
capacity-empty also restricts the no-flow shortcut to an exactly empty state,
so small inflows cannot create a normal inlet area without entering the equation.
The upstream scalar equation and root routine remain. None of these candidates
has passed full hydraulic qualification. See docs/2.0-kinwave-limiter-experiment.md.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source',type=Path,required=True)
    p.add_argument('--destination',type=Path,required=True)
    p.add_argument('--compiler',required=True)
    p.add_argument('--target',choices=['windows','linux'],required=True)
    p.add_argument('--mode',choices=['lower-area','bounded-flow','positive-bounds','centered-bounds','capacity-bounds','capacity-tight','capacity-empty'],default='lower-area')
    a=p.parse_args()
    root=Path(__file__).resolve().parents[2]
    base=json.loads((root/'native/standard/source.json').read_text())
    sha=lambda path:hashlib.sha256(path.read_bytes()).hexdigest()
    batch=a.destination.resolve();batch.mkdir(exist_ok=False,parents=True)
    source=batch/'source'
    for name,digest in base['files'].items():
        raw=(a.source/name).read_bytes().replace(b'\r\n',b'\n')
        if hashlib.sha256(raw).hexdigest()!=digest:raise ValueError('Upstream source mismatch: '+name)
        target=source/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(raw)
    path=source/'src/solver/kinwave.c';text=path.read_text()
    anchor='        result = solveContinuity(qin, ain, &aout);'
    if text.count(anchor)!=1:raise ValueError('Boundary patch anchor changed')
    replacement=anchor+'''

        /* EXPERIMENT ONLY: retain the original weighted continuity equation
           when its downstream-area solution would be negative. Do not assign
           a normal inlet area for water that has not entered the link yet. */
        if (result == -3)
        {
            ain -= C2 * WX / ((1.0 - WT) * dxdt);
            if (ain < 0.0) ain = 0.0;
        }
'''
    text=text.replace(anchor,replacement)
    if a.mode in ('capacity-bounds','capacity-tight','capacity-empty'):
        anchor='    qin = (*qinflow) / Conduit[k].barrels / Qfull;'
        if text.count(anchor)!=1:raise ValueError('Admission patch anchor changed')
        text=text.replace(anchor,anchor+'''
    /* EXPERIMENT ONLY: use the same admitted inflow in both the continuity
       equation and the returned node outflow. Excess remains at the node. */
    if (qin > 1.0) qin = 1.0;
''')
        anchor='        // --- report error if continuity eqn. not solved'
        if text.count(anchor)!=1:raise ValueError('Upper boundary patch anchor changed')
        text=text.replace(anchor,'''        /* EXPERIMENT ONLY: the full outlet area cannot absorb an arbitrary
           loss of inlet area during shutoff. Retain the inlet area required
           by the same discrete continuity equation at the returned outlet. */
        if (result == -2)
        {
            double discharge = Beta1*xsect_getSofA(pXsect, aout*Afull);
            ain = a1 + (WX*(qin-discharge) - (1.0-WX)*dq - q3
                - dxdt*WT*(aout-a2)) / (dxdt*(1.0-WT));
        }

'''+anchor)
    if a.mode in ('bounded-flow','positive-bounds','centered-bounds','capacity-bounds','capacity-tight','capacity-empty'):
        anchor='        qout = Beta1 * xsect_getSofA(pXsect, aout*Afull);'
        if text.count(anchor)!=1:raise ValueError('Outflow patch anchor changed')
        text=text.replace(anchor,anchor+'''
        /* EXPERIMENT ONLY: enforce the local scalar-flow maximum and retain
           water through the original weighted continuity equation. */
        {
            double qmax = MAX(qin, MAX(q1, q2));
            if (qout > qmax)
            {
                qout = qmax;
                aout = xsect_getAofS(pXsect, qout/Beta1) / Afull;
                ain = a1 + (WX*(qin-qout) - (1.0-WX)*dq - q3
                    - dxdt*WT*(aout-a2)) / (dxdt*(1.0-WT));
            }
        }
''')
    if a.mode in ('positive-bounds','centered-bounds','capacity-bounds','capacity-tight','capacity-empty'):
        anchor='        if ( qin > 1.0 ) qin = 1.0;'
        if text.count(anchor)!=1:raise ValueError('Projection patch anchor changed')
        text=text.replace(anchor,'''        /* EXPERIMENT ONLY: project a negative reconstructed inlet area
           onto ain=0 by solving the same scalar continuity equation, while
           decreasing outlet area/flow. This retains the local flow bound. */
        if (ain < 0.0)
        {
            int iteration;
            double budget = (1.0-WT)*a1 + WT*a2
                + (WX*qin - (1.0-WX)*dq - q3)/dxdt;
            double lo = 0.0, hi = aout;
            if (budget < 0.0)
            {
                report_writeErrorMsg(ERR_KINWAVE, Link[j].ID);
                return 1;
            }
            for (iteration=0; iteration<64; iteration++)
            {
                double mid = 0.5*(lo+hi);
                double discharge = Beta1*xsect_getSofA(pXsect, mid*Afull);
                if (WT*mid + WX/dxdt*discharge > budget) hi=mid;
                else lo=mid;
            }
            aout=lo;
            qout=Beta1*xsect_getSofA(pXsect, aout*Afull);
            ain=(budget-WT*aout-WX/dxdt*qout)/(1.0-WT);
        }
'''+anchor)
    if a.mode in ('centered-bounds','capacity-bounds','capacity-tight','capacity-empty'):
        for name in ('WX','WT'):
            anchor=f'static const double {name}      = 0.6;'
            if text.count(anchor)!=1:raise ValueError('Weight patch anchor changed')
            text=text.replace(anchor,anchor.replace('0.6','0.5'))
    if a.mode in ('capacity-tight','capacity-empty'):
        anchor='static const double EPSIL   = 0.001;'
        if text.count(anchor)!=1:raise ValueError('Tolerance patch anchor changed')
        text=text.replace(anchor,anchor.replace('0.001','1.0e-10'))
    if a.mode=='capacity-empty':
        anchor='    if ( qin <= TINY && q2 <= TINY )'
        if text.count(anchor)!=1:raise ValueError('Empty-state patch anchor changed')
        text=text.replace(anchor,'''    /* EXPERIMENT ONLY: retain the continuity equation for a small positive
       inflow or any retained water/previous flux, regardless of TINY. */
    if (qin == 0.0 && q1 == 0.0 && q2 == 0.0 && a1 == 0.0 && a2 == 0.0)''')
    path.write_text(text,newline='\n')
    solver=source/'src/solver';probe=root/'tools/native_dry_start_probe.c'
    output=batch/('boundary-candidate.dll' if a.target=='windows' else 'boundary-candidate.so')
    flags=['-shared','-O2','-fno-fast-math','-ffp-contract=off','-fopenmp','-Wall','-Wextra',
           '-Werror=implicit-function-declaration']
    flags+=['-static','-static-libgcc','-Wl,--no-insert-timestamp','-Wl,--image-base,0x180000000'] if a.target=='windows' else ['-fPIC']
    command=[a.compiler,*flags,'-I'+str(solver),'-I'+str(solver/'include'),
             *[str(source/name) for name in sorted(base['files']) if name.endswith('.c')],
             str(probe),'-o',str(output),'-lm']
    done=subprocess.run(command,capture_output=True,text=True)
    output.with_suffix(output.suffix+'.log').write_text(done.stdout+done.stderr)
    done.check_returncode()
    record=dict(scope='Experiment only; no adoption',adopted=False,mode=a.mode,base=base,command=command,
                source_digests={n:sha(source/n) for n in base['files']},changed_native_files=['src/solver/kinwave.c'],
                binary_sha256=sha(output),probe_sha256=sha(probe),harness_sha256=sha(Path(__file__)),
                compiler=subprocess.check_output([a.compiler,'--version'],text=True))
    output.with_suffix(output.suffix+'.json').write_text(json.dumps(record,indent=2)+'\n')
    print(json.dumps(dict(library=str(output),sha256=sha(output))))


if __name__=='__main__':main()
