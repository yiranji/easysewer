from dataclasses import replace
from datetime import timedelta
from pathlib import Path
import tempfile
import unittest

from easysewer import get_native_capabilities
from easysewer.io.inp import InpDocument
from easysewer.model import Model, Ref
from easysewer.model import controls as c
from easysewer.model.resources import Curve, CurvePoint, InlineTimeSeries, SeriesPoint
from test_controls_v2 import controlled, rebuild
from test_options_v2 import network
from test_regulators_v2 import regulator_model


@unittest.skipUnless(get_native_capabilities()["swmm_solver"], "Native solver unavailable")
class NativeControlTests(unittest.TestCase):
    def solve(self, directory, name, source, *, allow_error=False):
        from easysewer.runtime._solver_api import SWMMSolverAPI
        base=Path(directory)/name
        inp,rpt,out=(base.with_suffix(s) for s in ('.inp','.rpt','.out'))
        inp.write_text(source+'[REPORT]\nCONTROLS YES\n',encoding='utf-8')
        solver=SWMMSolverAPI()
        if solver.get_version()!=52004:
            self.skipTest('Requires fixed SWMM 5.2.4')
        started=False
        try:
            error=solver.open(str(inp),str(rpt),str(out))
            if not error:
                error=solver.start(1)
                started=not error
            if error:
                solver.close()
                if allow_error:
                    return error,rpt.read_text(errors='replace')
                self.fail(rpt.read_text(errors='replace'))
            link,node=solver.get_index(3,'P'),solver.get_index(2,'J')
            history,previous=[],0
            for _ in range(20000):
                error,elapsed=solver.step()
                self.assertEqual(error,0)
                if elapsed==0:
                    break
                history.append((previous,elapsed*86400,solver.get_value(407,link),solver.get_value(303,node),solver.get_value(410,link)))
                previous=elapsed*86400
            else:
                self.fail('Control fixture exceeded step bound')
            self.assertEqual(solver.end(),0)
            started=False
            return dict(history=history,balance=solver.get_mass_bal_err())
        finally:
            if started:
                solver.end()
            solver.close()

    def compare(self,directory,source):
        model=Model.from_document(InpDocument.from_text(source),strict=True)
        self.assertFalse(model.support.opaque_records)
        expected=self.solve(directory,'original',source)
        self.assertEqual(self.solve(directory,'rebuilt',rebuild(model).to_document().text),expected)
        return model,expected

    def test_all_node_link_and_simulation_attributes_preserve_native_process(self):
        with tempfile.TemporaryDirectory() as directory:
            for obj in ('NODE','LINK','CONDUIT','PUMP','ORIFICE','WEIR','OUTLET','SIMULATION'):
                model=network() if obj in ('NODE','LINK','CONDUIT','SIMULATION') else regulator_model({'PUMP':'PUMP2','ORIFICE':'SIDE','WEIR':'TRANSVERSE','OUTLET':'FUNCTIONAL/HEAD'}[obj])
                action='CONDUIT P STATUS = CLOSED' if obj in ('NODE','LINK','CONDUIT','SIMULATION') else f'{obj} P SETTING = .5'
                for attr in c.ATTRIBUTES[obj]:
                    with self.subTest(obj=obj,attr=attr):
                        token='ON' if attr=='STATUS' else '00:00:07' if attr in c.TIME_ATTRIBUTES else '01/01/2000' if attr=='DATE' else '01/02' if attr=='DAYOFYEAR' else '1'
                        left=f'{obj} {attr}' if obj=='SIMULATION' else f'{obj} {"J" if obj=="NODE" else "P"} {attr}'
                        source=model.to_document().text+f'[CONTROLS]\nRULE R\nIF {left} > {token}\nTHEN {action}\n'
                        self.compare(directory,source)

    def test_all_relations_boolean_or_precedence_and_same_priority_order(self):
        with tempfile.TemporaryDirectory() as directory:
            for relation in ('=','<>','<','<=','>','>='):
                source=network().to_document().text+f'[CONTROLS]\nRULE R\nIF NODE J DEPTH {relation} NODE O DEPTH\nTHEN CONDUIT P STATUS = CLOSED\nELSE CONDUIT P STATUS = OPEN\n'
                self.compare(directory,source)
            # True OR False AND False must be false with SWMM precedence.
            source='RULE R\nIF SIMULATION MONTH = 1\nOR SIMULATION MONTH = 2\nAND SIMULATION MONTH = 3\nTHEN CONDUIT P STATUS = CLOSED\nELSE CONDUIT P STATUS = OPEN\n'
            _,result=self.compare(directory,controlled(source).to_document().text)
            self.assertTrue(all(row[2]==1 for row in result['history']))
            first='RULE First\nIF SIMULATION TIME >= 0\nTHEN CONDUIT P STATUS = CLOSED\n'
            second='RULE Second\nIF SIMULATION TIME >= 0\nTHEN CONDUIT P STATUS = OPEN\n'
            for priority,expected in ((None,0),(-1,0),(0,0),(1,1)):
                model=controlled(first+second+('' if priority is None else f'PRIORITY {priority}\n'))
                result=self.solve(directory,'priority',model.to_document().text)
                self.assertTrue(all(row[2]==expected for row in result['history']))
            model=controlled(first+second)
            model.controls.move(('RULE','Second'),before=('RULE','First'))
            result=self.solve(directory,'reordered',model.to_document().text)
            self.assertTrue(all(row[2]==1 for row in result['history']))
            model=controlled(first+'AND CONDUIT P STATUS = OPEN\n')
            result=self.solve(directory,'same_rule_last',model.to_document().text)
            self.assertTrue(all(row[2]==1 for row in result['history']))

    def test_all_modulated_settings_and_pid_minutes_match_rebuild(self):
        with tempfile.TemporaryDirectory() as directory:
            for obj,kind in (('PUMP','PUMP2'),('ORIFICE','SIDE'),('WEIR','TRANSVERSE'),('OUTLET','FUNCTIONAL/HEAD')):
                for setting in ('.5','CURVE Control','TIMESERIES Settings','PID .1 1 .05'):
                    with self.subTest(obj=obj,setting=setting):
                        model=regulator_model(kind)
                        extra='[CURVES]\nControl CONTROL 0 .2 10 .8\n[TIMESERIES]\nSettings 0 .2 .08333333333333333 .8\n'
                        source=model.to_document().text+extra+f'[CONTROLS]\nRULE R\nIF NODE J DEPTH >= 1\nTHEN {obj} P SETTING = {setting}\nELSE {obj} P SETTING = .1\n'
                        _,result=self.compare(directory,source)
                        self.assertTrue(any(row[2]!=1 for row in result['history']))

    def test_every_math_function_and_native_power_minus_behavior(self):
        with tempfile.TemporaryDirectory() as directory:
            expressions=[f'{f}(.5)' for f in c.FUNCTIONS]+['2^3^2','-2^2','- 2^2','2^-2','(-2)^2','(2 + 3) / 2','sgn(0)','sqrt(-1)','log(-1)','1 / 0']
            for expression in expressions:
                with self.subTest(expression=expression):
                    source=network().to_document().text+f'[CONTROLS]\nEXPRESSION Value = {expression}\nRULE R\nIF Value > 0\nTHEN CONDUIT P STATUS = CLOSED\nELSE CONDUIT P STATUS = OPEN\n'
                    self.compare(directory,source)
            # Native pow clamps every nonpositive base to zero. Spaced unary
            # minus instead negates the positive power; SGN(0) is also zero.
            for expression,relation,value in (('-2^2','=',0),('- 2^2','=',-4),('sgn(0)','=',0),('2^3^2','=',512)):
                with self.subTest(oracle=expression):
                    model=controlled(f'EXPRESSION Value = {expression}\nRULE R\nIF Value {relation} {value}\nTHEN CONDUIT P STATUS = CLOSED\nELSE CONDUIT P STATUS = OPEN\n')
                    result=self.solve(directory,'power_oracle',model.to_document().text)
                    self.assertTrue(all(row[2]==0 for row in result['history']))

    def test_named_variables_expressions_rhs_variables_and_references(self):
        with tempfile.TemporaryDirectory() as directory:
            source='VARIABLE Adepth = NODE J DEPTH\nVARIABLE Bdepth = NODE O DEPTH\nEXPRESSION Delta = Adepth - Bdepth\nRULE R\nIF Delta > Bdepth\nTHEN CONDUIT P STATUS = CLOSED\nELSE CONDUIT P STATUS = OPEN\n'
            model,result=self.compare(directory,controlled(source).to_document().text)
            model.controls.rename(('VARIABLE','Adepth'),'UpstreamDepth')
            model.controls.rename(('EXPRESSION','Delta'),'Difference')
            model.controls.rename(('RULE','R'),'Control')
            self.assertEqual(self.solve(directory,'renamed_symbols',model.to_document().text),result)

    def test_rule_step_ast_roundtrip_preserves_exact_action_times(self):
        with tempfile.TemporaryDirectory() as directory:
            for seconds,expected in ((None,10),(0,10),(1,7),(20,20)):
                model=controlled('RULE R\nIF SIMULATION TIME >= 00:00:07\nTHEN CONDUIT P STATUS = CLOSED\nELSE CONDUIT P STATUS = OPEN\n')
                model.update_options(rule_step=None if seconds is None else timedelta(seconds=seconds))
                _,result=self.compare(directory,rebuild(model).to_document().text)
                first=next(row[0] for row in result['history'] if row[2]==0)
                self.assertAlmostEqual(first,expected,places=7)

    def test_network_reordering_rewrites_unchanged_control_program_after_pump_declaration(self):
        with tempfile.TemporaryDirectory() as directory:
            model=controlled('RULE R\nIF SIMULATION TIME >= 00:00:07\nTHEN PUMP P SETTING = .5\n',model=regulator_model('PUMP2'))
            expected=self.solve(directory,'before',model.to_document().text)
            # Rename an unused-in-controls node: only network identities change.
            model.nodes.rename('O','Outfall')
            output=model.to_document()
            self.assertLess(output.records('PUMPS')[0].number,output.records('CONTROLS')[0].number)
            self.assertEqual(self.solve(directory,'after',output.text),expected)

    def test_short_circuit_changes_curve_controller_and_native_time_equality_keeps_state(self):
        with tempfile.TemporaryDirectory() as directory:
            model=regulator_model('SIDE')
            source=model.to_document().text+'[CURVES]\nControl CONTROL 0 0 8 1\n[CONTROLS]\nRULE R\nIF NODE J MAXDEPTH = 8\nOR NODE J DEPTH > 0\nTHEN ORIFICE P SETTING = CURVE Control\n'
            parsed,result=self.compare(directory,source)
            self.assertTrue(all(row[2]==1 for row in result['history'])) # OR clause was skipped; MAXDEPTH controls.
            self.assertIn('control.short_circuit_controller',{d.code for d in parsed.validate(for_run=True).diagnostics})
            source=model.to_document().text+'[CURVES]\nControl CONTROL 0 0 8 1\n[CONTROLS]\nRULE Seed\nIF NODE J MAXDEPTH = 8\nTHEN ORIFICE P SETTING = 0\nPRIORITY -1\nRULE Time\nIF SIMULATION TIME = 0\nTHEN ORIFICE P SETTING = CURVE Control\n'
            parsed,result=self.compare(directory,source)
            self.assertEqual(result['history'][0][2],1) # Time equality retained MAXDEPTH from Seed.
            self.assertTrue(all(row[2]==0 for row in result['history'][1:]))
            self.assertIn('control.native_time_controller',{d.code for d in parsed.validate(for_run=True).diagnostics})

    def test_pump_status_and_series_endpoint_extension(self):
        with tempfile.TemporaryDirectory() as directory:
            for status,expected in (('ON',1),('OFF',0)):
                source=regulator_model('PUMP2').to_document().text+f'[CONTROLS]\nRULE R\nIF SIMULATION TIME >= 0\nTHEN PUMP P STATUS = {status}\n'
                _,result=self.compare(directory,source)
                self.assertTrue(all(row[2]==expected for row in result['history']))
            source=regulator_model('SIDE').to_document().text+'[TIMESERIES]\nSettings .01 .2 .02 .8\n[CONTROLS]\nRULE R\nIF SIMULATION TIME >= 0\nTHEN ORIFICE P SETTING = TIMESERIES Settings\n'
            _,result=self.compare(directory,source)
            self.assertTrue(all(row[2]==.2 for row in result['history'] if row[0]<36))
            self.assertTrue(all(row[2]==.8 for row in result['history'] if row[0]>72))

    def test_controls_before_pump_declaration_require_explicit_normalization(self):
        with tempfile.TemporaryDirectory() as directory:
            program='[CONTROLS]\nRULE R\nIF SIMULATION TIME >= 0\nTHEN PUMP P SETTING = .5\n'
            source=program+regulator_model('PUMP2').to_document().text
            model=Model.from_document(InpDocument.from_text(source),strict=True)
            self.assertIn('control.action_declaration_order',{d.code for d in model.validate(for_run=True).errors})
            error,_=self.solve(directory,'bad_order',source,allow_error=True)
            self.assertNotEqual(error,0)
            self.assertTrue(model.validate(for_run=True,normalize=True).is_valid)
            result=self.solve(directory,'normalized',model.to_document(normalize=True).text)
            self.assertTrue(all(row[2]==.5 for row in result['history']))

    def test_six_units_preserve_typed_control_settings_and_hydraulic_history(self):
        with tempfile.TemporaryDirectory() as directory:
            base=regulator_model('SIDE',storage=False)
            source=base.to_document().text+'[CURVES]\nControl CONTROL 0 .1 8 .9\n[CONTROLS]\nVARIABLE DepthValue = NODE J DEPTH\nEXPRESSION MeanDepth = (DepthValue + DepthValue)/2\nRULE R\nIF MeanDepth >= 0\nTHEN ORIFICE P SETTING = CURVE Control\n'
            model=Model.from_document(InpDocument.from_text(source),strict=True)
            expected=self.solve(directory,'CFS',rebuild(model).to_document().text)
            for unit,flow_factor in (('GPM',448.831),('MGD',.64632),('CMS',.02832),('LPS',28.317),('MLD',2.4466)):
                converted=model.copy()
                converted.convert_units(unit)
                actual=self.solve(directory,unit,converted.to_document().text)
                self.assertEqual(len(actual['history']),len(expected['history']))
                for a,b in zip(actual['history'],expected['history']):
                    self.assertEqual(a[:2],b[:2])
                    self.assertAlmostEqual(a[2],b[2],delta=1e-9)
                    self.assertAlmostEqual(a[3]/(1 if unit in ('GPM','MGD') else .3048),b[3],delta=1e-9)
                    self.assertAlmostEqual(a[4]/flow_factor,b[4],delta=1e-9)

    def test_all_48_rain_windows_through_named_expressions_and_direct_collision(self):
        from test_hydrology_v2 import hydrology_model
        with tempfile.TemporaryDirectory() as directory:
            model=hydrology_model()
            for hours in range(1,49):
                with self.subTest(hours=hours):
                    source=model.to_document().text+f'[CONTROLS]\nVARIABLE RainDepth = GAGE R {hours}-HR_DEPTH\nEXPRESSION RainValue = RainDepth\nRULE R\nIF RainValue >= .1\nTHEN OUTLET P SETTING = .5\nELSE OUTLET P SETTING = 1\n'
                    self.compare(directory,source)
            source=model.to_document().text+'[CONTROLS]\nRULE R\nIF GAGE R 8-HR_DEPTH > .1\nTHEN OUTLET P SETTING = .5\n'
            error,report=self.solve(directory,'collision',source,allow_error=True)
            self.assertNotEqual(error,0)
            self.assertIn('ERROR 205',report)


if __name__=='__main__':
    unittest.main()
