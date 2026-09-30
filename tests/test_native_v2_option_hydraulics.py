"""Hydraulic option effects with independent literals in both packaged engines."""
import hashlib,tempfile,unittest
from pathlib import Path
from easysewer import get_native_capabilities
from easysewer.io.output import OutputReader
from easysewer.model import Model,Ref
from test_option_effects_v2 import UNITS,load
from test_option_hydraulics_v2 import (hydraulic_fixture,numeric_cases,public_value,
                                     force_fixture,literal_force_source)
from test_native_v2_option_effects import observe,digest
from test_native_v2_regulator_fields import FAMILIES,library

EVIDENCE=[]


@unittest.skipUnless(get_native_capabilities()['swmm_solver'] and get_native_capabilities()['flexible_ponding'],
                     'Both packaged engines required')
class NativeHydraulicOptionTests(unittest.TestCase):
    def test_seven_numeric_options_have_distinct_actual_hydraulic_effects(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for family,name,symbol in FAMILIES:
                lib,_=library(name,symbol)
                for units in UNITS:
                    for field,keyword,values,kind in numeric_cases(units):
                        model=hydraulic_fixture(units);source=model.to_document().text;histories=[]
                        for value in values:
                            with self.subTest(family=family,units=units,field=field,value=value):
                                literal=source+'[OPTIONS]\n'+keyword+' '+format(value,'.17g')+'\n'
                                expected=observe(self,lib,root,literal)
                                model.update_options(**{field:public_value(value,kind)})
                                restored=Model.from_json_document(model.to_json_document(),strict=True)
                                self.assertEqual(observe(self,lib,root,restored.to_document(normalize=True).text),expected)
                                self.assertEqual(observe(self,lib,root,load(literal).to_document(normalize=True).text),expected)
                                histories.append(expected['series']['swmm:outflow'])
                                EVIDENCE.append(dict(kind='numeric-active',family=family,units=units,field=field,value=value,result=digest(expected)))
                        self.assertEqual(len(set(histories)),len(values),(family,units,field))
                        model.update_options(**{field:None})
                        plain=hydraulic_fixture(units);plain.update_options(**{field:None})
                        self.assertEqual(observe(self,lib,root,model.to_document().text),observe(self,lib,root,plain.to_document().text))

    def test_dynamic_wave_parameters_are_inactive_for_steady_and_kinematic_routing(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for family,name,symbol in FAMILIES:
                lib,_=library(name,symbol)
                for units in UNITS:
                    for routing in ('STEADY','KINWAVE'):
                        model=hydraulic_fixture(units);model.update_options(flow_routing=routing)
                        source=model.to_document().text;baseline=observe(self,lib,root,source)
                        for field,keyword,values,kind in numeric_cases(units):
                            if field=='min_slope':continue  # Slope affects all routing models.
                            with self.subTest(family=family,units=units,routing=routing,field=field):
                                literal=source+'[OPTIONS]\n'+keyword+' '+format(values[-1],'.17g')+'\n'
                                actual=observe(self,lib,root,literal)
                                self.assertEqual(actual['out'],baseline['out'])
                                self.assertEqual(actual['series'],baseline['series'])
                                self.assertEqual(observe(self,lib,root,load(literal).to_document(normalize=True).text),actual)
                                EVIDENCE.append(dict(kind='numeric-inactive',family=family,units=units,routing=routing,field=field,result=digest(actual)))

    def test_force_main_formulas_and_roughness_units_match_independent_literals(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for family,name,symbol in FAMILIES:
                lib,_=library(name,symbol)
                for units in UNITS:
                    results=[]
                    for equation in ('H-W','D-W'):
                        with self.subTest(family=family,units=units,equation=equation):
                            literal=literal_force_source(equation,units)
                            expected=observe(self,lib,root,literal)
                            model=force_fixture(equation,units)
                            self.assertEqual(observe(self,lib,root,model.to_document().text),expected)
                            restored=Model.from_json_document(model.to_json_document(),strict=True)
                            self.assertEqual(observe(self,lib,root,restored.to_document(normalize=True).text),expected)
                            with OutputReader(root/'model.out') as reader:
                                depth=reader.series(Ref(collection='swmm:links',key='P'),'swmm:depth').values
                            diameter=1 if units in UNITS[:3] else .3048
                            self.assertAlmostEqual(max(depth),diameter,delta=1e-6)
                            results.append(expected['series']['swmm:outflow'])
                            EVIDENCE.append(dict(kind='force-main',family=family,units=units,equation=equation,
                                                 max_pipe_depth=max(depth),result=digest(expected)))
                    self.assertNotEqual(*results)

    def test_offset_elevation_literals_and_conversion_preserve_hydraulics(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for family,name,symbol in FAMILIES:
                lib,_=library(name,symbol)
                for units in UNITS:
                    with self.subTest(family=family,units=units):
                        model=hydraulic_fixture(units);length=1 if units in UNITS[:3] else .3048
                        model.links.update('P',inlet_offset=.5*length,outlet_offset=.25*length)
                        source=model.to_document().text;baseline=observe(self,lib,root,source)
                        lines=source.splitlines();section=None
                        for i,line in enumerate(lines):
                            if line.startswith('['):section=line
                            elif section=='[CONDUITS]' and line.strip() and not line.startswith(';'):
                                tokens=line.split();link=model.links[tokens[0]]
                                tokens[5]=format(model.nodes[link.inlet.key].elevation+link.inlet_offset,'.17g')
                                tokens[6]=format(model.nodes[link.outlet.key].elevation+link.outlet_offset,'.17g')
                                lines[i]=' '.join(tokens)
                        literal='\n'.join(lines)+'\n[OPTIONS]\nLINK_OFFSETS ELEVATION\n'
                        expected=observe(self,lib,root,literal)
                        model.convert_link_offsets('ELEVATION')
                        restored=Model.from_json_document(model.to_json_document(),strict=True)
                        self.assertEqual(observe(self,lib,root,restored.to_document(normalize=True).text),expected)
                        # Saved results are float32. Bound coordinate roundoff by
                        # max(1e-7 absolute, 2**-22 relative); literal-vs-codec OUT is exact.
                        max_error=0
                        for key,values in baseline['series'].items():
                            self.assertEqual(len(values),len(expected['series'][key]))
                            for a,b in zip(values,expected['series'][key]):
                                self.assertAlmostEqual(a,b,delta=max(1e-7,abs(a)*2**-22))
                                max_error=max(max_error,abs(a-b))
                        EVIDENCE.append(dict(kind='offset-conversion',family=family,units=units,
                            max_system_absolute_error=max_error,result=digest(expected)))


if __name__=='__main__':unittest.main()
