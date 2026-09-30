"""Five divider variant groups: independent creation, syntax and context."""
from dataclasses import replace
from datetime import date,time,timedelta
import math
import unittest

from easysewer.io.inp import InpDocument
from easysewer.model import Model,Ref
from easysewer.model import network as n,geometry as g
from easysewer.model.inflows import DryWeatherFlow
from easysewer.model.resources import Curve,CurvePoint
from easysewer.model.report import ReportSelection
from easysewer.validation import ValidationError

KINDS=('OVERFLOW','CUTOFF','TABULAR','WEIR')
UNITS=('CFS','GPM','MGD','CMS','LPS','MLD')
# Pinned swmm5.c Qcf factors, including its rounded CMS and distinct LPS factors.
Q_FROM_CFS=dict(zip(UNITS,(1,448.831,.64632,.02832,28.317,2.4466)))
TAILS={'omitted':(None,None,None,None),'max':(5,None,None,None),
       'initial':(5,.1,None,None),'surcharge':(5,.1,.2,None),
       'full':(5,.1,.2,20),'pond-only':(None,None,None,20),'zero':(0,0,0,0)}
FIELDS=('max_depth','initial_depth','surcharge_depth','ponded_area')
CASES={kind:((kind,'omitted'),) for kind in KINDS}
CASES['optional node fields']=tuple((kind,tail) for kind in KINDS for tail in TAILS)
OWNER=Ref(collection='swmm:nodes',key='J')
EVIDENCE=[]

def factors(units):return (1 if units in UNITS[:3] else .3048),Q_FROM_CFS[units]
def number(value):return format(value,'.17g')
def optional(tail,units):
    length,_=factors(units)
    return tuple(None if v is None else v*(length**2 if i==3 else length) for i,v in enumerate(TAILS[tail]))

def row(kind,tail='omitted',units='CFS'):
    length,flow=factors(units)
    law={'OVERFLOW':'OVERFLOW','CUTOFF':'CUTOFF '+number(.4*flow),'TABULAR':'TABULAR Div',
         'WEIR':'WEIR '+' '.join(number(v) for v in (.3*flow,2*length,1.5*flow/length**1.5))}[kind]
    values=optional(tail,units)
    end=max((i+1 for i,v in enumerate(values) if v is not None),default=0)
    suffix=''.join(' '+number(v or 0) for v in values[:end])
    return f'J {number(10*length)} D {law}{suffix}'

def source(kind='OVERFLOW',tail='omitted',units='CFS',routing='KINWAVE',inflow=5):
    length,flow=factors(units)
    return (f'[OPTIONS]\nFLOW_UNITS {units}\nFLOW_ROUTING {routing}\nALLOW_PONDING YES\n'
        'START_DATE 01/01/2004\nEND_DATE 01/01/2004\nEND_TIME 00:10:00\n'
        'REPORT_STEP 00:00:30\nROUTING_STEP 5\nVARIABLE_STEP 0\n'
        f'[DIVIDERS]\n{row(kind,tail,units)} ; split\n[OUTFALLS]\n'
        f'O {number(9*length)} FREE\nO2 {number(8*length)} FREE\n[CONDUITS]\n'
        f'D J O2 {number(100*length)} .013 0 0\nP J O {number(100*length)} .013 0 0\n'
        f'[XSECTIONS]\nD CIRCULAR {number(3*length)} 0 0 0\nP CIRCULAR {number(.5*length)} 0 0 0\n'
        '[CURVES]\nDiv DIVERSION 0 0\n'
        f'Div {number(2*flow)} {number(.4*flow)}\nDiv {number(10*flow)} {number(6*flow)}\n'
        f'[DWF]\nJ FLOW {number(inflow*flow)}\n[REPORT]\nNODES ALL\nLINKS ALL\n')

