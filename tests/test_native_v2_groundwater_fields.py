"""Groundwater configuration defaults versus native state in both families."""
import hashlib
from dataclasses import replace
from itertools import product
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from easysewer import get_native_capabilities
from test_groundwater_fields_v2 import fixture, load, queries, UNITS, BINDING, OPTIONAL
from test_hydrology_fields_v2 import fixture as hydrology_fixture
import test_native_v2_node_fields as nodes
from test_native_v2_regulator_fields import FAMILIES, library

EVIDENCE=[]


@unittest.skipUnless(get_native_capabilities()['swmm_solver'] and get_native_capabilities()['flexible_ponding'],
                     'Native solvers unavailable')
class NativeGroundwaterFieldTests(unittest.TestCase):
    observe=nodes.NativeNodeFieldTests.observe

    def test_inherited_and_explicit_fields_preserve_complete_results(self):
        cases=[(units,flags,'normal') for units in UNITS for flags in product((False,True),repeat=4)]
        cases += [(units,(False,)*4,mode) for units in UNITS for mode in ('without-gwf','without-pattern','saturated','fixed-depth')]
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory)
            for family,name,symbol in FAMILIES:
                lib,path=library(name,symbol);rows=[]
                for units,flags,mode in cases:
                    with self.subTest(family=family,units=units,flags=flags,mode=mode):
                        source=fixture(units,flags,mode);m=load(source)
                        expected=self.observe(lib,base,source,full=True)
                        resolved={name:m.inspect_field(BINDING,name).semantics.effective for name in OPTIONAL}
                        self.assertTrue(all(f.status=='known' for f in resolved.values()))
                        m.groundwater.update('S',**{name:fact.value for name,fact in resolved.items()})
                        actual=self.observe(lib,base,m.to_document(normalize=True).text,full=True)
                        self.assertEqual(actual,expected)
                        rows.append(dict(units=units,flags=flags,mode=mode,steps=len(actual['history']),out_sha256=actual['out_sha256']))
                EVIDENCE.append(dict(kind='groundwater-field-native-results',family=family,cases=len(rows),rows=rows,
                    library_sha256=hashlib.sha256(Path(path).read_bytes()).hexdigest()))

    def test_initialization_clamp_is_not_an_equivalent_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory)
            for family,name,symbol in FAMILIES:
                lib,path=library(name,symbol);m=load(fixture(mode='saturated'))
                m.groundwater.update('S',upper_moisture=.3)
                m.timeseries.update('Rain',points=tuple(replace(point,value=2.) for point in m.timeseries['Rain'].points))
                original=self.observe(lib,base,m.to_document().text,full=True)
                # Rain starts immediately: otherwise the infiltration limit is
                # refreshed before the first storm and hides this distinction.
                # gwater_initState clamps depth, but the infiltration limit
                # still uses the original water-table input.
                m.groundwater.update('S',water_table_elevation=20-.001)
                altered=self.observe(lib,base,m.to_document().text,full=True)
                self.assertNotEqual(original['out_sha256'],altered['out_sha256'])
                EVIDENCE.append(dict(kind='groundwater-initialization-counterexample',family=family,
                    original_out_sha256=original['out_sha256'],altered_out_sha256=altered['out_sha256']))

    def test_unit_conversion_against_independent_input(self):
        length=.3048;flux=3048/43560
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory)
            for family,name,symbol in FAMILIES:
                lib,path=library(name,symbol);rows=[]
                for units in UNITS[1:]:
                    with self.subTest(family=family,units=units):
                        m=load(fixture());m.convert_units(units)
                        hydrology=load(hydrology_fixture());hydrology.convert_units(units)
                        if units in UNITS[:3]:
                            source=fixture();suffix=source[source.index('[PATTERNS]'):]
                        else:
                            suffix=('[PATTERNS]\nET MONTHLY .5 1.5\n[AQUIFERS]\n'
                                f'Aquifer .45 .1 .25 {.2*25.4:.17g} 10 {15*length:.17g} .5 {5*length:.17g} {.001*25.4:.17g} {-10*length:.17g} {2*length:.17g} .3 ET\n'
                                '[GROUNDWATER]\n'
                                f'S Aquifer J {20*length:.17g} {.001*flux/length**1.2:.17g} 1.2 {.0001*flux/length**1.1:.17g} 1.1 {.00001*flux/length**2:.17g} 0 *\n'
                                '[GWF]\n'
                                f'S LATERAL {flux:.17g} * (0.002 * ((HGW / {length:.17g}) - (HCB / {length:.17g})) + 0.0001 * (HSW / {length:.17g}))\n'
                                f'S DEEP 25.4 * (0.001 * ((HGW / {length:.17g}) / (HGS / {length:.17g})))\n')
                        expected=self.observe(lib,base,hydrology.to_document().text+suffix,full=True)
                        actual=self.observe(lib,base,m.to_document().text,full=True)
                        self.assertEqual(actual,expected)
                        rows.append(dict(units=units,out_sha256=actual['out_sha256']))
                EVIDENCE.append(dict(kind='groundwater-field-conversion',family=family,cases=len(rows),rows=rows))

    def test_groundwater_queries_survive_relocated_checkpoint(self):
        from easysewer.runtime import RunResult
        import test_native_v2_runner_checkpoint as checkpoint
        helper=checkpoint.NativeRunnerCheckpointTests()
        with patch.dict(os.environ,EASYSEWER_CHECKPOINT_PACKAGED='1'):
            for family in ('standard','custom'):
                for mode in ('normal','saturated','without-gwf'):
                    with self.subTest(family=family,mode=mode),tempfile.TemporaryDirectory() as directory:
                        root=Path(directory);m=load(fixture(mode=mode));before=queries(m)
                        original,saved=helper.original(root,family,model=m);helper.success(original);self.assertTrue(saved)
                        self.assertEqual(queries(original.snapshot.model()),before)
                        original.save(root/'expected');expected=RunResult.load(root/'expected')
                        self.assertEqual(queries(expected.snapshot.model()),before)
                        shutil.rmtree(root/'first');(root/'original.hsf').unlink();(root/'saved').rename(root/'moved')
                        actual=checkpoint.runner(family).resume(root/'moved'/saved[0].directory.name,checkpoint.resume_config(root/'resumed'))
                        helper.equivalent(expected,actual);self.assertEqual(queries(actual.snapshot.model()),before)
                        EVIDENCE.append(dict(kind='groundwater-fields-checkpoint',family=family,mode=mode,queries=len(before),
                            out_sha256=actual.output.sha256,original_workspace_removed=True))


if __name__=='__main__': unittest.main()
