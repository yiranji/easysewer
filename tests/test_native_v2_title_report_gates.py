"""Independent full native OUT/RPT comparisons for TITLE/REPORT variants."""
import ctypes,hashlib,os,struct,tempfile,unittest
from pathlib import Path
from easysewer import get_native_capabilities
from easysewer.model import Model
from easysewer.model.report import ReportSelection
from test_title_report_gates_v2 import (fixture,load,TITLES,UNITS,SWITCHES,KEYWORDS,
    HISTORIES,history_case,long_model,selection)
from test_native_v2_regulator_fields import FAMILIES,library
from test_native_v2_runner_checkpoint import reports

EVIDENCE=[]

def solve(test,lib,root,source):
    inp,rpt,out=(root/('model'+s) for s in ('.inp','.rpt','.out'))
    inp.write_text(source,encoding='utf-8');started=False
    try:
        test.assertEqual(lib.swmm_open(os.fsencode(inp),os.fsencode(rpt),os.fsencode(out)),0,rpt.read_text(errors='replace'))
        test.assertEqual(lib.swmm_start(1),0);started=True
        for _ in range(2000):
            elapsed=ctypes.c_double();test.assertEqual(lib.swmm_step(ctypes.byref(elapsed)),0)
            if not elapsed.value:break
        else:test.fail('Short input-gate fixture exceeded step bound')
        test.assertEqual(lib.swmm_end(),0);started=False
        test.assertEqual(lib.swmm_report(),0)
    finally:
        if started:test.assertEqual(lib.swmm_end(),0)
        test.assertEqual(lib.swmm_close(),0)
    raw=out.read_bytes();header=struct.unpack_from('<7i',raw)
    test.assertEqual(header[1],52004)
    pos=28;groups=[]
    for count in header[3:]:
        values=[]
        for _ in range(count):
            size,=struct.unpack_from('<i',raw,pos);pos+=4
            values.append(raw[pos:pos+size].decode('utf-8'));pos+=size
        groups.append(tuple(values))
    report=reports(rpt.read_bytes()).replace(b'\r\n',b'\n')
    return dict(out=raw,report=report,ids=tuple(groups),units=header[2])

def recorded(kind,family,units,result,**extra):
    EVIDENCE.append(dict(kind=kind,family=family,units=units,
        out_sha256=hashlib.sha256(result['out']).hexdigest(),
        report_sha256=hashlib.sha256(result['report']).hexdigest(),**extra))

@unittest.skipUnless(get_native_capabilities()['swmm_solver'] and get_native_capabilities()['flexible_ponding'],
                     'Both native engines required')