def created(kind='OVERFLOW',tail='omitted',units='CFS',routing='KINWAVE',inflow=5):
    length,flow=factors(units);model=Model()
    model.update_options(flow_units=units,flow_routing=routing,allow_ponding=True,
        start_date=date(2004,1,1),end_date=date(2004,1,1),end_time=time(0,10),
        report_step=timedelta(seconds=30),routing_step=timedelta(seconds=5),variable_step=0)
    model.curves.add(Curve(id='Div',kind='DIVERSION',points=tuple(CurvePoint(x=x*flow,y=y*flow) for x,y in ((0,0),(2,.4),(10,6)))))
    law={'OVERFLOW':n.OverflowDivider(),'CUTOFF':n.CutoffDivider(cutoff_flow=.4*flow),
        'TABULAR':n.TabularDivider(curve=Ref(collection='swmm:curves',key='Div')),
        'WEIR':n.WeirDivider(minimum_flow=.3*flow,height=2*length,coefficient=1.5*flow/length**1.5)}[kind]
    model.nodes.add(n.Divider(id='J',elevation=10*length,diverted_link=Ref(collection='swmm:links',key='D'),
        law=law,**dict(zip(FIELDS,optional(tail,units)))))
    for key,elevation in (('O',9),('O2',8)):
        model.nodes.add(n.Outfall(id=key,elevation=elevation*length,boundary=n.FreeBoundary()))
    for key,outlet,diameter in (('D','O2',3),('P','O',.5)):
        model.links.add(n.Conduit(id=key,inlet=OWNER,outlet=Ref(collection='swmm:nodes',key=outlet),
            length=100*length,roughness=.013,inlet_offset=0,outlet_offset=0,
            section=g.CrossSection(geometry=g.Circular(diameter=diameter*length))))
    model.dwf.add(DryWeatherFlow(node=OWNER,baseline=inflow*flow))
    model.update_report(nodes=ReportSelection(mode='ALL'),links=ReportSelection(mode='ALL'))
    return model

def load(text,strict=True):
    return Model.from_document(InpDocument.from_bytes(text if isinstance(text,bytes) else text.encode(),source='divider-gates.inp'),strict=strict)
def portable(model):return Model.from_json_document(model.to_json_document(),strict=True)
def facts(model):
    return tuple((p,model.inspect_field(OWNER,p).semantics.effective) for p in ('elevation','diverted_link','law',*FIELDS))

