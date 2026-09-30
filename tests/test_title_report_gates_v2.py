"""Per-variant TITLE/REPORT lifecycle, provenance and input boundaries."""
from dataclasses import replace
from datetime import time,timedelta
import unittest
from easysewer.io.inp import InpDocument
from easysewer.model import Model,Ref
from easysewer.model.project import ProjectTitle
from easysewer.model.report import ReportSelection,REPORT_DEFAULTS
from easysewer.model.resources import SeriesPoint
from easysewer.validation import ValidationError
from test_hydrology_v2 import hydrology_model

UNITS=('CFS','GPM','MGD','CMS','LPS','MLD')
TITLE=Ref(collection='swmm:title',key='text')
REPORT=Ref(collection='swmm:report',key='settings')
TITLES={
    'raw lines':('[TITLE]\nFirst "unclosed; literal\nSecond title\n',('First "unclosed; literal','Second title')),
    'empty lines':('[TITLE]\nFirst\n\nLast\n',('First','','Last')),
    'comments':('[TITLE]\n; comment only\nFirst ; inline\n',('; comment only','First ; inline')),
    'repeated sections':('[TITLE]\nFirst\n[TITLE]\nSecond\n',('First','Second')),
}
SWITCHES={'disabled':'DISABLED','input':'INPUT','continuity':'CONTINUITY',
          'flow_stats':'FLOWSTATS','controls':'CONTROLS','averages':'AVERAGES'}
KEYWORDS={'subcatchments':'SUBCATCHMENTS','nodes':'NODES','links':'LINKS'}
HISTORIES=(('ALL','{last}'),('{last}','{first}'),('{first}','NONE'),
           ('{first}','ALL','NONE'),('NONE',),('{first}','ALL'))

def load(text,strict=True):
    return Model.from_document(InpDocument.from_text(text,source='title-report.inp'),strict=strict)

def fixture(units='CFS',count=2):
    m=hydrology_model()
    m.update_options(end_date=m.options.start_date,end_time=time(0,20),
        allow_ponding=True,report_step=timedelta(minutes=1),routing_step=timedelta(seconds=30),
        wet_step=timedelta(minutes=1))
    m.timeseries.update('Rain',points=(SeriesPoint(time=timedelta(),value=.6),
        SeriesPoint(time=timedelta(minutes=20),value=0)))
    m.raingages.update('R',interval=timedelta(minutes=1))
    m.nodes.update('J',initial_depth=1)
    for i in range(1,count):
        j,o,p,s=('J'+str(i),'O'+str(i),'P'+str(i),'S'+str(i))
        m.nodes.add(replace(m.nodes['J'],id=j));m.nodes.add(replace(m.nodes['O'],id=o))
        m.links.add(replace(m.links['P'],id=p,inlet=Ref(collection='swmm:nodes',key=j),
            outlet=Ref(collection='swmm:nodes',key=o)))
        m.subcatchments.add(replace(m.subcatchments['S'],id=s,outlet=Ref(collection='swmm:nodes',key=j)))
    if units!='CFS':m.convert_units(units)
    return m

def selection(name,*keys,mode='SELECTED'):
    return ReportSelection(mode=mode,members=tuple(Ref(collection='swmm:'+name,key=k) for k in keys))

def history_case(name,history,model):
    names=tuple(model.collection('swmm:'+name));first,last=names[0],names[-1]
    values=tuple(v.format(first=first,last=last) for v in history)
    marked={v for v in values if v not in ('ALL','NONE')}
    expected=tuple(k for k in names if values[-1]=='ALL' or k in marked)
    return ''.join('[REPORT]\n'+KEYWORDS[name]+' '+v+'\n' for v in values),expected

def long_model(name):
    m=fixture(count=43)
    collection=m.collection('swmm:'+name)
    names=tuple(collection)[:43]
    collection.rename(names[39],'ALL');collection.rename(names[40],'NONE')
    return m,(*names[:39],'ALL','NONE',*names[41:])

