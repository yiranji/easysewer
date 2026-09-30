"""The 2.0 package exposes no 1.x API or data-conversion package."""
from importlib.util import find_spec
import unittest
import easysewer
from easysewer.model import Model


class PublicApiTests(unittest.TestCase):
    def test_model_entry_and_retired_exports(self):
        self.assertIs(easysewer.Model,Model)
        self.assertEqual(Model().to_document().text,'')
        for name in ('LegacyModel','UrbanDrainageModel','JsonHandler','ControlList','ControlRule',
                     'SWMMSolverAPI','FlexiblePondingSolverAPI','SWMMOutputAPI'):
            with self.subTest(name=name):
                self.assertNotIn(name,easysewer.__all__)
                with self.assertRaises(AttributeError):getattr(easysewer,name)

    def test_old_modules_are_absent(self):
        for name in ('ModelAPI','UDM','Area','Control','Curve','JsonHandler','Link','Node',
                     'Options','Rain','SolverAPI','OutputAPI','compat'):
            with self.subTest(name=name):
                self.assertIsNone(find_spec('easysewer.'+name))


if __name__=='__main__':unittest.main()
