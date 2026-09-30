"""Configured treatment facts against complete water-quality results and recovery."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
from itertools import product

from easysewer import get_native_capabilities
from easysewer.io.inp import InpDocument
from test_treatment_fields_v2 import fixture,load,queries,materialize,UNITS,POLLUTANT_UNITS,NODE_KINDS,FORMULAS
from test_native_v2_regulator_fields import FAMILIES,library
from test_native_v2_control_fields import observe as control_observe
import test_native_v2_hotstart as native_hotstart

EVIDENCE=[]
LEXICAL=('J Q0 C= Q0 + Q0\n','J Q0 C = Q0 + Q0\n','J Q0 "C = Q0 + Q0"\n',
         'J Q0 cResult ignored = "Q0 + Q0"\n','J Q0 R=.1\nJ Q0 C=.5*Q0\n')


def cases():
    for node,units,pu,a,b in product(NODE_KINDS,UNITS,POLLUTANT_UNITS,('C','R'),('C','R')):
        yield ('matrix',node,units,pu,a,b),fixture(node,units,pu,(a,b))
    for units in UNITS:
        for i,formula in enumerate(FORMULAS):yield ('formula',units,i),fixture(units=units,rows='J Q0 R='+formula+'\n')
        for i,row in enumerate(LEXICAL):yield ('lexical',units,i),fixture(units=units,rows=row)
        yield ('shadow',units,'process'),fixture(units=units,rows='J Q0 C=.5*Q0\n').replace('Q0','FLOWER')
        yield ('shadow',units,'concentration'),fixture(units=units,rows='J Q0 R=.2\nJ Q1 C=.5*Q1\n').replace('Q1','R_Q0')


def observe(test,lib,base,source):
    result=control_observe(test,lib,base,source)
    quality=native_hotstart.NativeHotstartTests.quality(test,base,'model')
    test.assertGreater(len(quality[1][0][0]),10)
    result['quality']=quality
    return result


@unittest.skipUnless(get_native_capabilities()['swmm_solver'] and get_native_capabilities()['flexible_ponding'] and get_native_capabilities()['swmm_output'],'Native solver/output libraries unavailable')
class NativeTreatmentFieldTests(unittest.TestCase):
    def test_configured_fields_preserve_complete_quality_results(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory)
            for family,name,symbol in FAMILIES:
                lib,path=library(name,symbol);rows=[]
                for case,source in cases():
                    with self.subTest(family=family,case=case):
                        m=load(source);materialize(m)
                        expected=observe(self,lib,base,source);actual=observe(self,lib,base,m.to_document(normalize=True).text)
                        self.assertEqual(actual,expected)
                        self.assertGreater(max(actual['quality'][1][0][0]),0)
                        rows.append(dict(case=case,steps=len(actual['history']),periods=len(actual['quality'][1][0][0]),
                            quality_sha256=hashlib.sha256(json.dumps(actual['quality']).encode()).hexdigest(),out_sha256=actual['out_sha256'],report_sha256=actual['report_sha256']))
                EVIDENCE.append(dict(kind='treatment-field-native-results',family=family,cases=len(rows),rows=rows,library_sha256=hashlib.sha256(Path(path).read_bytes()).hexdigest()))

    def test_hydraulic_and_pollutant_conversion_against_literal_substitution(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory)
            for family,name,symbol in FAMILIES:
                lib,path=library(name,symbol);rows=[]
                formula='.1+.001*FLOW+.001*DEPTH+.000001*AREA+.0001*DT+.001*HRT'
                for units,flow,length in (('GPM',448.831,1.),('MGD',.64632,1.),('CMS',.02832,.3048),('LPS',28.317,.3048),('MLD',2.4466,.3048)):
                    m=load(fixture(rows='J Q0 R='+formula+'\n'));m.convert_units(units)
                    converted=m.to_document(normalize=True).text
                    source='\n'.join(line.content for line in InpDocument.from_text(converted).lines if line.section!='TREATMENT')+'\n'
                    oracle=f'.1+.001*(FLOW/{flow!r})+.001*(DEPTH/{length!r})+.000001*(AREA/{length**2!r})+.0001*DT+.001*HRT'
                    actual=observe(self,lib,base,converted);self.assertEqual(actual,observe(self,lib,base,source+'[TREATMENT]\nJ Q0 R='+oracle+'\n'))
                    rows.append(dict(conversion='hydraulic',target=units,out_sha256=actual['out_sha256']))
                for kind in ('C','R'):
                    formula='.2*Q0+.01*Q1' if kind=='C' else '.01*Q0+.001*Q1'
                    m=load(fixture(rows=f'J Q0 {kind}={formula}\nJ Q1 C=.3*Q1+.01*Q0+.1*R_Q0\n'))
                    m.convert_pollutant_units('Q0','UG/L');m.convert_pollutant_units('Q1','MG/L')
                    converted=m.to_document(normalize=True).text
                    source='\n'.join(line.content for line in InpDocument.from_text(converted).lines if line.section!='TREATMENT')+'\n'
                    literal=formula.replace('Q0','(Q0/1000)').replace('Q1','(Q1/.001)')
                    if kind=='C':literal='1000*('+literal+')'
                    oracle=f'[TREATMENT]\nJ Q0 {kind}={literal}\nJ Q1 C=.001*(.3*(Q1/.001)+.01*(Q0/1000)+.1*R_Q0)\n'
                    actual=observe(self,lib,base,converted);self.assertEqual(actual,observe(self,lib,base,source+oracle))
                    rows.append(dict(conversion='pollutant',target=kind,out_sha256=actual['out_sha256']))
                EVIDENCE.append(dict(kind='treatment-field-conversion',family=family,cases=len(rows),rows=rows))

    def test_absence_zero_removal_and_no_inflow_kind_have_observable_differences(self):
        from test_native_v2_treatment import no_inflow_source
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory)
            for family,name,symbol in FAMILIES:
                lib,path=library(name,symbol)
                m=load(fixture(rows=''));m.pollutants.update('Q0',decay_rate=.8);source=m.to_document().text
                absent=observe(self,lib,base,source);zero=observe(self,lib,base,source+'[TREATMENT]\nJ Q0 R=0\n')
                self.assertNotEqual(absent['quality'],zero['quality'])
                source=no_inflow_source();removal=observe(self,lib,base,source+'[TREATMENT]\nJ Q0 R=.5\n');concentration=observe(self,lib,base,source+'[TREATMENT]\nJ Q0 C=.5*Q0\n')
                self.assertNotEqual(removal['quality'],concentration['quality'])
                EVIDENCE.append(dict(kind='treatment-runtime-counterexamples',family=family,absent_out_sha256=absent['out_sha256'],zero_out_sha256=zero['out_sha256'],removal_out_sha256=removal['out_sha256'],concentration_out_sha256=concentration['out_sha256']))

    def test_equation_sources_survive_result_archive_and_moved_checkpoint(self):
        from easysewer.runtime import RunResult
        import test_native_v2_runner_checkpoint as checkpoint
        helper=checkpoint.NativeRunnerCheckpointTests()
        with patch.dict(os.environ,EASYSEWER_CHECKPOINT_PACKAGED='1'):
            for family in ('standard','custom'):
                for case,source in (('mixed',fixture()),('process',fixture(rows='J Q0 R=.1+.0001*HRT+.00001*AREA\n')),('duplicate',fixture(rows=LEXICAL[-1]))):
                    with self.subTest(family=family,case=case),tempfile.TemporaryDirectory() as directory:
                        root=Path(directory);m=load(source);before=queries(m)
                        original,saved=helper.original(root,family,model=m);helper.success(original);self.assertTrue(saved)
                        self.assertEqual(queries(original.snapshot.model()),before)
                        original.save(root/'expected');expected=RunResult.load(root/'expected')
                        self.assertEqual(queries(expected.snapshot.model()),before)
                        shutil.rmtree(root/'first');(root/'original.hsf').unlink();(root/'saved').rename(root/'moved')
                        actual=checkpoint.runner(family).resume(root/'moved'/saved[0].directory.name,checkpoint.resume_config(root/'resumed'))
                        helper.equivalent(expected,actual);self.assertEqual(queries(actual.snapshot.model()),before)
                        EVIDENCE.append(dict(kind='treatment-fields-checkpoint',family=family,case=case,queries=len(before),out_sha256=actual.output.sha256,original_workspace_removed=True))


if __name__=='__main__':unittest.main()
