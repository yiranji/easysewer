"""Native treatment oracles: C/R semantics, names, units and state files."""

from dataclasses import replace
from datetime import timedelta
from itertools import product
from pathlib import Path
import tempfile
import unittest

from easysewer import get_native_capabilities
from easysewer.io.inp import InpDocument
from easysewer.io.hotstart import HotstartData, HotstartLayout
from easysewer.io.runoff_cache import RunoffData, RunoffLayout
from easysewer.model import Model
from easysewer.model.expressions import FUNCTIONS
from easysewer.model.network import Junction, Divider, OverflowDivider
from easysewer.scenario import ScenarioPatch, ChangePollutantUnits
from easysewer.validation import ValidationError
from test_quality_v2 import ref
from test_scenario_v2 import portable
from test_treatment_v2 import expression
import test_native_v2_quality as native_quality
import test_native_v2_project as native_project


def literal_base(kind='STORAGE', units='CFS'):
    model = Model.from_document(InpDocument.from_text(native_quality.literal_quality('NONE','EMC',units=units)), strict=True)
    node = model.nodes['J']
    if kind == 'JUNCTION':
        model.nodes.replace('J', Junction(id='J', elevation=node.elevation, max_depth=node.max_depth))
    elif kind == 'DIVIDER':
        model.nodes.replace('J', Divider(id='J', elevation=node.elevation, max_depth=node.max_depth,
            diverted_link=ref('links','P'), law=OverflowDivider()))
    return model.to_document().text+('[INFLOWS]\nJ FLOW "" FLOW 1 1 .1\nJ Q0 "" CONCEN 1 1 5\nJ Q1 "" CONCEN 1 1 10\n')


def no_inflow_source():
    model = Model.from_document(InpDocument.from_text(native_quality.literal_quality('NONE','NONE')), strict=True)
    series = model.timeseries['Rain']
    model.timeseries.update('Rain', points=tuple(replace(point,value=0) for point in series.points))
    model.nodes.update('J', initial_depth=1)
    for id in model.pollutants:
        model.pollutants.update(id, initial_concentration=10, decay_rate=0)
    return model.to_document().text


