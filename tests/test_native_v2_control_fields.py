"""Control field defaults, full action history, unit oracle and moved recovery."""
from datetime import timedelta
import ctypes
import hashlib
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from easysewer import get_native_capabilities
from easysewer.io.inp import InpDocument
from test_control_fields_v2 import fixture, load, queries, materialize, UNITS, ATTRIBUTES, SETTINGS, CALENDAR
from test_native_v2_regulator_fields import FAMILIES, library

EVIDENCE=[]


def cases():
    return [('program',None),*[('attribute',v) for v in ATTRIBUTES],*[('setting',v) for v in SETTINGS],
            *[('gage',v) for v in range(1,49)],*[('calendar',v) for v in CALENDAR]]


def observe(test,lib,base,text):
    from test_native_v2_runner_checkpoint import reports
    inp,rpt,out=(base/('model'+suffix) for suffix in ('.inp','.rpt','.out'))
    inp.write_text(text+'[REPORT]\nCONTROLS YES\n',encoding='utf-8');started=False
    try:
        test.assertEqual(lib.swmm_open(os.fsencode(inp),os.fsencode(rpt),os.fsencode(out)),0,rpt.read_text(errors='replace'))
        link,node=lib.swmm_getIndex(3,b'P'),lib.swmm_getIndex(2,b'J')
        test.assertGreaterEqual(link,0);test.assertGreaterEqual(node,0)
        test.assertEqual(lib.swmm_start(1),0);started=True
        history=[];previous=0.
        for _ in range(20000):
            elapsed=ctypes.c_double();test.assertEqual(lib.swmm_step(ctypes.byref(elapsed)),0)
            if not elapsed.value:break
            history.append((previous,elapsed.value,lib.swmm_getValue(407,link),lib.swmm_getValue(410,link),lib.swmm_getValue(303,node)))
            previous=elapsed.value
        else:test.fail('Control fixture exceeded step bound')
        test.assertGreater(len(history),10)
        test.assertEqual(lib.swmm_end(),0);started=False
    finally:
        if started:test.assertEqual(lib.swmm_end(),0)
        test.assertEqual(lib.swmm_close(),0)
    report=reports(rpt.read_bytes()).replace(os.fsencode(base),b'<workspace>').replace(b'\r\n',b'\n')
    return dict(history=history,out_sha256=hashlib.sha256(out.read_bytes()).hexdigest(),report_sha256=hashlib.sha256(report).hexdigest())


@unittest.skipUnless(get_native_capabilities()['swmm_solver'] and get_native_capabilities()['flexible_ponding'],
                     'Both native families unavailable')
class NativeControlFieldTests(unittest.TestCase):
    def test_all_attributes_settings_windows_calendar_and_full_action_history(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory)
            for family,name,symbol in FAMILIES:
                lib,path=library(name,symbol);rows=[]
                for units in UNITS:
                    for mode,value in cases():
                        with self.subTest(family=family,units=units,mode=mode,value=value):
                            source=fixture(mode,value,units);m=load(source)
                            expected=observe(self,lib,base,source)
                            materialize(m);actual=observe(self,lib,base,m.to_document(normalize=True).text)
                            self.assertEqual(actual,expected)
                            rows.append(dict(mode=mode,value=value,units=units,steps=len(actual['history']),
                                out_sha256=actual['out_sha256'],report_sha256=actual['report_sha256']))
                EVIDENCE.append(dict(kind='control-field-native-results',family=family,cases=len(rows),rows=rows,
                    library_sha256=hashlib.sha256(Path(path).read_bytes()).hexdigest()))

    def test_modulated_actions_against_independent_depth_threshold_and_curve_units(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory)
            for family,name,symbol in FAMILIES:
                lib,path=library(name,symbol);rows=[]
                for obj,setting in SETTINGS:
                    for units in UNITS[1:]:
                        with self.subTest(family=family,obj=obj,setting=setting,units=units):
                            m=load(fixture('setting',(obj,setting)));m.convert_units(units)
                            converted=m.to_document(normalize=True).text;length=.3048 if units in UNITS[3:] else 1.
                            lines=[]
                            for line in InpDocument.from_text(converted).lines:
                                if line.section=='CONTROLS':continue
                                if line.section=='CURVES' and line.values and line.values[0]=='Control':continue
                                lines.append(line.content)
                            source='\n'.join(lines)+'\n'
                            if setting.startswith('CURVE'):
                                source+='[CURVES]\nControl CONTROL 0 .2 '+format(10*length,'.17g')+' .8\n'
                            source+=f'[CONTROLS]\nRULE R\nIF NODE J DEPTH >= {length:.17g}\nTHEN {obj} P SETTING = {setting}\nELSE {obj} P SETTING = .1\n'
                            expected=observe(self,lib,base,source);actual=observe(self,lib,base,converted)
                            self.assertEqual(actual,expected)
                            rows.append(dict(object_type=obj,setting=setting,units=units,out_sha256=actual['out_sha256']))
                EVIDENCE.append(dict(kind='control-field-conversion',family=family,cases=len(rows),rows=rows))

    def test_priority_order_has_observable_counterexamples(self):
        from test_controls_v2 import controlled
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory)
            for family,name,symbol in FAMILIES:
                lib,path=library(name,symbol)
                first='RULE First\nIF SIMULATION TIME >= 0\nTHEN CONDUIT P STATUS = CLOSED\n'
                second='RULE Second\nIF SIMULATION TIME >= 0\nTHEN CONDUIT P STATUS = OPEN\n'
                m=controlled(first+second);m.update_options(report_step=timedelta(minutes=1))
                original=observe(self,lib,base,m.to_document().text)
                m.controls.move(('RULE','Second'),before=('RULE','First'));altered=observe(self,lib,base,m.to_document().text)
                self.assertNotEqual(original['out_sha256'],altered['out_sha256'])
                EVIDENCE.append(dict(kind='control-order-counterexample',family=family,original_out_sha256=original['out_sha256'],altered_out_sha256=altered['out_sha256']))

    def test_fields_and_action_state_survive_moved_checkpoint(self):
        from easysewer.runtime import RunResult
        import test_native_v2_runner_checkpoint as checkpoint
        helper=checkpoint.NativeRunnerCheckpointTests()
        with patch.dict(os.environ,EASYSEWER_CHECKPOINT_PACKAGED='1'):
            for family in ('standard','custom'):
                for label,mode,value in (('program','program',None),('pid','setting',('ORIFICE','PID .1 1 .05')),('rain48','gage',48)):
                    with self.subTest(family=family,label=label),tempfile.TemporaryDirectory() as directory:
                        root=Path(directory);m=load(fixture(mode,value));before=queries(m)
                        original,saved=helper.original(root,family,model=m);helper.success(original);self.assertTrue(saved)
                        self.assertEqual(queries(original.snapshot.model()),before)
                        original.save(root/'expected');expected=RunResult.load(root/'expected')
                        self.assertEqual(queries(expected.snapshot.model()),before)
                        shutil.rmtree(root/'first');(root/'original.hsf').unlink();(root/'saved').rename(root/'moved')
                        actual=checkpoint.runner(family).resume(root/'moved'/saved[0].directory.name,checkpoint.resume_config(root/'resumed'))
                        helper.equivalent(expected,actual);self.assertEqual(queries(actual.snapshot.model()),before)
                        EVIDENCE.append(dict(kind='control-fields-checkpoint',family=family,case=label,queries=len(before),
                            out_sha256=actual.output.sha256,original_workspace_removed=True))


if __name__=='__main__':unittest.main()
