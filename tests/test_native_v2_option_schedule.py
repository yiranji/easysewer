"""Actual runoff clock frames and independent antecedent buildup mass oracles."""
import hashlib,os,struct,tempfile,unittest
from pathlib import Path
from easysewer import get_native_capabilities
from easysewer.model import Model
from easysewer.io.inp import InpDocument
from easysewer.validation import ValidationError
from easysewer.io.hotstart import HotstartData,HotstartLayout
from test_option_effects_v2 import UNITS,load
from test_option_schedule_v2 import schedule_fixture,buildup_fixture
from test_native_v2_option_effects import observe,digest
from test_native_v2_regulator_fields import FAMILIES,library

EVIDENCE=[]
# The tagged engine's native period is 899 seconds for this calendar. First
# dry frame ends at the first gage interval; following frames stop at rain changes.
SCHEDULES=(
    (30,300,True,(60,120)+(30,)*23+(29,)),
    (60,300,True,(60,120)+(60,)*11+(59,)),
    (120,300,True,(60,120)+(60,)*11+(59,)),
    (60,120,True,(60,120)+(60,)*11+(59,)),
    (60,300,False,(60,300,300,239)),
    (60,120,False,(60,)+(120,)*6+(119,)),
    (120,60,True,(60,)*14+(59,)),
)


def run_schedule(test,lib,root,source):
    cache=root/'runoff.bin'
    result=observe(test,lib,root,source+f'[FILES]\nSAVE RUNOFF "{cache}"\n')
    raw=cache.read_bytes();test.assertEqual(raw[:12],b'SWMM5-RUNOFF')
    nc,np,unit,count=struct.unpack_from('<4i',raw,12)
    test.assertEqual((nc,np),(1,0));test.assertGreater(count,0)
    test.assertEqual(len(raw),28+count*36)
    steps=tuple(struct.unpack_from('<f',raw,28+36*i)[0] for i in range(count))
    return result,raw,steps


def initial_mass(test,lib,root,model,source):
    inp=root/'initial.inp';state=root/'initial.hsf';rpt=root/'initial.rpt';started=False
    inp.write_text(source+f'[FILES]\nSAVE HOTSTART "{state}"\n',encoding='utf-8')
    try:
        test.assertEqual(lib.swmm_open(os.fsencode(inp),os.fsencode(rpt),b''),0)
        test.assertEqual(lib.swmm_start(0),0);started=True
        test.assertEqual(lib.swmm_end(),0);started=False
    finally:
        if started:test.assertEqual(lib.swmm_end(),0)
        test.assertEqual(lib.swmm_close(),0)
    raw=state.read_bytes()
    data=HotstartData.from_bytes(raw,layout=HotstartLayout.from_model(model))
    return raw,data.subcatchments[0].landuses[0].buildup


@unittest.skipUnless(get_native_capabilities()['swmm_solver'] and get_native_capabilities()['flexible_ponding'],
                     'Both packaged engines required')
