import math
from pathlib import Path
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
from qualify_circular_rating import circular_state,maximum_rating


class CircularRatingTests(unittest.TestCase):
    def test_half_and_full_circle_have_known_radius_and_area(self):
        for fraction in (.5,1.):
            state=circular_state(fraction)
            self.assertAlmostEqual(state['area_ft2'],fraction*math.pi*1.5**2)
            self.assertAlmostEqual(state['hydraulic_radius_ft'],.75)
            self.assertAlmostEqual(state['depth_ft'],3*fraction)
        self.assertAlmostEqual(circular_state(.5)['flow_cfs'],circular_state(1.)['flow_cfs']/2)

    def test_empty_circle_and_tiny_positive_area_are_distinct(self):
        self.assertEqual(circular_state(0.)['flow_cfs'],0.)
        tiny=circular_state(1e-16)
        self.assertGreater(tiny['flow_cfs'],0.)
        self.assertTrue(math.isfinite(tiny['hydraulic_radius_ft']))

    def test_maximum_is_inside_pipe_and_exceeds_full_flow(self):
        state=maximum_rating();f=state['area_fraction']
        self.assertTrue(.95<f<1.)
        self.assertGreater(state['flow_ratio_to_full'],1.07)
        for neighbour in (f-.001,f+.001):
            self.assertLess(circular_state(neighbour)['flow_cfs'],state['flow_cfs'])

    def test_diameter_slope_and_roughness_scaling(self):
        value=circular_state(.8)['flow_cfs']
        self.assertAlmostEqual(circular_state(.8,diameter=6.)['flow_cfs']/value,2**(8/3))
        self.assertAlmostEqual(circular_state(.8,slope=.008)['flow_cfs']/value,2.)
        self.assertAlmostEqual(circular_state(.8,roughness=.026)['flow_cfs']/value,.5)

    def test_invalid_geometry_is_rejected(self):
        for f in (-1.,1.01,float('nan'),float('inf')):
            with self.assertRaises(ValueError):circular_state(f)
        for name in ('diameter','slope','roughness'):
            with self.assertRaises(ValueError):circular_state(.5,**{name:0.})


if __name__=='__main__':unittest.main()
