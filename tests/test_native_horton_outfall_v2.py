"""Current Horton-state coverage over the frozen six-unit project matrix.

Historical before/after comparisons have been replaced by deterministic current
runs, physical unit equivalence, and analytical degenerate/capacity limits. The
fixtures exercise hydrology; advertising outfall-gate capability does not itself
qualify flap-gate hydraulics. Revision rejection uses explicitly synthetic
metadata and makes no claim about preserved old native artifacts.
"""
from pathlib import Path
import unittest

from test_native_horton_integration_v2 import HortonAssertions, routed_model

EVIDENCE=[]


def model(units, case):
    text=(Path(__file__).resolve().parent/'fixtures/horton-outfall'/(units+'-'+case+'.inp')).read_text(encoding='ascii')
    return routed_model(text)


class HortonIntegrationTests(HortonAssertions,unittest.TestCase):
    def test_fourteen_hydrology_cases_both_families_and_units(self):
        self.check_matrix(model,('CFS','GPM','MGD','CMS','LPS','MLD'),
            ('horton','green-ampt','modified-green-ampt','curve-number','unlimited',
             'constant-rate','zero-decay','finite-cap','small-cap','zero-decay-small-cap',
             'zero-decay-unlimited','ordinary-constant','ordinary-zero-decay','zero-rates'),EVIDENCE)

    def test_finite_capacity_fresh_parent_resume_and_revision_boundaries(self):
        self.check_continuation(model,('CFS','GPM','MGD','CMS','LPS','MLD'),
            'zero-decay-small-cap',EVIDENCE)


if __name__=='__main__':
    unittest.main()
