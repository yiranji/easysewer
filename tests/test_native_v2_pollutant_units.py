"""Concentration-unit scenarios compared against independent native inputs."""

from pathlib import Path
import tempfile
import unittest

from easysewer import get_native_capabilities
from easysewer.io.inp import InpDocument
from easysewer.model import Model
from easysewer.scenario import ChangePollutantUnits, ScenarioPatch
from test_quality_v2 import ref
from test_scenario_v2 import portable
import test_native_v2_quality as native_quality


@unittest.skipUnless(get_native_capabilities()['swmm_solver'] and get_native_capabilities()['swmm_output'], 'Native solver/output unavailable')
class NativePollutantUnitTests(unittest.TestCase):
    def solve(self, root, name, source):
        return native_quality.NativeQualityTests.solve(self, root, name, source)

    def test_conversion_scenario_all_active_washoff_us_si_and_both_directions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for units in ('CFS', 'CMS'):
                for wash in ('EXP', 'RC', 'EMC'):
                    with self.subTest(units=units, wash=wash):
                        source = native_quality.literal_quality('POW', wash, units=units)
                        source += ('[TIMESERIES]\nConc 0 2\nConc 8 3\n[INFLOWS]\n'
                            'J FLOW "" FLOW 1 1 .1\nJ Q0 Conc CONCEN 1 1 .2\nO Q0 Conc CONCEN 1 1 .3\n'
                            'O Q1 "" MASS 126 1 .01\n[DWF]\nJ FLOW .1\nJ Q0 3\nJ Q1 4\n')
                        model = Model.from_document(InpDocument.from_text(source), strict=True)
                        before = model.to_json_document().to_bytes()
                        operations = (ChangePollutantUnits(target=ref('pollutants','Q0'), units='UG/L'),
                            ChangePollutantUnits(target=ref('pollutants','Q1'), units='MG/L'))
                        patch = ScenarioPatch.from_json_document(ScenarioPatch(operations=operations).to_json_document())
                        result = patch.apply(model)
                        self.assertEqual(before, model.to_json_document().to_bytes())
                        self.assertEqual(result.model.timeseries['Conc'].points[0].value, 2000)
                        oracle = source.replace('Q0 MG/L 2 3 4 -.01 NO * 0 1 .5', 'Q0 UG/L 2000 3000 4000 -.01 NO * 0 1000 500')
                        oracle = oracle.replace('Q1 UG/L 2 3 4 -.01 NO * 0 1 .5', 'Q1 MG/L .002 .003 .004 -.01 NO * 0 .001 .0005')
                        oracle = oracle.replace('Conc 0 2', 'Conc 0 2000').replace('Conc 8 3', 'Conc 8 3000')
                        oracle = oracle.replace('CONCEN 1 1 .2', 'CONCEN 1 1 200').replace('CONCEN 1 1 .3', 'CONCEN 1 1 300')
                        oracle = oracle.replace('MASS 126 1 .01', 'MASS .126 1 .01').replace('J Q0 3', 'J Q0 3000').replace('J Q1 4', 'J Q1 .004')
                        if wash == 'RC':
                            oracle = oracle.replace('Land Q0 RC 2', 'Land Q0 RC 2000').replace('Land Q1 RC 2', 'Land Q1 RC .002')
                        if wash == 'EMC':
                            oracle = oracle.replace('Land Q0 EMC 3', 'Land Q0 EMC 3000').replace('Land Q1 EMC 3', 'Land Q1 EMC .003')
                        actual = self.solve(root, 'converted', portable(result.model).to_document().text)
                        self.assertEqual(actual, self.solve(root, 'literal', oracle))
                        original = self.solve(root, 'original', source)
                        self.assertTrue(original['quality'] != actual['quality'])
                        back = ScenarioPatch(operations=(ChangePollutantUnits(target=ref('pollutants','Q0'), units='MG/L'),
                            ChangePollutantUnits(target=ref('pollutants','Q1'), units='UG/L'))).apply(result.model).model
                        self.assertEqual(original, self.solve(root, 'returned', portable(back).to_document().text))

    def test_reinterpretation_only_changes_label_and_matches_native_input(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = native_quality.literal_quality('POW', 'EMC')
            original = self.solve(root, 'original', source)
            for units in ('UG/L', '#/L'):
                model = Model.from_document(InpDocument.from_text(source), strict=True)
                patch = ScenarioPatch(operations=(ChangePollutantUnits(target=ref('pollutants','Q0'), units=units, mode='reinterpret'),))
                result = ScenarioPatch.from_json_document(patch.to_json_document()).apply(model)
                self.assertEqual(model.pollutants['Q0'].rainfall_concentration, result.model.pollutants['Q0'].rainfall_concentration)
                actual = self.solve(root, 'changed', portable(result.model).to_document().text)
                self.assertEqual(actual, self.solve(root, 'literal', source.replace('Q0 MG/L', 'Q0 '+units)))
                self.assertTrue(original['report'] != actual['report'])


if __name__ == '__main__':
    unittest.main()
