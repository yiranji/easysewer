"""LID field writeback, detailed reports and recovery in both native families."""
from dataclasses import replace
import hashlib
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from easysewer import get_native_capabilities
from easysewer.model import lid as l
from test_lid_fields_v2 import fixture, load, queries, materialize, UNITS, CONTROL
from test_native_v2_regulator_fields import FAMILIES, library
import test_native_v2_node_fields as nodes

EVIDENCE=[]
MODES=(('BC','zero-surface'),('BC','no-storage'),('PP','no-storage'),
       ('PP','multiple'),('IT','impervious'),('RB','disabled'))


def detail_hash(path):
    return hashlib.sha256(path.read_bytes().replace(b'\r\n',b'\n')).hexdigest()


@unittest.skipUnless(get_native_capabilities()['swmm_solver'] and get_native_capabilities()['flexible_ponding'],
                     'Both native families unavailable')
class NativeLidFieldTests(unittest.TestCase):
    observe=nodes.NativeNodeFieldTests.observe

    def test_all_types_defaults_overrides_and_complete_reports(self):
        cases=[(kind,units,extra,'normal') for kind in l.LID_KINDS for units in UNITS for extra in (False,True)]
        cases += [(kind,units,False,mode) for units in UNITS for kind,mode in MODES]
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory);detail=base/'detail.txt'
            for family,name,symbol in FAMILIES:
                lib,path=library(name,symbol);rows=[]
                for kind,units,extra,mode in cases:
                    with self.subTest(family=family,kind=kind,units=units,extra=extra,mode=mode):
                        source=fixture(kind,units,extra,mode,detail);m=load(source)
                        expected=self.observe(lib,base,source,full=True);expected_detail=detail.read_bytes()
                        self.assertIn(b'Storage',expected_detail)
                        materialize(m)
                        actual=self.observe(lib,base,m.to_document(normalize=True).text,full=True)
                        self.assertEqual(actual,expected);self.assertEqual(detail.read_bytes(),expected_detail)
                        rows.append(dict(lid_kind=kind,units=units,extra=extra,mode=mode,steps=len(actual['history']),
                            out_sha256=actual['out_sha256'],detail_normalized_sha256=detail_hash(detail)))
                EVIDENCE.append(dict(kind='lid-field-native-results',family=family,cases=len(rows),rows=rows,
                    library_sha256=hashlib.sha256(Path(path).read_bytes()).hexdigest()))

    def test_internal_void_fraction_is_not_a_replacement_input_ratio(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory)
            for family,name,symbol in FAMILIES:
                lib,path=library(name,symbol);m=load(fixture('IT'))
                original=self.observe(lib,base,m.to_document().text,full=True)
                layer=m.lid_controls['L'].storage
                m.lid_controls.update('L',storage=replace(layer,void_ratio=layer.void_ratio/(1+layer.void_ratio)))
                altered=self.observe(lib,base,m.to_document().text,full=True)
                self.assertNotEqual(original['out_sha256'],altered['out_sha256'])
                EVIDENCE.append(dict(kind='lid-ratio-counterexample',family=family,
                    original_out_sha256=original['out_sha256'],altered_out_sha256=altered['out_sha256']))

    def test_unit_conversion_against_independent_layer_numbers(self):
        from test_native_v2_lid import literal_lid, si_lid
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory)
            for family,name,symbol in FAMILIES:
                lib,path=library(name,symbol);rows=[]
                for kind in l.LID_KINDS:
                    for units in UNITS[1:]:
                        with self.subTest(family=family,kind=kind,units=units):
                            m=load(fixture(kind,extra=True))
                            # Independent literal fixture covers the same input
                            # parameters. It uses an explicit zero ToPerv flag.
                            m.convert_units(units)
                            bare=load(fixture(kind,extra=True));bare.lid_usage.remove('lid-usage-1');bare.lid_controls.remove('L')
                            if 'Head' in bare.curves:bare.curves.remove('Head')
                            bare.convert_units(units)
                            suffix=literal_lid(kind,advanced=True)
                            if units in UNITS[3:]:suffix=si_lid(suffix)
                            expected=self.observe(lib,base,bare.to_document().text+suffix,full=True)
                            actual=self.observe(lib,base,m.to_document().text,full=True)
                            self.assertEqual(actual,expected)
                            rows.append(dict(lid_kind=kind,units=units,out_sha256=actual['out_sha256']))
                EVIDENCE.append(dict(kind='lid-field-conversion',family=family,cases=len(rows),rows=rows))

    def test_field_sources_and_detail_report_survive_relocated_checkpoint(self):
        from easysewer.runtime import RunResult
        import test_native_v2_runner_checkpoint as checkpoint
        helper=checkpoint.NativeRunnerCheckpointTests()
        with patch.dict(os.environ,EASYSEWER_CHECKPOINT_PACKAGED='1'):
            for family in ('standard','custom'):
                for kind in ('BC','PP','GR'):
                    with self.subTest(family=family,kind=kind),tempfile.TemporaryDirectory() as directory:
                        root=Path(directory);detail=root/'detail.txt';m=load(fixture(kind,extra=True,detail=detail));before=queries(m)
                        original,saved=helper.original(root,family,model=m);helper.success(original);self.assertTrue(saved)
                        self.assertEqual(queries(original.snapshot.model()),before)
                        original.save(root/'expected');expected=RunResult.load(root/'expected')
                        self.assertEqual(queries(expected.snapshot.model()),before)
                        shutil.rmtree(root/'first');(root/'original.hsf').unlink();detail.unlink();(root/'saved').rename(root/'moved')
                        actual=checkpoint.runner(family).resume(root/'moved'/saved[0].directory.name,checkpoint.resume_config(root/'resumed'))
                        helper.equivalent(expected,actual);self.assertEqual(queries(actual.snapshot.model()),before)
                        details=[a for a in actual.artifacts if a.role=='swmm:lid-detail'];self.assertEqual(len(details),1)
                        EVIDENCE.append(dict(kind='lid-fields-checkpoint',family=family,lid_kind=kind,queries=len(before),
                            out_sha256=actual.output.sha256,original_workspace_removed=True,detail_reports=len(details)))


if __name__=='__main__':unittest.main()
