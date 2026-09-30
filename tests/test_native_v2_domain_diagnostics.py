"""Combined groundwater/LID/RDII/quality/control warnings through real recovery."""
from dataclasses import replace
from datetime import time
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from easysewer import get_native_capabilities
from easysewer.io.inp import InpDocument
from easysewer.model import Model
from easysewer.model import controls as c, treatment as t
from easysewer.model.expressions import BinaryExpression, ExpressionNumber
from easysewer.model.inflows import DryWeatherFlow
from easysewer.model.resources import Pattern
from easysewer.model.rdii import UnitHydrograph, RdiiInflow
from easysewer.runtime import RunResult
from test_groundwater_v2 import groundwater_model
from test_lid_v2 import control, usage
from test_quality_v2 import ref
from test_rdii_v2 import response
from test_hydrology_fields_v2 import UNITS
from test_native_v2_regulator_fields import FAMILIES, library
from test_native_v2_control_fields import observe

EVIDENCE=[]
CODES={'groundwater.initial_clamp','lid.barrel_cover_default','rdii.truncated_seconds',
       'quality.co_units','inflow.repeated_pattern_kind','control.native_rain_attribute','treatment.hrt_nonstorage'}


def warning_model(units='CFS'):
    m=groundwater_model();m.update_options(end_time=time(4),allow_ponding=True)
    m.groundwater.update('S',surface_elevation=2)
    m.lid_controls.add(control('RB',extra=False));m.lid_usage.add(usage())
    m.hydrographs.add(UnitHydrograph(id='UH',rain_gage=ref('raingages','R'),responses=(response(peak=1.0001),)))
    m.rdii.add(RdiiInflow(node=ref('nodes','J'),hydrograph=ref('hydrographs','UH'),sewer_area=.2))
    m.pollutants.update('Q0',co_pollutant=ref('pollutants','Q1'),co_fraction=.01)
    m.patterns.add(Pattern(id='P1',kind='MONTHLY',factors=(1.,)))
    m.patterns.add(Pattern(id='P2',kind='MONTHLY',factors=(.5,)))
    m.dwf.add(DryWeatherFlow(node=ref('nodes','J'),baseline=.01,patterns=(ref('patterns','P1'),ref('patterns','P2'))))
    m.controls.add(c.ControlVariable(id='RainWindow',value=c.Attribute(object_type='GAGE',attribute='DEPTH',
        history_hours=8,target=ref('raingages','R'))))
    m.treatment.add(t.Treatment(node=ref('nodes','O'),pollutant=ref('pollutants','Q0'),kind='R',
        expression=BinaryExpression(operator='*',left=ExpressionNumber(value=0),right=t.TreatmentProcessVariable(name='HRT'))))
    m.convert_units(units)
    return Model.from_document(InpDocument.from_text(m.to_document().text,source='original-combined.inp'),strict=True)


def warnings(result):
    return tuple(d for d in result.diagnostics.diagnostics if d.code in CODES)


@unittest.skipUnless(get_native_capabilities()['swmm_solver'] and get_native_capabilities()['flexible_ponding'],
                     'Both solver families required')
class NativeDomainDiagnosticTests(unittest.TestCase):
    def test_complete_combined_results_six_units_and_both_families(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for family,name,symbol in FAMILIES:
                lib,_=library(name,symbol);rows=[]
                for units in UNITS:
                    with self.subTest(family=family,units=units):
                        m=warning_model(units);report=m.validate(for_run=True)
                        self.assertTrue(report.is_valid,report)
                        ds=[d for d in report.diagnostics if d.code in CODES]
                        self.assertEqual({d.code for d in ds},CODES)
                        self.assertTrue(all(d.subject and any(v.spans for v in d.locations) for d in ds))
                        restored=Model.from_json_document(m.to_json_document(),strict=True)
                        self.assertEqual(restored.validate(for_run=True),report)
                        expected=observe(self,lib,root,m.document.text)
                        self.assertEqual(observe(self,lib,root,restored.to_document(normalize=True).text),expected)
                        rows.append(dict(units=units,codes=sorted({d.code for d in ds}),**expected))
                EVIDENCE.append(dict(kind='domain-diagnostic-results',family=family,rows=rows))

    def test_related_sources_survive_moved_recovery_and_result_archive(self):
        import test_native_v2_runner_checkpoint as checkpoint
        helper=checkpoint.NativeRunnerCheckpointTests()
        with patch.dict(os.environ,EASYSEWER_CHECKPOINT_PACKAGED='1'):
            for family in ('standard','custom'):
                with self.subTest(family=family),tempfile.TemporaryDirectory() as directory:
                    root=Path(directory);original,saved=helper.original(root,family,model=warning_model())
                    helper.success(original);self.assertTrue(saved)
                    before=warnings(original);self.assertEqual({d.code for d in before},CODES)
                    self.assertTrue(all(any(s.source=='original-combined.inp' for v in d.locations for s in v.spans) for d in before))
                    original.save(root/'expected');expected=RunResult.load(root/'expected')
                    self.assertEqual(warnings(expected),before)
                    shutil.rmtree(root/'first');(root/'original.hsf').unlink();(root/'saved').rename(root/'moved')
                    resumed=checkpoint.runner(family).resume(root/'moved'/saved[0].directory.name,checkpoint.resume_config(root/'resumed'))
                    helper.equivalent(expected,resumed)
                    after=warnings(resumed);self.assertEqual(after[:len(before)],before)
                    revalidated=after[len(before):]
                    self.assertEqual({d.code for d in revalidated},CODES-{'rdii.truncated_seconds','control.native_rain_attribute'})
                    self.assertTrue(all(v.source_sha256==original.snapshot.input_sha256
                        for d in revalidated for v in d.locations if v.spans))
                    self.assertTrue(all(s.source=='checkpoint-input:'+original.snapshot.input_sha256
                        for d in revalidated for v in d.locations for s in v.spans))
                    resumed.save(root/'archive');self.assertEqual(warnings(RunResult.load(root/'archive')),after)
                    EVIDENCE.append(dict(kind='domain-diagnostic-recovery',family=family,
                        original_workspace_removed=True,out_sha256=resumed.output.sha256,diagnostics=len(before),
                        revalidated_diagnostics=len(revalidated),revalidation_source_labelled=True))


if __name__=='__main__':unittest.main()
