"""Both native families: surface defaults, literal conversions and recovery."""
from dataclasses import replace
import hashlib
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from easysewer import get_native_capabilities
from easysewer.io.inp import InpDocument
from test_surface_fields_v2 import fixture, load, queries, materialize, UNITS, KINDS
from test_native_v2_regulator_fields import FAMILIES, library
import test_native_v2_node_fields as nodes

EVIDENCE=[]
TRANSECTS=('transect','inherited','zero-factors')


def independent_surface_conversion(original, converted, units):
    """Pinned native LENGTH/FLOW factors, without querying the new contracts."""
    length=.3048 if units in UNITS[3:] else 1.
    flow={'GPM':448.831,'MGD':.64632,'CMS':.02832,'LPS':28.317,'MLD':2.4466}[units]
    sections=('STREETS','INLETS','INLET_USAGE','TRANSECTS')
    rows={section:[] for section in sections}
    width=1.
    for line in InpDocument.from_text(original).lines:
        if line.section not in sections or line.kind!='data':continue
        v=list(line.values)
        def scale(col,factor):
            if col<len(v):v[col]=format(float(v[col])*factor,'.17g')
        if line.section=='STREETS':
            for col in (1,2,5,6,8):scale(col,length)
        elif line.section=='INLETS':
            if v[1]!='CUSTOM':
                scale(2,length);scale(3,length)
                if len(v)>6 and v[4]=='GENERIC':scale(6,length)
        elif line.section=='INLET_USAGE':
            scale(5,flow);scale(6,length);scale(7,length)
        elif v[0]=='X1':
            width=(float(v[8]) or 1.) if length!=1 else 1.
            # Materialize width only across US/SI; match native bank/station
            # operation ordering, not a dimension annotation in the model.
            for col in (3,4):scale(col,width);scale(col,length)
            if length!=1:v[8]='1'
            scale(9,length**2)
        elif v[0]=='GR':
            for col in range(1,len(v),2):scale(col,length);scale(col+1,width);scale(col+1,length)
        rows[line.section].append(' '.join(v))
    base='\n'.join(line.content for line in InpDocument.from_text(converted).lines if line.section not in sections)+'\n'
    for section in sections:
        if rows[section]:base+='['+section+']\n'+'\n'.join(rows[section])+'\n'
    return base+'[REPORT]\n'


@unittest.skipUnless(get_native_capabilities()['swmm_solver'] and get_native_capabilities()['flexible_ponding'],
                     'Both native families unavailable')
class NativeSurfaceFieldTests(unittest.TestCase):
    observe=nodes.NativeNodeFieldTests.observe

    def test_all_variants_defaults_and_original_stateful_inputs(self):
        cases=[(kind,units,extra) for kind in KINDS for units in UNITS for extra in (False,True)]
        cases += [(kind,units,False) for kind in TRANSECTS for units in UNITS]
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory)
            for family,name,symbol in FAMILIES:
                lib,path=library(name,symbol);rows=[]
                for kind,units,extra in cases:
                    with self.subTest(family=family,kind=kind,units=units,extra=extra):
                        source=fixture(kind,units,extra);m=load(source)
                        expected=self.observe(lib,base,source,full=True)
                        materialize(m)
                        actual=self.observe(lib,base,m.to_document(normalize=True).text,full=True)
                        self.assertEqual(actual,expected)
                        rows.append(dict(surface_kind=kind,units=units,extra=extra,steps=len(actual['history']),out_sha256=actual['out_sha256']))
                EVIDENCE.append(dict(kind='surface-field-native-results',family=family,cases=len(rows),rows=rows,
                    library_sha256=hashlib.sha256(Path(path).read_bytes()).hexdigest()))

    def test_inherited_channel_roughness_cannot_be_replaced_by_original_nc(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory)
            for family,name,symbol in FAMILIES:
                lib,path=library(name,symbol);m=load(fixture('inherited'))
                original=self.observe(lib,base,m.to_document().text,full=True)
                m.transects.update('T',roughness=replace(m.transects['T'].roughness,channel=.02))
                altered=self.observe(lib,base,m.to_document().text,full=True)
                self.assertNotEqual(original['out_sha256'],altered['out_sha256'])
                EVIDENCE.append(dict(kind='surface-roughness-counterexample',family=family,
                    original_out_sha256=original['out_sha256'],altered_out_sha256=altered['out_sha256']))

    def test_unit_conversion_against_independent_surface_tokens(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory)
            for family,name,symbol in FAMILIES:
                lib,path=library(name,symbol);rows=[]
                for kind in (*KINDS,'transect','zero-factors'):
                    for units in UNITS[1:]:
                        with self.subTest(family=family,kind=kind,units=units):
                            m=load(fixture(kind,extra=True));original=m.to_document(normalize=True).text
                            m.convert_units(units);converted=m.to_document(normalize=True).text
                            expected=self.observe(lib,base,independent_surface_conversion(original,converted,units),full=True)
                            actual=self.observe(lib,base,converted,full=True)
                            self.assertEqual(actual,expected)
                            rows.append(dict(surface_kind=kind,units=units,out_sha256=actual['out_sha256']))
                EVIDENCE.append(dict(kind='surface-field-conversion',family=family,cases=len(rows),rows=rows))

    def test_field_sources_survive_relocated_checkpoint_and_result_archive(self):
        from easysewer.runtime import RunResult
        import test_native_v2_runner_checkpoint as checkpoint
        helper=checkpoint.NativeRunnerCheckpointTests()
        with patch.dict(os.environ,EASYSEWER_CHECKPOINT_PACKAGED='1'):
            for family in ('standard','custom'):
                for kind in ('inherited','Combo','CR'):
                    with self.subTest(family=family,kind=kind),tempfile.TemporaryDirectory() as directory:
                        root=Path(directory);m=load(fixture(kind,extra=True));before=queries(m)
                        original,saved=helper.original(root,family,model=m);helper.success(original);self.assertTrue(saved)
                        self.assertEqual(queries(original.snapshot.model()),before)
                        original.save(root/'expected');expected=RunResult.load(root/'expected')
                        self.assertEqual(queries(expected.snapshot.model()),before)
                        shutil.rmtree(root/'first');(root/'original.hsf').unlink();(root/'saved').rename(root/'moved')
                        actual=checkpoint.runner(family).resume(root/'moved'/saved[0].directory.name,checkpoint.resume_config(root/'resumed'))
                        helper.equivalent(expected,actual);self.assertEqual(queries(actual.snapshot.model()),before)
                        EVIDENCE.append(dict(kind='surface-fields-checkpoint',family=family,surface_kind=kind,queries=len(before),
                            out_sha256=actual.output.sha256,original_workspace_removed=True))


if __name__=='__main__':unittest.main()
