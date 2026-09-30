"""Native behavior after a JSON reconstruction without copying source INP."""

from datetime import timedelta
import tempfile
import unittest

from easysewer import get_native_capabilities
from easysewer.io.inp import InpDocument
from easysewer.model import Model, Ref
from easysewer.model import climate as cl
from easysewer.model.resources import InlineTimeSeries, SeriesPoint
from test_controls_v2 import controlled
from test_hydrology_v2 import hydrology_model, snowpack, INFILTRATION
from test_json_v2 import restore
from test_native_v2_network import CASES, NETWORK, RESOURCES, SETTINGS
from test_regulators_v2 import regulator_model, KINDS
import test_native_v2_network as native_network
import test_native_v2_controls as native_controls
import test_native_v2_hydrology as native_hydrology


@unittest.skipUnless(get_native_capabilities()['swmm_solver'] and get_native_capabilities()['swmm_output'],'Native APIs unavailable')
class NativeJsonTests(unittest.TestCase):
    def test_all_cross_sections_and_link_families_keep_complete_native_histories(self):
        with tempfile.TemporaryDirectory() as directory:
            solver=native_network.NativeNetworkTests()
            for kind,parameters in CASES.items():
                with self.subTest(shape=kind):
                    source=SETTINGS+NETWORK.format(shape=kind+' '+parameters)+RESOURCES.get(kind,'')
                    model=Model.from_document(InpDocument.from_text(source),strict=True)
                    expected=solver.solve(directory,'original',source)
                    actual=solver.solve(directory,'json',restore(model,source=False).to_document().text)
                    self.assertEqual(actual,expected)
            controller=native_controls.NativeControlTests()
            for kind in KINDS:
                with self.subTest(link=kind):
                    model=regulator_model(kind)
                    expected=controller.solve(directory,'original',model.to_document().text)
                    actual=controller.solve(directory,'json',restore(model,source=False).to_document().text)
                    self.assertEqual(actual,expected)

    def test_control_action_modes_expression_references_and_rule_step_survive_json(self):
        with tempfile.TemporaryDirectory() as directory:
            solver=native_controls.NativeControlTests()
            for setting in ('.5','CURVE Control','TIMESERIES Settings','PID .1 1 .05'):
                model=regulator_model('SIDE',storage=False)
                source=model.to_document().text+'[CURVES]\nControl CONTROL 0 .1 8 .9\n[TIMESERIES]\nSettings 0 .2 .1 .8\n[CONTROLS]\nVARIABLE DepthValue = NODE J DEPTH\nEXPRESSION MeanDepth = ABS(DepthValue + DepthValue)/2\nRULE R\nIF MeanDepth >= 1\nTHEN ORIFICE P SETTING = '+setting+'\nELSE ORIFICE P SETTING = .1\n'
                model=Model.from_document(InpDocument.from_text(source),strict=True)
                expected=solver.solve(directory,'original',source)
                actual=solver.solve(directory,'json',restore(model,source=False).to_document().text)
                self.assertEqual(actual,expected)
                self.assertTrue(any(row[2]!=1 for row in actual['history']))
            for seconds,expected_time in ((None,10),(0,10),(1,7),(20,20)):
                model=controlled('RULE R\nIF SIMULATION TIME >= 00:00:07\nTHEN CONDUIT P STATUS = CLOSED\nELSE CONDUIT P STATUS = OPEN\n')
                model.update_options(rule_step=None if seconds is None else timedelta(seconds=seconds))
                result=solver.solve(directory,'schedule',restore(model,source=False).to_document().text)
                self.assertAlmostEqual(next(row[0] for row in result['history'] if row[2]==0),expected_time,places=7)

    def test_combined_climate_snow_runoff_all_methods_and_units_keep_out_series(self):
        solver=native_hydrology.NativeHydrologyTests()
        with tempfile.TemporaryDirectory() as directory:
            for method in INFILTRATION:
                base=hydrology_model(method)
                base.snowpacks.add(snowpack())
                base.subcatchments.update('S',snowpack=Ref(collection='swmm:snowpacks',key='Snow'))
                base.timeseries.add(InlineTimeSeries(id='Air',points=tuple(SeriesPoint(time=timedelta(hours=h),value=t) for h,t in ((0,25),(24,25),(48,45),(72,45)))))
                base.update_climate(temperature=cl.SeriesTemperature(series=Ref(collection='swmm:timeseries',key='Air')),wind=cl.MonthlyWindSpeeds(values=(8.,)*12))
                for units in ('CFS','GPM','MGD','CMS','LPS','MLD'):
                    with self.subTest(method=method,units=units):
                        model=base.copy()
                        model.convert_units(units)
                        expected=solver.solve(directory,'original',model.to_document().text)
                        restored=restore(model,source=False)
                        actual=solver.solve(directory,'json',restored.to_document().text)
                        self.assertEqual(actual,expected)
                        self.assertGreater(max(actual['runoff']),0)
                        self.assertGreater(max(actual['snow']),0)


if __name__=='__main__':
    unittest.main()
