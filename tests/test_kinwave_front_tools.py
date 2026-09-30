"""Independent counterexamples for the hydraulic qualification checks."""
import csv
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
from assess_kinwave_front import assess
from qualify_kinwave_front import reference


class KinematicQualificationTests(unittest.TestCase):
    def sample(self,directory,*,early=False,pulse=False):
        flow=.3;tau=reference(flow)['arrival_seconds']
        arrival=tau/2 if early else tau
        times=[arrival,arrival+.5,7200.]
        values=[0.,flow,flow*2 if pulse else flow]
        trace=directory/'trace.csv'
        with trace.open('w',newline='') as stream:
            writer=csv.writer(stream)
            writer.writerow(['seconds','outflow_cfs'])
            writer.writerows(zip(times,values))
        data=dict(kind='pulse' if pulse else 'step',divisions=1,flow_cfs=flow,
                  library={'sha256':'synthetic-reference'},maximum_outflow_cfs=max(values),minimum_outflow_cfs=0.,
                  normalized_l1=(tau-arrival)/tau if early else 0.,
                  balance={'flow_percent':0.},final_volumes_ft3=None,
                  artifact_sha256={'trace.csv':hashlib.sha256(trace.read_bytes()).hexdigest()})
        (directory/'result.json').write_text(json.dumps(data))

    def test_exact_rectangular_normal_depth_and_shock_speed(self):
        # Choose depth first, compute Q directly, then verify the inverse and speed.
        depth=1.;area=3.;radius=3/5
        flow=1.486/.013*area*radius**(2/3)*(.002**.5)
        actual=reference(flow)
        self.assertAlmostEqual(actual['depth_ft'],depth,places=13)
        self.assertAlmostEqual(actual['arrival_seconds'],1000*area/flow,places=11)

    def test_exact_front_passes(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary);self.sample(directory)
            self.assertTrue(assess(directory)['passed'])

    def test_zero_balance_cannot_hide_early_front(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary);self.sample(directory,early=True)
            result=assess(directory)
            self.assertTrue(result['criteria']['continuity'])
            self.assertFalse(result['criteria']['step_shape'])
            self.assertFalse(result['criteria']['step_timing'])
            self.assertFalse(result['passed'])

    def test_zero_balance_cannot_hide_pulse_overshoot(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary);self.sample(directory,pulse=True)
            result=assess(directory)
            self.assertTrue(result['criteria']['continuity'])
            self.assertFalse(result['criteria']['peak_bound'])
            self.assertFalse(result['passed'])

    def test_changed_trace_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary);self.sample(directory)
            with (directory/'trace.csv').open('a') as stream:stream.write('7201,0\n')
            with self.assertRaisesRegex(ValueError,'Artifact changed'):assess(directory)

    def volume_trace(self,directory,*,temporary_loss=0.,initials=(0.,0.,0.)):
        self.sample(directory)
        trace=directory/'trace.csv'
        with trace.open() as stream:rows=list(csv.DictReader(stream))
        with trace.open('w',newline='') as stream:
            writer=csv.writer(stream)
            writer.writerow(['seconds','outflow_cfs','initial_ft3','inflow_ft3',
                             'outflow_losses_ft3','storage_ft3','residual_ft3'])
            for i,row in enumerate(rows):
                loss=temporary_loss if i==1 else 0.
                writer.writerow([row['seconds'],row['outflow_cfs'],initials[i],100*(i+1),
                                 0.,100*(i+1)-loss,loss])
        path=directory/'result.json';data=json.loads(path.read_text())
        data['artifact_sha256']['trace.csv']=hashlib.sha256(trace.read_bytes()).hexdigest()
        data['final_volumes_ft3']=[300.,0.,300.,0.]
        path.write_text(json.dumps(data))

    def test_final_zero_balance_cannot_hide_temporary_water_loss(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary);self.volume_trace(directory,temporary_loss=10.)
            result=assess(directory)
            self.assertTrue(result['criteria']['continuity'])
            self.assertFalse(result['criteria']['instantaneous_balance'])
            self.assertEqual(result['maximum_balance_threshold_excess_ft3'],8.)
            self.assertFalse(result['passed'])

    def test_conserved_trace_with_fixed_initial_storage_passes(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary);self.volume_trace(directory)
            self.assertTrue(assess(directory)['passed'])

    def test_variable_initial_storage_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary);self.volume_trace(directory,initials=(0.,1.,0.))
            with self.assertRaisesRegex(ValueError,'Initial storage changed'):assess(directory)

    def test_nonfinite_volume_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary);self.volume_trace(directory,temporary_loss=float('nan'))
            with self.assertRaisesRegex(ValueError,'Nonfinite volume'):assess(directory)

    def test_negative_area_fails_despite_valid_outflow_and_final_balance(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary);self.sample(directory)
            path=directory/'result.json';data=json.loads(path.read_text())
            data['internal_area_checks']=dict(minimum_ft2=-1e-5,maximum_ft2=.3,
                                              negative_count=1,above_full_count=0)
            path.write_text(json.dumps(data))
            result=assess(directory)
            self.assertTrue(result['criteria']['continuity'])
            self.assertTrue(result['criteria']['peak_bound'])
            self.assertFalse(result['criteria']['nonnegative_area'])
            self.assertFalse(result['passed'])

    def test_terminal_zero_clock_is_rejected_even_with_valid_digest(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary);self.sample(directory)
            trace=directory/'trace.csv'
            with trace.open('a') as stream:stream.write('0,0\n')
            path=directory/'result.json';data=json.loads(path.read_text())
            data['artifact_sha256']['trace.csv']=hashlib.sha256(trace.read_bytes()).hexdigest()
            path.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError,'routing clock'):assess(directory)


if __name__=='__main__':unittest.main()