class DividerGateTests(unittest.TestCase):
    def test_each_variant_source_create_edit_clear_and_roundtrip(self):
        for variant,cases in CASES.items():
            for kind,tail in cases:
                for units in UNITS:
                    with self.subTest(variant=variant,kind=kind,tail=tail,units=units):
                        text=source(kind,tail,units)
                        raw=b'\xef\xbb\xbf'+text.replace('[DIVIDERS]','[dividers]\n; 分流节点\n[DIVIDERS]').replace('\n','\r\n').encode()
                        model=load(raw);self.assertEqual(model.to_document().to_bytes(),raw)
                        fresh=created(kind,tail,units);self.assertTrue(fresh.validate(for_run=True).is_valid)
                        self.assertEqual(portable(fresh).nodes['J'],fresh.nodes['J'])
                        for other in (fresh,portable(model),load(model.to_document(normalize=True).text),load(fresh.to_document().text)):
                            self.assertEqual(facts(other),facts(model))
                        length,flow=factors(units)
                        model.nodes.update('J',elevation=11*length,law=n.CutoffDivider(cutoff_flow=.7*flow),
                            max_depth=6*length,initial_depth=.2*length,surcharge_depth=.3*length,ponded_area=30*length**2)
                        for other in (portable(model),load(model.to_document().text)):
                            self.assertEqual(other.nodes['J'],model.nodes['J'])
                        model.nodes.update('J',**dict.fromkeys(FIELDS))
                        self.assertTrue(all(getattr(load(model.to_document().text).nodes['J'],p) is None for p in FIELDS))
            EVIDENCE.append(dict(gate='source-create-edit-roundtrip',variant=variant,cases=len(cases)*6))

    def test_each_variant_identity_order_references_and_rollback(self):
        for variant,cases in CASES.items():
            for kind,tail in cases:
                with self.subTest(variant=variant,kind=kind,tail=tail):
                    model=load(source(kind,tail));before=model.to_json_document()
                    with self.assertRaises(RuntimeError):
                        with model.transaction():
                            model.nodes.rename('J','Temporary');model.links.rename('D','Moved');raise RuntimeError('rollback')
                    self.assertEqual(model.to_json_document(),before)
                    with self.assertRaises(ValueError):model.nodes.rename('J','O')
                    self.assertEqual(model.to_json_document(),before)
                    with self.assertRaises(ValidationError):model.links.remove('D')
                    model.links.rename('D','Diverted');self.assertEqual(model.nodes['J'].diverted_link.key,'Diverted')
                    if kind=='TABULAR':
                        with self.assertRaises(ValidationError):model.curves.remove('Div')
                        model.curves.rename('Div','Curve');self.assertEqual(model.nodes['J'].law.curve.key,'Curve')
                    model.nodes.move('O2',before='J');model.links.move('P',before='Diverted')
                    restored=load(model.to_document().text)
                    self.assertEqual(tuple(restored.nodes),tuple(model.nodes));self.assertEqual(tuple(restored.links),tuple(model.links))
                    model.nodes.rename('J','Split');self.assertEqual(model.links['P'].inlet.key,'Split')
                    self.assertIn(('Split','FLOW'),model.dwf)
                    self.assertEqual(portable(model).nodes['Split'],model.nodes['Split'])
                    with self.assertRaises(ValidationError):model.nodes.remove('Split')
                    model.nodes.remove('Split',cascade=True)
                    self.assertFalse(model.links);self.assertFalse(model.dwf)
                    self.assertNotIn('Split',load(model.to_document().text).nodes)
            EVIDENCE.append(dict(gate='identity-references-rollback',variant=variant,cases=len(cases)))

    def test_each_variant_defaults_units_and_sources(self):
        for variant,cases in CASES.items():
            for kind,tail in cases:
                for units in UNITS:
                    with self.subTest(variant=variant,kind=kind,tail=tail,units=units):
                        model=load(source(kind,tail,units));length,flow=factors(units)
                        for field in FIELDS:
                            info=model.inspect_field(OWNER,field)
                            self.assertEqual(info.semantics.default.value,0)
                            self.assertEqual(info.provenance.status,'omitted' if getattr(model.nodes['J'],field) is None else 'explicit')
                        self.assertEqual(model.inspect_field(OWNER,'elevation').semantics.unit.value,'ft' if length==1 else 'm')
                        target='CMS' if length==1 else 'CFS';tl,tq=factors(target)
                        changed=model.copy();changed.convert_units(target)
                        self.assertAlmostEqual(changed.nodes['J'].elevation,10*tl,places=10)
                        self.assertEqual(changed.nodes['J'].diverted_link,model.nodes['J'].diverted_link)
                        for field in FIELDS:
                            value=getattr(model.nodes['J'],field);actual=getattr(changed.nodes['J'],field)
                            if value is None:self.assertIsNone(actual)
                            else:self.assertAlmostEqual(actual,value*(tl/length)**(2 if field=='ponded_area' else 1),places=10)
                        if kind=='CUTOFF':self.assertAlmostEqual(changed.nodes['J'].law.cutoff_flow,.4*tq,places=10)
                        if kind=='TABULAR':
                            self.assertAlmostEqual(changed.curves['Div'].points[1].x,2*tq,places=10)
                            self.assertAlmostEqual(changed.curves['Div'].points[1].y,.4*tq,places=10)
                        if kind=='WEIR':
                            law=changed.nodes['J'].law
                            self.assertAlmostEqual(law.minimum_flow,.3*tq,places=10)
                            self.assertAlmostEqual(law.height,2*tl,places=10)
                            self.assertAlmostEqual(law.coefficient,1.5*tq/tl**1.5,places=10)
                            self.assertTrue(model.inspect_field(OWNER,('law','coefficient')).provenance.declarations)
            EVIDENCE.append(dict(gate='defaults-units-sources',variant=variant,cases=len(cases)*6))

    def test_each_variant_invalid_unknown_input_and_diagnostic_sources(self):
        for variant,cases in CASES.items():
            for kind,tail in cases:
                with self.subTest(variant=variant,kind=kind,tail=tail):
                    text=source(kind,tail);original=row(kind,tail)+' ; split'
                    invalid=('J','J bad D OVERFLOW','J 10 D CUTOFF','J 10 D WEIR .3 2',
                        'J 10 D CUTOFF nan','J 10 D CUTOFF -1','J 10 D WEIR .3 0 1.5',
                        'J 10 Missing OVERFLOW','J 10 D TABULAR Missing',row(kind,tail)+' 0 0 0 0 EXTRA')
                    for replacement in invalid:
                        bad=text.replace(original,replacement);model=load(bad,strict=False)
                        self.assertEqual(model.document.text,bad)
                        self.assertTrue(model.validate().errors,replacement)
                        self.assertTrue(any(d.span or d.locations for d in model.validate().errors))
                        with self.assertRaises(ValidationError):model.to_document()
                    duplicate=load(text+'[DIVIDERS]\nj 10 D OVERFLOW\n',strict=False)
                    self.assertFalse(duplicate.validate().is_valid)
                    unknown=load(text.replace(original,'J 10 D FUTURE .3'),strict=False)
                    self.assertIn('FUTURE',unknown.document.text);self.assertTrue(unknown.support.opaque_records)
                    with self.assertRaises(ValidationError):unknown.links.rename('D','Unsafe')
                    model=load(text);before=model.to_json_document()
                    for changes in ({'max_depth':-1},{'initial_depth':math.inf},{'diverted_link':Ref(collection='swmm:links',key='Missing')}):
                        with self.assertRaises((ValueError,ValidationError)):
                            with model.transaction():model.nodes.update('J',**changes)
                        self.assertEqual(model.to_json_document(),before)
            EVIDENCE.append(dict(gate='invalid-unknown-diagnostics',variant=variant,cases=len(cases)))

    def test_routing_topology_resource_roles_and_weir_boundaries(self):
        for kind in KINDS:
            for routing in ('STEADY','KINWAVE','DYNWAVE'):
                for units in UNITS:
                    with self.subTest(kind=kind,routing=routing,units=units):
                        model=created(kind,'full',units,routing)
                        self.assertEqual(model.inspect_field(OWNER,'law').semantics.effective.status,'not_applicable' if routing=='DYNWAVE' else 'known')
                        self.assertEqual('divider.inactive_law' in {d.code for d in model.validate().diagnostics},routing=='DYNWAVE')
                        missing=model.copy();missing.nodes.update('J',diverted_link=None)
                        self.assertIn('divider.missing_link',{d.code for d in missing.validate(for_run=True).errors})
                        detached=model.copy();detached.links.update('D',inlet=Ref(collection='swmm:nodes',key='O'))
                        self.assertIn('divider.unattached_link',{d.code for d in detached.validate(for_run=True).errors})
                        incoming=model.copy();incoming.links.update('D',inlet=Ref(collection='swmm:nodes',key='O2'),outlet=OWNER)
                        self.assertEqual('divider.incoming_diversion' in {d.code for d in incoming.validate(for_run=True).errors},routing!='DYNWAVE')
                        extra=model.copy();extra.links.add(replace(extra.links['P'],id='Third'))
                        self.assertEqual('divider.too_many_outlets' in {d.code for d in extra.validate(for_run=True).errors},routing!='DYNWAVE')
        model=created('TABULAR');model.curves.update('Div',kind='STORAGE')
        self.assertIn('resource.wrong_purpose',{d.code for d in model.validate().errors})
        for minimum,valid,warning in ((0,True,False),(1,True,True),(2,False,False)):
            model=created('WEIR');model.nodes.update('J',law=n.WeirDivider(minimum_flow=minimum,height=1,coefficient=1))
            report=model.validate(for_run=True)
            self.assertEqual(report.is_valid,valid)
            self.assertEqual('divider.zero_weir_range' in {d.code for d in report.diagnostics},warning)
        EVIDENCE.append(dict(gate='routing-topology-boundaries',laws=4,routing_modes=3,units=6,weir_limits=3))

if __name__=='__main__':unittest.main()