def rereads(m):
    return (Model.from_document(m.to_document(),strict=True),
            Model.from_json_document(m.to_json_document(),strict=True))

class TitleReportGateTests(unittest.TestCase):
    def test_title_each_variant_invalid_input_source_and_rejected_edits(self):
        for variant,(text,_) in TITLES.items():
            with self.subTest(variant=variant):
                bad=text+'invalid\x00line\n';m=load(bad,False)
                self.assertEqual(m.document.text,bad)
                self.assertTrue(any(d.span and d.span.line==len(bad.splitlines()) for d in m.validate().errors))
                with self.assertRaises(ValidationError):m.to_document()
                with self.assertRaises(ValidationError):load(bad)
                m=load(text);before=m.to_document().to_bytes()
                for value in ('[REPORT]','first\nsecond','bad\x00line'):
                    with self.assertRaises(ValidationError):m.update_title(lines=(value,))
                    self.assertEqual(m.to_document().to_bytes(),before)

    def test_title_each_variant_create_edit_clear_remove_and_rollback(self):
        for variant,(text,lines) in TITLES.items():
            with self.subTest(variant=variant):
                m=load(text);self.assertEqual(m.title.lines,lines);self.assertEqual(m.to_document().text,text)
                fresh=Model();fresh.update_title(lines=lines)
                for n in (*rereads(m),*rereads(fresh)):self.assertEqual(n.title.lines,lines)
                before=m.to_document().to_bytes();old=m.title
                self.assertEqual(tuple(m.collection('swmm:title')),('text',))
                with self.assertRaises(TypeError):m.collection('swmm:title').rename('text','other')
                with self.assertRaises(RuntimeError):
                    with m.transaction():
                        m.update_title(lines=tuple(reversed(lines)))
                        raise RuntimeError('rollback')
                with self.assertRaises(ValidationError):m.update_title(lines=('bad\x00',))
                self.assertEqual(m.title,old);self.assertEqual(m.to_document().to_bytes(),before)
                m.update_title(lines=(*lines,'Added'))
                for n in rereads(m):self.assertEqual(n.title,m.title)
                m.update_title(lines=())
                for n in rereads(m):self.assertEqual(n.title.lines,())
                m.collection('swmm:title').remove('text')
                for n in rereads(m):self.assertEqual(n.title.lines,());self.assertFalse(n.collection('swmm:title'))

    def test_title_each_variant_raw_sources_json_units_and_empty_boundaries(self):
        for variant,(text,lines) in TITLES.items():
            for units in UNITS:
                with self.subTest(variant=variant,units=units):
                    m=load(text);m.reinterpret_units(units)
                    before=m.field_provenance(TITLE,'lines')
                    self.assertEqual(tuple(d.raw_text for d in before.declarations),lines)
                    for d in before.declarations:
                        self.assertEqual(d.tokens,());self.assertEqual(d.source_owners,(TITLE.canonical,))
                        self.assertEqual(text.splitlines()[d.line-1],d.raw_text)
                        self.assertEqual((d.span.source,d.span.column,d.span.end_column),('title-report.inp',1,len(d.raw_text)+1))
                    info=m.inspect_field(TITLE,'lines');self.assertEqual(info.semantics.unit.status,'not_applicable')
                    n=Model.from_json_document(m.to_json_document(),strict=True)
                    self.assertEqual(n.field_provenance(TITLE,'lines'),before)
                    m.convert_units('CMS' if units!='CMS' else 'CFS')
                    self.assertEqual(m.title.lines,lines);self.assertEqual(m.field_provenance(TITLE,'lines'),before)
        for text,lines in (('',()),('[TITLE]\n',()),('[TITLE]\n\n',('',)),('[TITLE]\n; only\n',('; only',))):
            m=load(text);self.assertEqual(m.title.lines,lines);self.assertEqual(m.to_document().text,text)
            for n in rereads(m):self.assertEqual(n.title.lines,lines)
        m=Model();m.collection('swmm:title').add(ProjectTitle())
        self.assertEqual(m.inspect_field(TITLE,'lines').semantics.default.value,())

    def test_report_each_switch_explicit_default_clear_source_invalid_and_rollback(self):
        for field,keyword in SWITCHES.items():
            for units in UNITS:
                with self.subTest(field=field,units=units):
                    base=fixture(units).to_document().text
                    m=load(base+'[REPORT]\n'+keyword+' NO\n[REPORT]\n'+keyword+' YES\n')
                    self.assertTrue(getattr(m.report,field))
                    d=m.field_provenance(REPORT,field).declarations
                    self.assertEqual(tuple(v.tokens[0].raw for v in d),('NO','YES'))
                    self.assertEqual(tuple(v.contributes for v in d),(False,True))
                    for value in (True,False,None):
                        m.update_report(**{field:value})
                        for n in rereads(m):self.assertIs(getattr(n.report,field),value)
                    self.assertEqual(getattr(m.effective_report.settings,field),dict(REPORT_DEFAULTS)[field])
                    before=m.to_document().to_bytes();old=m.report
                    with self.assertRaises(ValidationError):m.update_report(**{field:1})
                    with self.assertRaises(RuntimeError):
                        with m.transaction():
                            m.update_report(**{field:True});raise RuntimeError('rollback')
                    self.assertEqual(m.report,old);self.assertEqual(m.to_document().to_bytes(),before)
                    bad=base+'[REPORT]\n'+keyword+' maybe\n';n=load(bad,False)
                    self.assertEqual(n.document.text,bad)
                    issue=next(d for d in n.validate().errors if d.code=='report.invalid_input')
                    self.assertEqual(issue.field,field);self.assertEqual(issue.span.line,len(bad.splitlines()))
                    with self.assertRaises(ValidationError):n.to_document()

    def test_report_three_collections_histories_sources_units_and_repeated_sections(self):
        for units in UNITS:
            for name in KEYWORDS:
                for history in HISTORIES:
                    with self.subTest(units=units,name=name,history=history):
                        base=fixture(units);tail,expected=history_case(name,history,base)
                        text=base.to_document().text+tail;m=load(text)
                        self.assertEqual(m.to_document().text,text)
                        self.assertEqual(tuple(r.key for r in getattr(m.effective_report,name)),expected)
                        for n in rereads(m):
                            self.assertEqual(n.report,m.report)
                            self.assertEqual(n.field_provenance(REPORT,name),m.field_provenance(REPORT,name))
                        n=Model.from_document(m.to_document(normalize=True),strict=True)
                        self.assertEqual(n.report,m.report)
                        for d in m.field_provenance(REPORT,name).declarations:
                            for token in d.tokens:
                                self.assertEqual(text.splitlines()[token.span.line-1][token.span.column-1:token.span.end_column-1],token.raw)
                        before=m.report;m.convert_units('CMS' if units!='CMS' else 'CFS');self.assertEqual(m.report,before)

    def test_report_three_collections_identity_order_rename_delete_clear_and_rollback(self):
        for name in KEYWORDS:
            with self.subTest(name=name):
                m=fixture();rows=m.collection('swmm:'+name);keys=tuple(rows)
                m.update_report(**{name:selection(name,*reversed(keys))})
                self.assertEqual(tuple(r.key for r in getattr(m.effective_report,name)),keys)
                target=Ref(collection='swmm:'+name,key=keys[-1])
                self.assertTrue(any(use.owner==REPORT for use in m.referenced_by(target)))
                before=m.to_document().to_bytes()
                with self.assertRaises(ValidationError):rows.remove(target.key)
                self.assertEqual(m.to_document().to_bytes(),before)
                rows.rename(target.key,'Renamed')
                self.assertEqual(getattr(m.report,name).members[0].key,'Renamed')
                rows.move('Renamed',before=keys[0])
                self.assertEqual(getattr(m.effective_report,name)[0].key,'Renamed')
                before=m.to_document().to_bytes();old=m.report
                with self.assertRaises(RuntimeError):
                    with m.transaction():
                        m.update_report(**{name:ReportSelection(mode='ALL')});rows.rename('Renamed','Changed')
                        raise RuntimeError('rollback')
                self.assertEqual(m.to_document().to_bytes(),before);self.assertEqual(m.report,old)
                for value in (ReportSelection(mode='ALL'),ReportSelection(mode='NONE'),None):
                    m.update_report(**{name:value})
                    for n in rereads(m):self.assertEqual(getattr(n.report,name),value)
                self.assertFalse(any(use.owner==REPORT for use in m.referenced_by(Ref(collection='swmm:'+name,key='Renamed'))))

    def test_report_three_collections_long_lists_reserved_tokens_and_import_limit(self):
        for name in KEYWORDS:
            with self.subTest(name=name):
                m,names=long_model(name)
                for size in (38,39,40,41,43):
                    m.update_report(**{name:selection(name,*names[:size])})
                    doc=m.to_document()
                    self.assertTrue(all(len(row.values)<=40 and row.values[1] not in ('ALL','NONE') for row in doc.records('REPORT')))
                    for n in rereads(m):self.assertEqual(n.report,m.report)
                m.collection('swmm:report').remove('settings');base=m.to_document().text
                text=base+'[REPORT]\n'+KEYWORDS[name]+' '+' '.join(names)+'\n';m=load(text)
                self.assertEqual(getattr(m.report,name).members,selection(name,*names[:39]).members)
                self.assertEqual(m.to_document().text,text)
                self.assertIn('report.native_token_limit',{d.code for d in m.validate().diagnostics})
                self.assertEqual(load(m.to_document(normalize=True).text).report,m.report)

    def test_report_three_collections_invalid_references_unknown_and_atomicity(self):
        for name in KEYWORDS:
            with self.subTest(name=name):
                m=fixture();key=next(iter(m.collection('swmm:'+name)))
                before=m.to_document().to_bytes()
                for value in (selection(name),selection(name,key,key.lower()),selection(name,'ALL'),
                              ReportSelection(mode='SELECTED',members=(Ref(collection='swmm:timeseries',key='Rain'),))):
                    with self.assertRaises(ValidationError):m.update_report(**{name:value})
                    self.assertEqual(m.to_document().to_bytes(),before)
                text=before.decode()+'[REPORT]\n'+KEYWORDS[name]+' Missing\n';bad=load(text,False)
                issues=[d for d in bad.validate().errors if d.code=='model.unresolved_reference']
                self.assertTrue(issues)
                for issue in issues:
                    self.assertIsNone(issue.span)
                    self.assertEqual(issue.locations[0].status,'untracked')
                    context=next(v for v in issue.locations if v.subject.collection=='swmm:report' and v.subject.path==(name,'members'))
                    self.assertEqual(context.status,'current')
                    self.assertEqual({s.line for s in context.spans},{len(text.splitlines())})
                self.assertEqual(bad.document.text,text)
                unknown=before.decode()+'[REPORT]\nFUTURE opaque\n';n=load(unknown)
                self.assertEqual(n.to_document().text,unknown)
                issue=next(d for d in n.validate().diagnostics if d.code=='report.unsupported_keyword')
                self.assertEqual(issue.span.line,len(unknown.splitlines()))
                with self.assertRaises(ValidationError):n.update_report(**{name:ReportSelection(mode='ALL')})
                self.assertEqual(n.to_document().text,unknown)
                with self.assertRaises(ValidationError):n.collection('swmm:'+name).rename(key,'Changed')
                self.assertIn(key,n.collection('swmm:'+name))

if __name__=='__main__':unittest.main()
