"""Actual OpenMP thread selection under controlled worker environment."""
from dataclasses import replace
import hashlib,json,os,re,subprocess,sys,tempfile,unittest
from pathlib import Path
from easysewer import get_native_capabilities
from easysewer.model import Model,Ref
from test_option_hydraulics_v2 import hydraulic_fixture
from test_native_v2_option_effects import observe,digest
from test_native_v2_regulator_fields import FAMILIES,library

EVIDENCE=[]


def thread_probe():
    test=unittest.TestCase();rows=[]
    test.assertEqual(os.environ['OMP_NUM_THREADS'],'2')
    with tempfile.TemporaryDirectory() as directory:
        root=Path(directory)
        for family,name,symbol in FAMILIES:
            lib,_=library(name,symbol)
            for branches in (1,8):
                base=hydraulic_fixture()
                for i in range(1,branches):
                    key='K'+str(i);ref=Ref(collection='swmm:nodes',key=key)
                    outfall='O'+str(i);outref=Ref(collection='swmm:nodes',key=outfall)
                    base.nodes.add(replace(base.nodes['K'],id=key))
                    base.nodes.add(replace(base.nodes['O'],id=outfall))
                    base.links.add(replace(base.links['P'],id='P'+str(i),outlet=ref))
                    base.links.add(replace(base.links['Q'],id='Q'+str(i),inlet=ref,outlet=outref))
                results=[]
                for requested in (1,0,2,2147483647):
                    expected=1 if branches==1 or requested==1 else 2
                    literal=base.to_document().text+f'[OPTIONS]\nTHREADS {requested}\n'
                    result=observe(test,lib,root,literal)
                    match=re.search(rb'Number of Threads\s+\.+\s+(\d+)',result['report'])
                    test.assertIsNotNone(match)
                    test.assertEqual(int(match[1]),expected)
                    model=base.copy();model.update_options(threads=requested)
                    restored=Model.from_json_document(model.to_json_document(),strict=True)
                    test.assertEqual(restored.options.threads,requested)
                    test.assertEqual(observe(test,lib,root,restored.to_document(normalize=True).text),result)
                    report=re.sub(rb'(Number of Threads\s+\.+\s+)\d+',rb'\g<1><threads>',result['report'])
                    results.append((result['out'],report))
                    rows.append(dict(kind='actual-thread-selection',family=family,links=2*branches,
                        requested=requested,actual=expected,result=digest(result)))
                # Only the displayed count changes for this deterministic network.
                test.assertTrue(all(r==results[0] for r in results))
    return rows


@unittest.skipUnless(get_native_capabilities()['swmm_solver'] and get_native_capabilities()['flexible_ponding'],
                     'Both packaged engines required')
class NativeOptionThreadTests(unittest.TestCase):
    def test_small_network_fallback_and_large_network_thread_clamp(self):
        # OpenMP reads its environment at initialization. A fresh process makes
        # this independent of libraries already loaded by the test runner.
        env=dict(os.environ,OMP_NUM_THREADS='2',OMP_THREAD_LIMIT='2',OMP_DYNAMIC='FALSE')
        code=('import sys,json;sys.path[:0]='+repr(sys.path)+';'
              'from test_native_v2_option_threads import thread_probe;print(json.dumps(thread_probe()))')
        run=subprocess.run([sys.executable,'-B','-c',code],env=env,stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,text=True,encoding='utf-8',timeout=120)
        self.assertEqual(run.returncode,0,run.stderr)
        rows=json.loads(run.stdout);self.assertEqual(len(rows),16)
        EVIDENCE.extend(rows)


if __name__=='__main__':unittest.main()