class NativeTitleReportGateTests(unittest.TestCase):
    def test_title_each_variant_native_roundtrip_edit_clear_and_first_three_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for family,name,symbol in FAMILIES:
                lib,_=library(name,symbol)
                for units in UNITS:
                    base=fixture(units);base.update_report(nodes=ReportSelection(mode='ALL'))
                    plain=solve(self,lib,root,base.to_document().text)
                    for variant,(text,_) in TITLES.items():
                        with self.subTest(family=family,units=units,variant=variant):
                            source=base.to_document().text+text;m=load(source)
                            expected=solve(self,lib,root,source)
                            m=Model.from_json_document(m.to_json_document(),strict=True)
                            self.assertEqual(solve(self,lib,root,m.to_document(normalize=True).text),expected)
                            self.assertEqual(expected['out'],plain['out'])
                            m.update_title(lines=('Edited first','; skip comment','','Edited second','Edited third','Ignored fourth'))
                            changed=solve(self,lib,root,m.to_document().text)
                            self.assertEqual(changed['out'],plain['out'])
                            for value in (b'Edited first',b'Edited second',b'Edited third'):self.assertIn(value,changed['report'])
                            for value in (b'skip comment',b'Ignored fourth'):self.assertNotIn(value,changed['report'])
                            m.update_title(lines=())
                            cleared=solve(self,lib,root,m.to_document().text)
                            self.assertEqual(cleared,plain)
                            recorded('title',family,units,expected,variant=variant,
                                edited_out_unchanged=True,clear_equal_to_absent=True,first_three_verified=True)

    def test_all_three_selection_histories_six_units_both_engines(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for family,name,symbol in FAMILIES:
                lib,_=library(name,symbol)
                for units in UNITS:
                    base=fixture(units)
                    for collection in KEYWORDS:
                        index=tuple(KEYWORDS).index(collection)
                        for history in HISTORIES:
                            with self.subTest(family=family,units=units,collection=collection,history=history):
                                tail,ids=history_case(collection,history,base);source=base.to_document().text+tail
                                expected=solve(self,lib,root,source)
                                self.assertEqual(expected['ids'][index],ids)
                                model=Model.from_json_document(load(source).to_json_document(),strict=True)
                                self.assertEqual(solve(self,lib,root,model.to_document(normalize=True).text),expected)
                                self.assertEqual(tuple(v.key for v in getattr(model.effective_report,collection)),ids)
                                noun={'nodes':'Node','links':'Link','subcatchments':'Subcatchment'}[collection]
                                first=next(iter(base.collection('swmm:'+collection)))
                                detail=('<<< '+noun+' '+first+' >>>').encode()
                                self.assertEqual(detail in expected['report'],history[-1]!='NONE' and first in ids)
                                recorded('selection-history',family,units,expected,collection=collection,
                                    history=history,expected_ids=ids)

    def test_six_switches_independent_literals_and_actual_report_effects(self):
        phrases={'input':b'Node Summary','continuity':b'Flow Routing Continuity',
                 'flow_stats':b'Routing Time Step Summary','controls':b'setting changed'}
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for family,name,symbol in FAMILIES:
                lib,_=library(name,symbol)
                for units in UNITS:
                    base=fixture(units)
                    base.update_report(nodes=ReportSelection(mode='ALL'),links=ReportSelection(mode='ALL'),
                        subcatchments=ReportSelection(mode='ALL'),**{k:False for k in SWITCHES})
                    base=load(base.to_document().text+'[CONTROLS]\nRULE Switch\nIF SIMULATION TIME >= 00:05:00\n'
                        'THEN OUTLET P SETTING = 0.2\nELSE OUTLET P SETTING = 1\n')
                    plain=solve(self,lib,root,base.to_document().text)
                    for field,keyword in SWITCHES.items():
                        with self.subTest(family=family,units=units,field=field):
                            literal=base.to_document().text+'[REPORT]\n'+keyword+' YES\n'
                            expected=solve(self,lib,root,literal)
                            changed=base.copy();changed.update_report(**{field:True})
                            changed=Model.from_json_document(changed.to_json_document(),strict=True)
                            actual=solve(self,lib,root,changed.to_document(normalize=True).text)
                            self.assertEqual(actual,expected)
                            if field=='averages':self.assertNotEqual(actual['out'],plain['out'])
                            else:self.assertEqual(actual['out'],plain['out'])
                            if field in phrases:
                                self.assertIn(phrases[field],actual['report']);self.assertNotIn(phrases[field],plain['report'])
                            elif field=='disabled':
                                self.assertIn(b'Analysis Options',plain['report']);self.assertNotIn(b'Analysis Options',actual['report'])
                                self.assertIn(b'<<< Node J >>>',actual['report'])
                            recorded('switch',family,units,actual,field=field,literal_equal=True)

    def test_three_long_lists_reserved_ids_chunk_boundary_and_native_truncation(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for family,name,symbol in FAMILIES:
                lib,_=library(name,symbol)
                for collection in KEYWORDS:
                    for units in ('CFS','CMS'):
                        with self.subTest(family=family,collection=collection,units=units):
                            m,names=long_model(collection)
                            if units!='CFS':m.convert_units(units)
                            base=m.to_document().text;index=tuple(KEYWORDS).index(collection)
                            # One literal native line truncates after 39 members.
                            source=base+'[REPORT]\n'+KEYWORDS[collection]+' '+' '.join(names)+'\n'
                            expected=solve(self,lib,root,source);self.assertEqual(expected['ids'][index],names[:39])
                            parsed=load(source)
                            self.assertEqual(solve(self,lib,root,parsed.to_document(normalize=True).text),expected)
                            # Public creation emits multiple lines and preserves reserved IDs.
                            m.update_report(**{collection:selection(collection,*names)})
                            all_members=solve(self,lib,root,m.to_document().text)
                            self.assertEqual(all_members['ids'][index],names)
                            m=Model.from_json_document(m.to_json_document(),strict=True)
                            self.assertEqual(solve(self,lib,root,m.to_document(normalize=True).text),all_members)
                            recorded('long-list',family,units,all_members,collection=collection,
                                truncated_ids=names[:39],created_ids=names)

if __name__=='__main__':unittest.main()