@unittest.skipUnless(get_native_capabilities()['swmm_solver'] and get_native_capabilities()['swmm_output'], 'Native solver/output unavailable')
class NativeTreatmentTests(unittest.TestCase):
    def solve(self, root, name, source):
        return native_quality.NativeQualityTests.solve(self, root, name, source)

    def test_four_node_kinds_both_units_and_all_C_R_pairings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for kind,units,first,second in product(('STORAGE','JUNCTION','DIVIDER','OUTFALL'),('CFS','CMS'),('C','R'),('C','R')):
                with self.subTest(node=kind,units=units,first=first,second=second):
                    node = 'O' if kind == 'OUTFALL' else 'J'
                    q0 = '.8*Q0' if first == 'C' else '.25'
                    q1 = '.1*Q0+.5*Q1+.1*R_Q0' if second == 'C' else '.02*Q0+.2*R_Q0'
                    source = literal_base(kind,units)+f'[TREATMENT]\n{node} Q0 {first}={q0}\n{node} Q1 {second} = {q1}\n'
                    model = Model.from_document(InpDocument.from_text(source),strict=True)
                    self.assertTrue(model.validate(for_run=True).is_valid)
                    expected = self.solve(root,'literal',source)
                    actual = self.solve(root,'rebuilt',portable(model).to_document().text)
                    self.assertEqual(actual,expected)
                    self.assertGreater(max(actual['quality'][1][0][0]),0)

    def test_process_variables_all_functions_signed_arithmetic_and_aliases(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); base = literal_base()
            formulas = [f'.1+.01*{name}(.5)' for name in FUNCTIONS]
            formulas += ['.1+.000001*'+name for name in ('HRT','DT','FLOW','DEPTH','AREA')]
            formulas += ['.1+.01*(-2^2)',' .1+.00001*(2^3^2)', '.1+.01*(-.5^2)', '.1+.01*SGN(0)']
            for text in formulas:
                source = base+'[TREATMENT]\nJ Q0 R='+text+'\n'
                model = Model.from_document(InpDocument.from_text(source),strict=True)
                self.assertEqual(self.solve(root,'literal',source),self.solve(root,'rebuilt',portable(model).to_document().text),text)
            self.assertEqual(self.solve(root,'sign',base+'[TREATMENT]\nJ Q0 R=.1+.01*SGN(0)\n'),
                self.solve(root,'sign-zero',base+'[TREATMENT]\nJ Q0 R=.1\n'))
            aliases = ('J Q0 RemOval ignored = .25\n', 'J Q0 cResult = .5*Q0+0*DTsuffix\n')
            for row in aliases:
                source = base+'[TREATMENT]\n'+row
                model = Model.from_document(InpDocument.from_text(source),strict=True)
                self.assertEqual(self.solve(root,'alias',source),self.solve(root,'canonical',portable(model).to_document().text))

    def test_name_lookup_process_prefix_and_actual_R_prefixed_pollutant(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for source in (
                literal_base().replace('Q0','FLOWER')+'[TREATMENT]\nJ FLOWER C=.5*FLOWER\n',
                literal_base().replace('Q1','R_Q0')+'[TREATMENT]\nJ Q0 R=.2\nJ R_Q0 C=.5*R_Q0\n',
            ):
                model = Model.from_document(InpDocument.from_text(source),strict=True)
                self.assertEqual(self.solve(root,'literal',source),self.solve(root,'rebuilt',portable(model).to_document().text))

    def test_edits_removal_dependencies_and_clamps_change_real_quality(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); base = literal_base()
            source = base+'[TREATMENT]\nJ Q0 R=.25\nJ Q1 R=.2*R_Q0\n'
            original = self.solve(root,'original',source)
            model = Model.from_document(InpDocument.from_text(source),strict=True)
            model.treatment.update(('J','Q0'),expression=expression('.5'))
            actual = self.solve(root,'edited',portable(model).to_document().text)
            self.assertEqual(actual,self.solve(root,'oracle',source.replace('R=.25','R=.5')))
            self.assertTrue(original['quality']!=actual['quality'])
            self.assertTrue(original['quality'][1][0][1]!=actual['quality'][1][0][1])
            for lhs,rhs in (('R=-1','R=0'),('R=2','R=1'),('C=-1','C=0'),('C=1e8','C=Q0')):
                self.assertEqual(self.solve(root,'clamped',base+'[TREATMENT]\nJ Q0 '+lhs+'\n'),
                    self.solve(root,'bound',base+'[TREATMENT]\nJ Q0 '+rhs+'\n'))

    def test_zero_removal_is_not_absence_and_missing_removal_is_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); base = literal_base().replace('2 3 4 -.01','2 3 4 .8')
            model = Model.from_document(InpDocument.from_text(base),strict=True)
            self.assertEqual(model.pollutants['Q0'].decay_rate,.8)
            none = self.solve(root,'absent',base)
            zero = self.solve(root,'zero',base+'[TREATMENT]\nJ Q0 R=0\n')
            self.assertTrue(none['quality']!=zero['quality'])
            for row in ('C=Q0','R=R_Q1'):
                source = base+'[TREATMENT]\nJ Q0 '+row+'\n'
                parsed = Model.from_document(InpDocument.from_text(source),strict=True)
                self.assertEqual(zero,self.solve(root,'equivalent',portable(parsed).to_document().text))

    def test_no_inflow_C_R_difference_and_native_pollutant_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); base = no_inflow_source()
            removal = self.solve(root,'no-flow-R',base+'[TREATMENT]\nJ Q0 R=.5\n')
            concentration = self.solve(root,'no-flow-C',base+'[TREATMENT]\nJ Q0 C=.5*Q0\n')
            self.assertTrue(removal['quality'][1][0][0]!=concentration['quality'][1][0][0])
            source = base+'[TREATMENT]\nJ Q0 R=.5\nJ Q1 C=Q1*(1-R_Q0)\n'
            original = self.solve(root,'original-order',source)
            model = Model.from_document(InpDocument.from_text(source),strict=True)
            model.pollutants.move('Q1',before='Q0')
            moved = self.solve(root,'moved-order',portable(model).to_document().text)
            lines = InpDocument.from_text(source).records('POLLUTANTS')
            oracle = source.replace(lines[0].raw+lines[1].raw,lines[1].raw+lines[0].raw)
            self.assertEqual(moved,self.solve(root,'literal-order',oracle))
            self.assertTrue(original['quality'][1][0][1]!=moved['quality'][1][0][0])

    def test_concentration_leaf_reads_referenced_pollutant_C_R_context(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); base = no_inflow_source()
            c_source = base+'[TREATMENT]\nJ Q0 C=Q0\nJ Q1 C=.5*Q0\n'
            r_source = base+'[TREATMENT]\nJ Q0 R=0\nJ Q1 C=.5*Q0\n'
            c = self.solve(root,'context-C',c_source)
            r = self.solve(root,'context-R',r_source)
            self.assertTrue(c['quality'][1][0][1]!=r['quality'][1][0][1])
            for source in (c_source,r_source):
                model = Model.from_document(InpDocument.from_text(source),strict=True)
                self.assertEqual(self.solve(root,'literal',source),self.solve(root,'rebuilt',portable(model).to_document().text))

    def test_hydraulic_unit_substitution_matches_independent_formula(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); base = literal_base()
            formula = '.1+.001*FLOW+.001*DEPTH+.000001*AREA+.0001*DT+.001*HRT'
            for units,flow,length in (('GPM',448.831,1.),('MGD',.64632,1.),('CMS',.02832,.3048),('LPS',28.317,.3048),('MLD',2.4466,.3048)):
                model = Model.from_document(InpDocument.from_text(base),strict=True)
                model.convert_units(units)
                converted_base = portable(model).to_document().text
                source = base+'[TREATMENT]\nJ Q0 R='+formula+'\n'
                model = Model.from_document(InpDocument.from_text(source),strict=True); model.convert_units(units)
                literal = f'.1+.001*(FLOW/{flow!r})+.001*(DEPTH/{length!r})+.000001*(AREA/{length**2!r})+.0001*DT+.001*HRT'
                actual = self.solve(root,'converted',portable(model).to_document().text)
                self.assertEqual(actual,self.solve(root,'oracle',converted_base+'[TREATMENT]\nJ Q0 R='+literal+'\n'))

    def test_pollutant_unit_conversion_transforms_C_output_and_all_concentration_leaves(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for kind in ('C','R'):
                base = literal_base()
                formula = '.2*Q0+.01*Q1' if kind=='C' else '.01*Q0+.001*Q1'
                source = base+f'[TREATMENT]\nJ Q0 {kind}={formula}\nJ Q1 C=.3*Q1+.01*Q0+.1*R_Q0\n'
                model = Model.from_document(InpDocument.from_text(source),strict=True)
                operations = (ChangePollutantUnits(target=ref('pollutants','Q0'),units='UG/L'),ChangePollutantUnits(target=ref('pollutants','Q1'),units='MG/L'))
                result = ScenarioPatch.from_json_document(ScenarioPatch(operations=operations).to_json_document()).apply(model).model
                converted_base = Model.from_document(InpDocument.from_text(base),strict=True)
                for operation in operations: converted_base.convert_pollutant_units(operation.target.key,operation.units)
                oracle_first = formula.replace('Q0','(Q0/1000)').replace('Q1','(Q1/.001)')
                if kind=='C': oracle_first = '1000*('+oracle_first+')'
                oracle = converted_base.to_document().text+f'[TREATMENT]\nJ Q0 {kind}={oracle_first}\nJ Q1 C=.001*(.3*(Q1/.001)+.01*(Q0/1000)+.1*R_Q0)\n'
                self.assertEqual(self.solve(root,'converted',portable(result).to_document().text),self.solve(root,'oracle',oracle))

    def test_effective_repeated_assignments_and_model_cycle_diagnostic(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); base = literal_base()
            repeated = base+'[TREATMENT]\nJ Q0 R=R_Q1\nJ Q1 R=R_Q0\nJ Q0 R=.2\n'
            model = Model.from_document(InpDocument.from_text(repeated),strict=True)
            self.assertEqual(self.solve(root,'repeated',repeated),self.solve(root,'effective',portable(model).to_document().text))
            cyclic = base+'[TREATMENT]\nJ Q0 R=R_Q1\nJ Q1 R=R_Q0\n'
            result = native_project.NativeProjectTests.solve(self,root,'cycle',cyclic,allow_error=True,report_encoding='cp1252')
            self.assertNotIn('error',result)
            model = Model.from_document(InpDocument.from_text(cyclic))
            self.assertIn('treatment.removal_cycle',{d.code for d in model.validate().errors})
            with self.assertRaises(ValidationError): model.to_document()

    def test_treatment_quality_state_and_runoff_cache_native_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = literal_base()+'[TREATMENT]\nJ Q0 C=.5*Q0\nJ Q1 R=.2*R_Q0\n'
            model = Model.from_document(InpDocument.from_text(source),strict=True)
            for kind,Data,Layout in (('HOTSTART',HotstartData,HotstartLayout),('RUNOFF',RunoffData,RunoffLayout)):
                original,copy = root/(kind+'.bin'),root/(kind+'-copy.bin')
                self.solve(root,'producer',source+f'[FILES]\nSAVE {kind} "{original}"\n')
                data = Data.from_bytes(original.read_bytes(),layout=Layout.from_model(model))
                self.assertEqual(data.to_bytes(),original.read_bytes()); data.write(copy)
                a = self.solve(root,'original',source+f'[FILES]\nUSE {kind} "{original}"\n')
                b = self.solve(root,'copy',source+f'[FILES]\nUSE {kind} "{copy}"\n')
                if kind=='RUNOFF': b['report'] = b['report'].replace(str(copy),str(original))
                self.assertEqual(a,b)


if __name__ == '__main__':
    unittest.main()
