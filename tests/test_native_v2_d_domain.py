"""D actual combined groundwater/treatment outputs and fresh-parent continuation."""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from easysewer import get_native_capabilities
from easysewer.io.output import OutputReader
from easysewer.model import Model
from easysewer.runtime import RunResult
from easysewer.utils import probe_library_path
from d_domain_fixture import ORACLE,model,imported,edited,edited_oracle,state,ref
from test_native_v2_b_domain import observe,FAMILIES
from test_native_v2_standard_io import direct_library
from test_scenario_v2 import portable

EVIDENCE=[]


def converted():
    value=model();value.convert_pollutant_units('A','UG/L');return value


CONVERTED_ORACLE=ORACLE.replace('A MG/L 0 20','A UG/L 0 20000').replace('J A "" CONCEN 1 1 10',
    'J A "" CONCEN 1 1 10000').replace('C=A * EXP(-.1 * HRT)','C=1000 * ((A / 1000) * EXP(-.1 * HRT))')


def histories(path,*,edited=False):
    with OutputReader(path) as reader:
        return dict(flow=reader.series(ref('links','P'),'swmm:flow').values,
            groundwater={k:reader.series(ref('subcatchments',k),'swmm:groundwater_flow').values
                for k in (('Basin','S2') if edited else ('S','S2'))},
            water_table=reader.series(ref('subcatchments','S2'),'swmm:groundwater_elevation').values,
            quality={k:reader.series(ref('links','P'),'swmm:concentration',pollutant=ref('pollutants',k)).values
                for k in (('Solids','Tracer') if edited else ('A','B'))})


@unittest.skipUnless(get_native_capabilities()['swmm_solver'] and get_native_capabilities()['flexible_ponding'],
                    'Standard/custom native solvers unavailable')
