"""Located snow-transfer warnings survive real results and moved recovery."""
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
from easysewer.model import Model, Ref
from easysewer.model import hydrology as h, climate as c
from easysewer.runtime import RunResult
from test_hydrology_v2 import hydrology_model, snowpack
from test_hydrology_fields_v2 import UNITS
from test_native_v2_regulator_fields import FAMILIES, library
from test_native_v2_control_fields import observe

EVIDENCE=[]


def warning_model(units='CFS'):
    m=hydrology_model();m.update_options(allow_ponding=True,end_date=m.options.start_date,end_time=time(4))
    m.snowpacks.add(snowpack(removal=h.SnowRemoval(threshold=0,out_of_system=0,to_impervious=0,
        to_pervious=0,immediate_melt=0,to_subcatchment=.25,destination=Ref(collection='swmm:subcatchments',key='Receiver'))))
    m.subcatchments.update('S',snowpack=Ref(collection='swmm:snowpacks',key='Snow'))
    m.subcatchments.add(replace(m.subcatchments['S'],id='Receiver',area=3,impervious_percent=100,snowpack=None))
    m.update_climate(evaporation=c.Evaporation(source=c.ConstantEvaporation(rate=.1)),
        wind=c.MonthlyWindSpeeds(values=(1.0,)*12))
    m.convert_units(units)
    return Model.from_document(InpDocument.from_text(m.to_document().text,source='original-snow-climate.inp'),strict=True)


def warnings(result):
    return tuple(d for d in result.diagnostics.diagnostics if d.code=='snowpack.ignored_transfer')


@unittest.skipUnless(get_native_capabilities()['swmm_solver'] and get_native_capabilities()['flexible_ponding'],
                     'Both solver families required')
class NativeHydroClimateDiagnosticTests(unittest.TestCase):
    def test_complete_results_six_units_both_families(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for family,name,symbol in FAMILIES:
                lib,_=library(name,symbol);rows=[]
                for units in UNITS:
                    with self.subTest(family=family,units=units):
                        m=warning_model(units);report=m.validate(for_run=True)
                        self.assertTrue(report.is_valid,report)
                        d=next(d for d in report.diagnostics if d.code=='snowpack.ignored_transfer')
                        self.assertEqual([v.status for v in d.locations],['current','omitted','current'])
                        restored=Model.from_json_document(m.to_json_document(),strict=True)
                        self.assertEqual(restored.validate(for_run=True),report)
                        expected=observe(self,lib,root,m.document.text)
                        self.assertEqual(observe(self,lib,root,restored.to_document(normalize=True).text),expected)
                        rows.append(dict(units=units,**expected))
                EVIDENCE.append(dict(kind='hydro-climate-diagnostic-results',family=family,rows=rows))

    def test_warning_related_sources_moved_recovery_and_archive(self):
        import test_native_v2_runner_checkpoint as checkpoint
        helper=checkpoint.NativeRunnerCheckpointTests()
        with patch.dict(os.environ,EASYSEWER_CHECKPOINT_PACKAGED='1'):
            for family in ('standard','custom'):
                with self.subTest(family=family),tempfile.TemporaryDirectory() as directory:
                    root=Path(directory);original,saved=helper.original(root,family,model=warning_model())
                    helper.success(original);self.assertTrue(saved)
                    before=warnings(original);self.assertTrue(before)
                    self.assertEqual(before[0].subject.collection,'swmm:snowpacks')
                    self.assertEqual({s.collection for s in before[0].related},{'swmm:subcatchments'})
                    self.assertEqual(before[0].locations[0].spans[0].source,'original-snow-climate.inp')
                    original.save(root/'expected');expected=RunResult.load(root/'expected')
                    self.assertEqual(warnings(expected),before)
                    shutil.rmtree(root/'first');(root/'original.hsf').unlink();(root/'saved').rename(root/'moved')
                    resumed=checkpoint.runner(family).resume(root/'moved'/saved[0].directory.name,
                        checkpoint.resume_config(root/'resumed'))
                    helper.equivalent(expected,resumed);self.assertEqual(warnings(resumed),before)
                    resumed.save(root/'archive');self.assertEqual(warnings(RunResult.load(root/'archive')),before)
                    EVIDENCE.append(dict(kind='hydro-climate-diagnostic-recovery',family=family,
                        original_workspace_removed=True,out_sha256=resumed.output.sha256,diagnostics=len(before)))


if __name__=='__main__':unittest.main()
