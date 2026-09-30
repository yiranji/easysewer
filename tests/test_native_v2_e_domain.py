"""E complete-output oracles, active seasonal effects and moved checkpoints."""
from dataclasses import replace
import hashlib,os,shutil,subprocess,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from easysewer import get_native_capabilities
from easysewer.io.output import OutputReader
from easysewer.io.runoff_cache import RdiiData
from easysewer.model import Model
from easysewer.runtime import RunResult
from easysewer.utils import probe_library_path
from e_domain_fixture import ORACLE,model,imported,edited,edited_oracle,state,ref
from test_native_v2_b_domain import observe,FAMILIES
from test_native_v2_standard_io import direct_library
from test_scenario_v2 import portable

EVIDENCE=[]

@unittest.skipUnless(get_native_capabilities()['swmm_solver'] and get_native_capabilities()['flexible_ponding'],
                    'Standard/custom native solvers unavailable')
class NativeEDomainTests(unittest.TestCase):
    def test_both_engines_full_outputs_match_independent_legacy_and_edited_oracles(self):
        for family,name,symbol in FAMILIES:
            lib,library=direct_library(probe_library_path(name),revision_symbol=symbol);hashes={}
            for kind,literal,value in (('created',ORACLE,model()),('edited-created',edited_oracle(),edited(model())),
                ('edited-imported',edited_oracle(),edited())):
                with self.subTest(family=family,kind=kind),tempfile.TemporaryDirectory() as directory:
                    root=Path(directory)
                    sources=(literal,value.to_document().text,Model.from_document(value.to_document(),strict=True).to_document().text,
                        portable(value).to_document().text)
                    outputs=[observe(lib,root/str(i),source+'[FILES]\nSAVE RDII rdii.bin\n') for i,source in enumerate(sources)]
                    for actual in outputs[1:]:self.assertEqual(actual,outputs[0])
                    raw=(root/'0/rdii.bin').read_bytes();data=RdiiData.from_bytes(raw)
                    for i in range(1,4):self.assertEqual((root/str(i)/'rdii.bin').read_bytes(),raw)
                    self.assertEqual({f.time.month for f in data.frames},{1,2})
                    self.assertEqual(len(data.node_indices),2)
                    for month in (1,2):
                        for i in range(2):self.assertGreater(max(f.flows[i] for f in data.frames if f.time.month==month),0)
                    hashes[kind]=hashlib.sha256(outputs[0][0]).hexdigest()
                    EVIDENCE.append(dict(family=family,kind=kind,comparisons=3,out_sha256=hashes[kind],
                        rdii_sha256=hashlib.sha256(raw).hexdigest(),library_sha256=hashlib.sha256(library.read_bytes()).hexdigest(),
                        months=[1,2],active_nodes=2,frames=len(data.frames)))
            self.assertEqual(hashes['edited-created'],hashes['edited-imported'])
            self.assertNotEqual(hashes['created'],hashes['edited-created'])

    def test_month_override_later_all_components_and_prior_gage_have_distinct_native_effects(self):
        for family,name,symbol in FAMILIES:
            lib,_=direct_library(probe_library_path(name),revision_symbol=symbol)
            with self.subTest(family=family),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);data={};flows={}
                for mode in ('all','no-feb','no-later-all','no-medium','no-long','no-prior-gage'):
                    value=model();literal=ORACLE;group=value.hydrographs['Shared']
                    if mode=='no-feb':
                        value.hydrographs.update('Shared',responses=tuple(r for r in group.responses if r.month!='FEB'))
                        literal=literal.replace('Shared FEB MEDIUM .16 .1 2 0 0 0\n','').replace('Shared FEB SHORT .3 .025 1 0 0 0\n','')
                    elif mode=='no-later-all':
                        value.hydrographs.update('Shared',responses=tuple(r for i,r in enumerate(group.responses) if i!=5))
                        literal=literal.replace('Shared ALL SHORT .1 .05 1\n','')
                    elif mode in ('no-medium','no-long'):
                        kind='MEDIUM' if mode=='no-medium' else 'LONG'
                        value.hydrographs.update('Shared',responses=tuple(replace(r,fraction=0) if r.response==kind else r for r in group.responses))
                        if kind=='MEDIUM':literal=literal.replace('.08 .1 2','0 .1 2').replace('FEB MEDIUM .16','FEB MEDIUM 0')
                        else:literal=literal.replace('.04 .2 3','0 .2 3')
                    elif mode=='no-prior-gage':
                        value.hydrographs.update('Shared',prior_rain_gages=())
                        literal=literal.replace('Shared Earlier\n','')
                    self.assertEqual(observe(lib,root/(mode+'-literal'),literal+'[FILES]\nSAVE RDII rdii.bin\n'),
                        observe(lib,root/mode,value.to_document().text+'[FILES]\nSAVE RDII rdii.bin\n'))
                    data[mode]=RdiiData.read(root/mode/'rdii.bin')
                    with OutputReader(root/mode/'model.out') as reader:flows[mode]=reader.series(ref('links','P'),'swmm:flow').values
                by_month=lambda value,month:tuple((f.native_time,f.flows) for f in value.frames if f.time.month==month)
                self.assertEqual(by_month(data['all'],1),by_month(data['no-feb'],1))
                self.assertNotEqual(by_month(data['all'],2),by_month(data['no-feb'],2))
                self.assertNotEqual(by_month(data['all'],1),by_month(data['no-later-all'],1))
                for mode in ('no-medium','no-long'):
                    self.assertNotEqual(data['all'].frames,data[mode].frames)
                    self.assertGreater(sum(sum(f.flows) for f in data['all'].frames),sum(sum(f.flows) for f in data[mode].frames))
                self.assertEqual(data['all'],data['no-prior-gage'])
                self.assertNotEqual(flows['all'],flows['no-prior-gage'])
                EVIDENCE.append(dict(family=family,kind='active-effects',feb_changes_only_after_boundary=True,
                    later_all_overrides_january=True,medium_and_long_contribute=True,prior_gage_changes_control_only=True))

    def test_fresh_parent_resume_on_each_side_of_month_boundary_preserves_shared_graph(self):
        import easysewer
        import test_native_v2_runner_checkpoint as helper_module
        helper=helper_module.NativeRunnerCheckpointTests()
        package=str(Path(easysewer.__file__).resolve().parent.parent);tests=str(Path(__file__).resolve().parent)
        child='''import sys
from pathlib import Path
package,tests,family,checkpoint,output,archive=sys.argv[1:]
sys.path[:0]=[package,tests]
import easysewer
assert Path(easysewer.__file__).resolve().is_relative_to(Path(package).resolve())
from e_domain_fixture import schema
from test_native_v2_runner_checkpoint import runner,resume_config
value=runner(family).resume(checkpoint,resume_config(Path(output)),schema=schema())
assert value.succeeded,(value.failure,value.diagnostics)
value.save(archive)
'''
        original_schedule=helper_module.schedule
        def schedule(path,callback=None,seconds=120):return original_schedule(path,callback,seconds=900)
        with patch.dict(os.environ,EASYSEWER_CHECKPOINT_PACKAGED='1'),patch.object(helper_module,'schedule',schedule):
            for family,_,_ in FAMILIES:
                with self.subTest(family=family),tempfile.TemporaryDirectory() as directory:
                    root=Path(directory).resolve();value=edited(imported());before=state(value)
                    owners=(ref('hydrographs','Seasonal'),ref('rdii','Receiving'),ref('rdii','K'))
                    origins=tuple(value.provenance(owner) for owner in owners)
                    original,saved=helper.original(root,family,model=value);helper.success(original)
                    selected=(next(s for s in saved if 0<s.simulation_seconds<1800),next(s for s in saved if 1800<s.simulation_seconds<7200))
                    original.save(root/'expected');expected=RunResult.load(root/'expected')
                    self.assertEqual(state(expected.snapshot.model()),before)
                    for name in ('first','original.hsf','saved','moved'):self.assertTrue((root/name).resolve().is_relative_to(root))
                    shutil.rmtree(root/'first');(root/'original.hsf').unlink();(root/'saved').rename(root/'moved')
                    for index,checkpoint in enumerate(selected):
                        process=subprocess.run([sys.executable,'-I','-B','-c',child,package,tests,family,
                            str(root/'moved'/checkpoint.directory.name),str(root/('resumed'+str(index))),str(root/('result'+str(index)))],
                            capture_output=True,text=True,timeout=120,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                        self.assertEqual(process.returncode,0,process.stdout+process.stderr)
                        actual=RunResult.load(root/('result'+str(index)));helper.equivalent(expected,actual)
                        restored=actual.snapshot.model();self.assertEqual(state(restored),before)
                        self.assertEqual(tuple(restored.provenance(owner) for owner in owners),origins)
                        EVIDENCE.append(dict(family=family,kind='fresh-parent-'+str(index),out_sha256=actual.output.sha256,
                            simulation_seconds=checkpoint.simulation_seconds,checkpoints=len(saved),preserved_origins=3,original_workspace_removed=True))

if __name__=='__main__':unittest.main()
