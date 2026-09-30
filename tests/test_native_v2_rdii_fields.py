"""RDII defaults and ordered seasonal metadata against both native families."""
from dataclasses import fields, replace
import hashlib
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from easysewer import get_native_capabilities
from test_rdii_fields_v2 import fixture, load, queries, UNITS, GROUP, INFLOW
import test_native_v2_node_fields as nodes
from test_native_v2_regulator_fields import FAMILIES, library

EVIDENCE=[]


@unittest.skipUnless(get_native_capabilities()['swmm_solver'] and get_native_capabilities()['flexible_ponding'],'Native solvers unavailable')
class NativeRdiiFieldTests(unittest.TestCase):
    observe=nodes.NativeNodeFieldTests.observe

    def test_months_forms_and_defaults_preserve_complete_results(self):
        cases=[(u,'defaults',month) for u in UNITS for month in range(1,13)]
        cases += [(u,mode,1) for u in UNITS for mode in ('explicit','override','prior','legacy','crossmonth')]
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory)
            for family,name,symbol in FAMILIES:
                lib,path=library(name,symbol);rows=[]
                for units,mode,month in cases:
                    with self.subTest(family=family,units=units,mode=mode,month=month):
                        source=fixture(units,mode,month);m=load(source)
                        for name in ('rain_gage','prior_rain_gages','responses'):
                            fact=m.inspect_field(GROUP,name).semantics.effective
                            self.assertEqual(fact.status,'known');m.hydrographs.update('UH',**{name:fact.value})
                        expected=self.observe(lib,base,source,full=True)
                        actual=self.observe(lib,base,m.to_document(normalize=True).text,full=True)
                        self.assertEqual(actual,expected)
                        rows.append(dict(units=units,mode=mode,month=month,steps=len(actual['history']),out_sha256=actual['out_sha256']))
                EVIDENCE.append(dict(kind='rdii-field-native-results',family=family,cases=len(rows),rows=rows,
                    library_sha256=hashlib.sha256(Path(path).read_bytes()).hexdigest()))

    def test_truncating_peak_alone_changes_native_results(self):
        # Peak and base truncate independently. This deliberately wrong rewrite
        # must be observable, so equality tests cannot mask the distinction.
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory)
            for family,name,symbol in FAMILIES:
                lib,path=library(name,symbol);m=load(fixture())
                row=m.hydrographs['UH'].responses[0]
                row=replace(row,time_to_peak=.02055,recession_ratio=.99)
                m.hydrographs.update('UH',responses=(row,))
                original=self.observe(lib,base,m.to_document().text,full=True)
                wrong=replace(row,time_to_peak=row.native_peak_seconds/3600)
                self.assertNotEqual(wrong.native_base_seconds,row.native_base_seconds)
                m.hydrographs.update('UH',responses=(wrong,))
                altered=self.observe(lib,base,m.to_document().text,full=True)
                self.assertNotEqual(altered['out_sha256'],original['out_sha256'])
                EVIDENCE.append(dict(kind='rdii-time-truncation-counterexample',family=family,original_peak=row.native_peak_seconds,
                    original_base=row.native_base_seconds,altered_base=wrong.native_base_seconds,
                    original_out_sha256=original['out_sha256'],altered_out_sha256=altered['out_sha256']))

    def test_seasonal_sources_survive_relocated_checkpoint(self):
        from easysewer.runtime import RunResult
        import test_native_v2_runner_checkpoint as checkpoint
        helper=checkpoint.NativeRunnerCheckpointTests()
        with patch.dict(os.environ,EASYSEWER_CHECKPOINT_PACKAGED='1'):
            for family in ('standard','custom'):
                for mode in ('prior','legacy','crossmonth'):
                    with self.subTest(family=family,mode=mode),tempfile.TemporaryDirectory() as directory:
                        root=Path(directory);m=load(fixture(mode=mode));before=queries(m)
                        original,saved=helper.original(root,family,model=m);helper.success(original);self.assertTrue(saved)
                        self.assertEqual(queries(original.snapshot.model()),before)
                        original.save(root/'expected');expected=RunResult.load(root/'expected')
                        self.assertEqual(queries(expected.snapshot.model()),before)
                        shutil.rmtree(root/'first');(root/'original.hsf').unlink();(root/'saved').rename(root/'moved')
                        actual=checkpoint.runner(family).resume(root/'moved'/saved[0].directory.name,checkpoint.resume_config(root/'resumed'))
                        helper.equivalent(expected,actual);self.assertEqual(queries(actual.snapshot.model()),before)
                        EVIDENCE.append(dict(kind='rdii-fields-checkpoint',family=family,mode=mode,queries=len(before),
                            out_sha256=actual.output.sha256,original_workspace_removed=True))


if __name__=='__main__':unittest.main()