class NativeOptionScheduleTests(unittest.TestCase):
    def test_step_zero_rejection_negative_native_wrap_and_decimal_clock_coercions(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for family,name,symbol in FAMILIES:
                lib,_=library(name,symbol)
                for units in UNITS:
                    source=schedule_fixture(units).to_document().text
                    for keyword in ('WET_STEP','DRY_STEP'):
                        for token in ('0','-1'):
                            with self.subTest(family=family,units=units,keyword=keyword,invalid=token):
                                literal=source+f'[OPTIONS]\n{keyword} {token}\n'
                                inp=root/'bad.inp';rpt=root/'bad.rpt'
                                inp.write_text(literal,encoding='utf-8')
                                try:
                                    code=lib.swmm_open(os.fsencode(inp),os.fsencode(rpt),os.fsencode(root/'bad.out'))
                                    if token=='0':self.assertNotEqual(code,0)
                                    else:self.assertEqual(code,0)
                                finally:self.assertEqual(lib.swmm_close(),0)
                                if token=='0':self.assertIn('ERROR',rpt.read_text())
                                else:
                                    # The native decimal parser wraps -1 hour to 23 hours:
                                    # floor(-1/24) gives the positive fractional day while
                                    # the subsequent integer cast truncates toward zero.
                                    # The public nonnegative-duration contract rejects it.
                                    self.assertEqual(run_schedule(self,lib,root,literal),
                                        run_schedule(self,lib,root,source+f'[OPTIONS]\n{keyword} 23:00:00\n'))
                                with self.assertRaises(ValidationError):load(literal)
                                invalid=Model.from_document(InpDocument.from_text(literal),strict=False)
                                self.assertEqual(invalid.document.text,literal)
                                self.assertTrue(any(d.code=='options.invalid_input' and d.field==keyword.lower()
                                                    for d in invalid.validate().errors))
                                EVIDENCE.append(dict(kind='schedule-input-boundary',family=family,units=units,keyword=keyword,
                                    token=token,native_error=code,python_rejected=True,
                                    native_effective_seconds=82800 if token=='-1' else None))
                        # Following the rejected inputs, the same library must run normally.
                        expected=run_schedule(self,lib,root,source+f'[OPTIONS]\n{keyword} 00:00:01\n')
                        for token in ('0.0004','00:00:01.9'):
                            with self.subTest(family=family,units=units,keyword=keyword,coerced=token):
                                literal=source+f'[OPTIONS]\n{keyword} {token}\n'
                                actual=run_schedule(self,lib,root,literal)
                                self.assertEqual(actual,expected)
                                model=load(literal)
                                self.assertTrue(any(d.code=='options.native_coercion' and d.field==keyword.lower()
                                                    for d in model.validate().diagnostics))
                                self.assertEqual(run_schedule(self,lib,root,model.to_document(normalize=True).text),expected)
                                EVIDENCE.append(dict(kind='schedule-coercion',family=family,units=units,keyword=keyword,token=token,
                                                     cache_sha256=hashlib.sha256(actual[1]).hexdigest(),result=digest(actual[0])))

    def test_wet_dry_steps_rain_interval_clamps_and_rain_boundaries(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for family,name,symbol in FAMILIES:
                lib,_=library(name,symbol)
                for units in UNITS:
                    outputs={}
                    for wet,dry,raining,expected_steps in SCHEDULES:
                        with self.subTest(family=family,units=units,wet=wet,dry=dry,raining=raining):
                            base=schedule_fixture(units,raining=raining)
                            literal=base.to_document().text+f'[OPTIONS]\nWET_STEP 00:{wet//60:02}:{wet%60:02}\nDRY_STEP 00:{dry//60:02}:00\n'
                            result,raw,steps=run_schedule(self,lib,root,literal)
                            self.assertEqual(steps,expected_steps)
                            self.assertEqual(sum(steps),899)
                            model=schedule_fixture(units,wet,dry,raining=raining)
                            restored=Model.from_json_document(model.to_json_document(),strict=True)
                            self.assertEqual(run_schedule(self,lib,root,restored.to_document(normalize=True).text),(result,raw,steps))
                            self.assertEqual(run_schedule(self,lib,root,load(literal).to_document(normalize=True).text),(result,raw,steps))
                            if raining:self.assertGreater(max(result['series']['swmm:runoff']),0)
                            else:self.assertEqual(set(result['series']['swmm:runoff']),{0.0})
                            outputs[wet,dry,raining]=result['out']
                            EVIDENCE.append(dict(kind='runoff-schedule',family=family,units=units,wet=wet,dry=dry,raining=raining,
                                steps=steps,cache_sha256=hashlib.sha256(raw).hexdigest(),result=digest(result)))
                    self.assertNotEqual(outputs[30,300,True],outputs[60,300,True])
                    self.assertEqual(outputs[60,300,True],outputs[120,300,True])
                    self.assertEqual(outputs[60,300,False],outputs[60,120,False])

    def test_antecedent_days_initial_mass_washoff_and_explicit_loading_override(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for family,name,symbol in FAMILIES:
                lib,_=library(name,symbol)
                for units in UNITS:
                    for override in (False,True):
                        results=[];hydraulics=[]
                        for days,expected in ((0,0),(.5,2),(2,8),(5,20)):
                            with self.subTest(family=family,units=units,override=override,days=days):
                                model=buildup_fixture(units,override=override,raining=True)
                                literal=model.to_document().text+f'[OPTIONS]\nDRY_DAYS {days}\n'
                                raw,mass=initial_mass(self,lib,root,model,literal)
                                self.assertEqual(mass,(8,8) if override else (expected,expected))
                                result=observe(self,lib,root,literal)
                                model.update_options(dry_days=days)
                                restored=Model.from_json_document(model.to_json_document(),strict=True)
                                self.assertEqual(initial_mass(self,lib,root,restored,restored.to_document(normalize=True).text),(raw,mass))
                                self.assertEqual(observe(self,lib,root,restored.to_document(normalize=True).text),result)
                                results.append(result['out']);hydraulics.append(result['series'])
                                EVIDENCE.append(dict(kind='antecedent-dry-days',family=family,units=units,override=override,days=days,
                                    initial_mass=mass,hotstart_sha256=hashlib.sha256(raw).hexdigest(),result=digest(result)))
                        self.assertTrue(all(h==hydraulics[0] for h in hydraulics))
                        self.assertEqual(len(set(results)),1 if override else 4)


if __name__=='__main__':unittest.main()