class NativeDDomainTests(unittest.TestCase):
    def test_both_engines_match_independent_creation_edit_and_pollutant_unit_oracles(self):
        for family,name,symbol in FAMILIES:
            lib,library=direct_library(probe_library_path(name),revision_symbol=symbol)
            hashes={}
            for kind,oracle,value in (('created',ORACLE,model()),('edited-created',edited_oracle(),edited(model())),
                ('edited-imported',edited_oracle(),edited()),('pollutant-units',CONVERTED_ORACLE,converted())):
                with self.subTest(family=family,kind=kind),tempfile.TemporaryDirectory() as directory:
                    root=Path(directory)
                    sources=(oracle,value.to_document().text,Model.from_document(value.to_document(),strict=True).to_document().text,
                        portable(value).to_document().text)
                    outputs=[observe(lib,root/str(i),source) for i,source in enumerate(sources)]
                    for actual in outputs[1:]:self.assertEqual(actual,outputs[0])
                    data=histories(root/'0/model.out',edited=kind.startswith('edited'))
                    self.assertGreater(max(data['flow']),0)
                    self.assertTrue(all(max(values)>0 for values in data['groundwater'].values()))
                    self.assertTrue(all(max(values)>0 for values in data['quality'].values()))
                    hashes[kind]=hashlib.sha256(outputs[0][0]).hexdigest()
                    EVIDENCE.append(dict(family=family,kind=kind,comparisons=3,out_sha256=hashes[kind],
                        report_sha256=hashlib.sha256(outputs[0][1]).hexdigest(),library_sha256=hashlib.sha256(library.read_bytes()).hexdigest(),
                        groundwater_peaks={k:max(v) for k,v in data['groundwater'].items()},
                        concentration_peaks={k:max(v) for k,v in data['quality'].items()}))
            self.assertEqual(hashes['edited-created'],hashes['edited-imported'])
            self.assertEqual(len(set(hashes.values())),3)

    def test_both_groundwater_expressions_and_dependent_treatments_change_real_histories(self):
        for family,name,symbol in FAMILIES:
            lib,_=direct_library(probe_library_path(name),revision_symbol=symbol)
            with self.subTest(family=family),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);results={}
                for mode in ('all','no-treatment','no-lateral','no-deep'):
                    value=model();oracle=ORACLE
                    if mode=='no-treatment':
                        for key in tuple(value.treatment):value.treatment.remove(key)
                        oracle=oracle.replace('J A C=A * EXP(-.1 * HRT)\n','').replace('J B R=.2 * R_A + 0 * B\n','')
                    elif mode in ('no-lateral','no-deep'):
                        kind='LATERAL' if mode=='no-lateral' else 'DEEP';value.gwf.remove(('S2',kind))
                        row='S2 LATERAL 0.003 * (HGW - HCB)\n' if kind=='LATERAL' else 'S2 DEEP 0.002 * (HGW / HGS)\n'
                        oracle=oracle.replace(row,'')
                    self.assertEqual(observe(lib,root/(mode+'-literal'),oracle),observe(lib,root/mode,value.to_document().text))
                    results[mode]=histories(root/mode/'model.out')
                base=results['all'];untreated=results['no-treatment']
                self.assertEqual(untreated['flow'],base['flow'])
                self.assertEqual(untreated['groundwater'],base['groundwater'])
                for pollutant in ('A','B'):
                    self.assertGreater(sum(untreated['quality'][pollutant]),sum(base['quality'][pollutant]))
                self.assertNotEqual(results['no-lateral']['groundwater']['S2'],base['groundwater']['S2'])
                self.assertNotEqual(results['no-deep']['water_table'],base['water_table'])
                EVIDENCE.append(dict(family=family,kind='active-domain-effects',groundwater_and_flow_unchanged_without_treatment=True,
                    both_treatments_reduce_concentrations=True,lateral_affects_groundwater_flow=True,deep_affects_water_table=True))

    def test_joint_graph_origins_and_all_outputs_survive_moved_checkpoint_in_fresh_parent(self):
        import easysewer
        from test_native_v2_runner_checkpoint import NativeRunnerCheckpointTests
        helper=NativeRunnerCheckpointTests()
        package=str(Path(easysewer.__file__).resolve().parent.parent);tests=str(Path(__file__).resolve().parent)
        child='''import sys
from pathlib import Path
package,tests,family,checkpoint,output,archive=sys.argv[1:]
sys.path[:0]=[package,tests]
import easysewer
assert Path(easysewer.__file__).resolve().is_relative_to(Path(package).resolve())
from d_domain_fixture import schema
from test_native_v2_runner_checkpoint import runner,resume_config
value=runner(family).resume(checkpoint,resume_config(Path(output)),schema=schema())
assert value.succeeded,(value.failure,value.diagnostics)
value.save(archive)
'''
        with patch.dict(os.environ,EASYSEWER_CHECKPOINT_PACKAGED='1'):
            for family,_,_ in FAMILIES:
                with self.subTest(family=family),tempfile.TemporaryDirectory() as directory:
                    root=Path(directory).resolve();value=edited(imported());before=state(value)
                    owners=tuple(ref(c,k) for c in ('aquifers','groundwater','gwf','treatment') for k in getattr(value,c))
                    origins=tuple(value.provenance(owner) for owner in owners)
                    original,saved=helper.original(root,family,model=value);helper.success(original)
                    self.assertTrue(saved);self.assertGreater(saved[0].simulation_seconds,0)
                    self.assertLess(saved[0].simulation_seconds,1800)
                    original.save(root/'expected');expected=RunResult.load(root/'expected')
                    self.assertEqual(state(expected.snapshot.model()),before)
                    for name in ('first','original.hsf','saved','moved'):
                        self.assertTrue((root/name).resolve().is_relative_to(root))
                    shutil.rmtree(root/'first');(root/'original.hsf').unlink();(root/'saved').rename(root/'moved')
                    process=subprocess.run([sys.executable,'-I','-B','-c',child,package,tests,family,
                        str(root/'moved'/saved[0].directory.name),str(root/'resumed'),str(root/'result')],
                        capture_output=True,text=True,timeout=90,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                    self.assertEqual(process.returncode,0,process.stdout+process.stderr)
                    actual=RunResult.load(root/'result');helper.equivalent(expected,actual)
                    restored=actual.snapshot.model();self.assertEqual(state(restored),before)
                    self.assertEqual(tuple(restored.provenance(owner) for owner in owners),origins)
                    EVIDENCE.append(dict(family=family,kind='fresh-parent-checkpoint',out_sha256=actual.output.sha256,
                        checkpoints=len(saved),preserved_origins=len(owners),original_workspace_removed=True))


if __name__=='__main__':unittest.main()
