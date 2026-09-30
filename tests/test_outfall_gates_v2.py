"""Literal and independently created outfalls across all seven variant groups."""
from dataclasses import replace
from datetime import date,time,timedelta
import unittest

from easysewer.io.inp import InpDocument
from easysewer.model import Model,Ref
from easysewer.model import network as n,hydrology as h,geometry as g
from easysewer.model.resources import Curve,CurvePoint,InlineTimeSeries,SeriesPoint
from easysewer.model.report import ReportSelection
from easysewer.validation import ValidationError

UNITS=('CFS','GPM','MGD','CMS','LPS','MLD')
KINDS=('FREE','NORMAL','FIXED','TIDAL','TIMESERIES')
OWNER=Ref(collection='swmm:nodes',key='O')
CASES={kind:((kind,None,False),) for kind in KINDS}
CASES['flap gate']=tuple((kind,gate,False) for kind in KINDS for gate in (False,True))
CASES['route-to catchment']=tuple((kind,gate,True) for kind in KINDS for gate in (None,False,True))
EVIDENCE=[]


def boundary(kind,stage=14):
    return {'FREE':n.FreeBoundary(),'NORMAL':n.NormalBoundary(),'FIXED':n.FixedBoundary(stage=stage),
        'TIDAL':n.TidalBoundary(curve=Ref(collection='swmm:curves',key='Tide')),
        'TIMESERIES':n.SeriesBoundary(series=Ref(collection='swmm:timeseries',key='Stage'))}[kind]


def row(kind,gated=None,route=False,stage=14):
    parameter={'FREE':'','NORMAL':'','FIXED':f' {stage}','TIDAL':' Tide','TIMESERIES':' Stage'}[kind]
    tail=(' YES' if gated else ' NO') if gated is not None or route else ''
    return f'O 9 {kind}{parameter}{tail}'+(' S' if route else '')


def source(kind='FREE',gated=None,route=False,units='CFS',direction='reverse'):
    stage=14 if direction=='reverse' else 9.5
    result=(f'[OPTIONS]\nFLOW_UNITS {units}\nFLOW_ROUTING DYNWAVE\nINFILTRATION HORTON\n'
        'ALLOW_PONDING YES\nSTART_DATE 01/01/2004\nEND_DATE 01/01/2004\nEND_TIME 00:10:00\n'
        'REPORT_STEP 00:00:30\nWET_STEP 00:00:30\nDRY_STEP 00:00:30\nROUTING_STEP 5\nVARIABLE_STEP 0\n'
        f'[STORAGE]\nJ 10 5 {0 if direction=="reverse" else 4} FUNCTIONAL 2 1.7 50\n'
        f'[OUTFALLS]\n{row(kind,gated,route,stage)} ; boundary\nO2 0 FREE\n'
        '[CONDUITS]\nP J O 100 .013 0 0\n[XSECTIONS]\nP CIRCULAR 1 0 0 0\n')
    if kind=='TIDAL':result+=f'[CURVES]\nTide TIDAL 0 {stage}\nTide 24 {stage}\n'
    if kind=='TIMESERIES':result+=f'[TIMESERIES]\nStage 0:00 {stage}\nStage 24:00 {stage}\n'
    return result+('[RAINGAGES]\nR INTENSITY 1:00 1 TIMESERIES Rain\n'
        '[TIMESERIES]\nRain 0:00 0\nRain 24:00 0\n[SUBCATCHMENTS]\nS R O2 1 100 100 1 0\n'
        '[SUBAREAS]\nS .01 .1 0 0 100 OUTLET\n[INFILTRATION]\nS 3 .2 4 2 0\n'
        '[REPORT]\nSUBCATCHMENTS ALL\nNODES ALL\nLINKS ALL\n')


def created(kind='FREE',gated=None,route=False,units='CFS',direction='reverse'):
    """Build every resource through public APIs, without parsing a fixture."""
    model=Model();stage=14 if direction=='reverse' else 9.5
    model.update_options(flow_units=units,flow_routing='DYNWAVE',infiltration='HORTON',allow_ponding=True,
        start_date=date(2004,1,1),end_date=date(2004,1,1),end_time=time(0,10),
        report_step=timedelta(seconds=30),wet_step=timedelta(seconds=30),dry_step=timedelta(seconds=30),
        routing_step=timedelta(seconds=5),variable_step=0)
    if kind=='TIDAL':model.curves.add(Curve(id='Tide',kind='TIDAL',points=(CurvePoint(x=0,y=stage),CurvePoint(x=24,y=stage))))
    if kind=='TIMESERIES':model.timeseries.add(InlineTimeSeries(id='Stage',points=tuple(SeriesPoint(time=timedelta(hours=t),value=stage) for t in (0,24))))
    model.timeseries.add(InlineTimeSeries(id='Rain',points=tuple(SeriesPoint(time=timedelta(hours=t),value=0) for t in (0,24))))
    model.nodes.add(n.Storage(id='J',elevation=10,max_depth=5,initial_depth=0 if direction=='reverse' else 4,
        shape=n.FunctionalStorage(coefficient=2,exponent=1.7,constant=50)))
    model.nodes.add(n.Outfall(id='O',elevation=9,boundary=boundary(kind,stage),gated=gated,
        route_to=Ref(collection='swmm:subcatchments',key='S') if route else None))
    model.nodes.add(n.Outfall(id='O2',elevation=0,boundary=n.FreeBoundary()))
    model.links.add(n.Conduit(id='P',inlet=Ref(collection='swmm:nodes',key='J'),outlet=OWNER,
        length=100,roughness=.013,inlet_offset=0,outlet_offset=0,section=g.CrossSection(geometry=g.Circular(diameter=1))))
    model.raingages.add(h.RainGage(id='R',form='INTENSITY',interval=timedelta(hours=1),snow_factor=1,
        source=h.SeriesRainfall(series=Ref(collection='swmm:timeseries',key='Rain'))))
    model.subcatchments.add(h.Subcatchment(id='S',rain_gage=Ref(collection='swmm:raingages',key='R'),
        outlet=Ref(collection='swmm:nodes',key='O2'),area=1,impervious_percent=100,width=100,slope=1,curb_length=0,
        subareas=h.Subareas(impervious_roughness=.01,pervious_roughness=.1,impervious_storage=0,pervious_storage=0,zero_storage_percent=100),
        infiltration=h.Infiltration(parameters=h.Horton(maximum_rate=3,minimum_rate=.2,decay=4,drying_time=2,maximum_volume=0))))
    model.update_report(**{name:ReportSelection(mode='ALL') for name in ('subcatchments','nodes','links')})
    return model


def load(text,strict=True,source_path='outfall-gates.inp'):
    doc=InpDocument.from_bytes(text if isinstance(text,bytes) else text.encode(),source=source_path)
    return Model.from_document(doc,strict=strict)


def portable(model):return Model.from_json_document(model.to_json_document(),strict=True)


def facts(model):
    return tuple((p,model.inspect_field(OWNER,p).semantics.effective) for p in ('elevation','boundary','gated','route_to'))


class OutfallGateTests(unittest.TestCase):
    def test_each_variant_source_create_edit_clear_and_roundtrip(self):
        for variant,cases in CASES.items():
            for kind,gated,route in cases:
                for units in UNITS:
                    with self.subTest(variant=variant,kind=kind,gated=gated,route=route,units=units):
                        text=source(kind,gated,route,units)
                        decorated=text.replace('[OUTFALLS]','[outfalls]\n; 出水口').replace('O2 0 FREE','[OUTFALLS]\nO2 0 FREE')
                        raw=b'\xef\xbb\xbf'+decorated.replace('\n','\r\n').encode()
                        model=load(raw);self.assertEqual(model.to_document().to_bytes(),raw)
                        fresh=created(kind,gated,route,units)
                        self.assertTrue(fresh.validate(for_run=True).is_valid,fresh.validate(for_run=True))
                        self.assertEqual(portable(fresh).nodes['O'],fresh.nodes['O'])
                        for other in (fresh,portable(model),load(model.to_document(normalize=True).text),load(fresh.to_document().text)):
                            self.assertEqual(facts(other),facts(model))
                        model.nodes.update('O',elevation=8,boundary=n.FixedBoundary(stage=13),gated=True,
                            route_to=Ref(collection='swmm:subcatchments',key='S'))
                        for other in (portable(model),load(model.to_document().text)):
                            self.assertEqual(other.nodes['O'],model.nodes['O'])
                        model.nodes.update('O',gated=None,route_to=None)
                        self.assertIsNone(load(model.to_document().text).nodes['O'].gated)
                        self.assertIsNone(portable(model).nodes['O'].route_to)
            EVIDENCE.append(dict(gate='source-create-edit-roundtrip',variant=variant,cases=len(cases)*len(UNITS)))

    def test_each_variant_identity_order_reference_rename_delete_and_rollback(self):
        for variant,cases in CASES.items():
            for kind,gated,route in cases:
                with self.subTest(variant=variant,kind=kind,gated=gated,route=route):
                    model=load(source(kind,gated,route));original=model.to_json_document()
                    with self.assertRaises(RuntimeError):
                        with model.transaction():
                            model.nodes.rename('O','Temporary');model.nodes.update('Temporary',gated=True)
                            raise RuntimeError('rollback')
                    self.assertEqual(model.to_json_document(),original)
                    with self.assertRaises((ValidationError,ValueError)):model.nodes.rename('O','O2')
                    self.assertEqual(model.to_json_document(),original)
                    if kind in ('TIDAL','TIMESERIES'):
                        collection=model.curves if kind=='TIDAL' else model.timeseries
                        name='Tide' if kind=='TIDAL' else 'Stage'
                        with self.assertRaises(ValidationError):collection.remove(name)
                        collection.rename(name,'BoundaryResource')
                        ref=getattr(model.nodes['O'].boundary,'curve' if kind=='TIDAL' else 'series')
                        self.assertEqual(ref.key,'BoundaryResource')
                        self.assertEqual(load(model.to_document().text).nodes['O'].boundary,model.nodes['O'].boundary)
                    if route:
                        with self.assertRaises(ValidationError):model.subcatchments.remove('S')
                        model.subcatchments.rename('S','Receiver')
                        self.assertEqual(model.nodes['O'].route_to.key,'Receiver')
                        self.assertEqual(portable(model).nodes['O'].route_to.key,'Receiver')
                    model.nodes.move('O2',before='O');self.assertEqual(tuple(load(model.to_document().text).nodes),tuple(model.nodes))
                    model.nodes.rename('O','Changed');self.assertEqual(model.links['P'].outlet.key,'Changed')
                    with self.assertRaises(ValidationError):model.nodes.remove('Changed')
                    model.nodes.remove('Changed',cascade=True)
                    self.assertNotIn('P',model.links);self.assertNotIn('Changed',load(model.to_document().text).nodes)
            EVIDENCE.append(dict(gate='identity-references-rollback',variant=variant,cases=len(cases)))

    def test_each_variant_defaults_units_context_and_sources(self):
        for variant,cases in CASES.items():
            for kind,gated,route in cases:
                for units in UNITS:
                    with self.subTest(variant=variant,kind=kind,gated=gated,route=route,units=units):
                        model=load(source(kind,gated,route,units));info=model.inspect_field(OWNER,'gated')
                        self.assertEqual(info.semantics.default.value,False)
                        self.assertEqual(info.semantics.effective.value,bool(gated))
                        self.assertEqual(info.provenance.status,'explicit' if gated is not None or route else 'omitted')
                        info=model.inspect_field(OWNER,'route_to')
                        self.assertEqual(info.provenance.status,'explicit' if route else 'omitted')
                        self.assertIsNone(info.semantics.default.value)
                        if route:self.assertEqual(info.semantics.effective.value,Ref(collection='swmm:subcatchments',key='S'))
                        unit='ft' if units in UNITS[:3] else 'm'
                        self.assertEqual(model.inspect_field(OWNER,'elevation').semantics.unit.value,unit)
                        if kind=='FIXED':
                            info=model.inspect_field(OWNER,('boundary','stage'))
                            self.assertEqual(info.semantics.unit.value,unit)
                            self.assertEqual(info.provenance.declarations[0].tokens[0].raw,'14')
                        changed=model.copy();changed.convert_units('CMS' if units in UNITS[:3] else 'CFS')
                        factor=.3048 if units in UNITS[:3] else 1/.3048
                        self.assertAlmostEqual(changed.nodes['O'].elevation,9*factor,places=10)
                        self.assertEqual(changed.nodes['O'].gated,model.nodes['O'].gated)
                        self.assertEqual(changed.nodes['O'].route_to,model.nodes['O'].route_to)
                        if kind=='FIXED':self.assertAlmostEqual(changed.nodes['O'].boundary.stage,14*factor,places=10)
                        if kind=='TIDAL':self.assertAlmostEqual(changed.curves['Tide'].points[0].y,14*factor,places=10)
                        if kind=='TIMESERIES':self.assertAlmostEqual(changed.timeseries['Stage'].points[0].value,14*factor,places=10)
            EVIDENCE.append(dict(gate='defaults-units-sources',variant=variant,cases=len(cases)*len(UNITS)))

    def test_each_variant_invalid_unknown_and_diagnostic_sources(self):
        for variant,cases in CASES.items():
            for kind,gated,route in cases:
                with self.subTest(variant=variant,kind=kind,gated=gated,route=route):
                    base=source(kind,gated,route);original=row(kind,gated,route)+' ; boundary'
                    invalid=('O','O bad FREE','O 9 FIXED','O 9 FIXED nan','O 9 FREE MAYBE',
                        'O 9 FREE NO Missing','O 9 TIDAL Missing','O 9 TIMESERIES Missing',row(kind,gated,route)+' EXTRA EXTRA EXTRA')
                    for bad in invalid:
                        text=base.replace(original,bad);model=load(text,strict=False)
                        self.assertEqual(model.document.text,text)
                        errors=model.validate().errors;self.assertTrue(errors,(bad,model.validate()))
                        self.assertTrue(any(d.span or d.locations for d in errors),bad)
                        with self.assertRaises(ValidationError):model.to_document()
                    duplicate=load(base+'[OUTFALLS]\no 9 FREE\n',strict=False)
                    self.assertFalse(duplicate.validate().is_valid)
                    unknown=load(base.replace(original,'O 9 FUTURE_BOUNDARY 7'),strict=False)
                    self.assertIn('FUTURE_BOUNDARY',unknown.document.text)
                    self.assertTrue(unknown.support.opaque_records)
                    with self.assertRaises(ValidationError):unknown.nodes.rename('O2','Changed')
                    model=load(base);before=model.to_json_document()
                    for kwargs in ({'gated':'YES'},{'elevation':float('inf')},{'route_to':Ref(collection='swmm:subcatchments',key='Missing')}):
                        with self.assertRaises((ValueError,ValidationError)):
                            with model.transaction():model.nodes.update('O',**kwargs)
                        self.assertEqual(model.to_json_document(),before)
            EVIDENCE.append(dict(gate='invalid-unknown-diagnostics',variant=variant,cases=len(cases)))

    def test_optional_gate_and_route_combinations_preserve_effective_values(self):
        for kind in KINDS:
            for gated in (None,False,True):
                for route in (False,True):
                    for units in UNITS:
                        with self.subTest(kind=kind,gated=gated,route=route,units=units):
                            model=created(kind,gated,route,units)
                            self.assertEqual(portable(model).nodes['O'],model.nodes['O'])
                            for other in (load(model.to_document().text),load(model.to_document(normalize=True).text)):
                                self.assertEqual(facts(other),facts(model))
                            if route and gated is None:self.assertIn(' NO S',model.to_document().text)
        EVIDENCE.append(dict(gate='combinations',boundaries=5,gate_states=3,route_states=2,units=6))

    def test_boundary_stage_extremes_and_resource_roles_are_explicit(self):
        for units in UNITS:
            for stage in (-1,0,8,9,14):
                for kind in ('FIXED','TIDAL','TIMESERIES'):
                    with self.subTest(units=units,stage=stage,kind=kind):
                        model=created(kind,True,True,units)
                        if kind=='FIXED':model.nodes.update('O',boundary=n.FixedBoundary(stage=stage))
                        elif kind=='TIDAL':model.curves.update('Tide',points=(CurvePoint(x=0,y=stage),CurvePoint(x=24,y=stage)))
                        else:model.timeseries.update('Stage',points=tuple(SeriesPoint(time=timedelta(hours=t),value=stage) for t in (0,24)))
                        self.assertTrue(model.validate(for_run=True).is_valid)
                        restored=load(model.to_document().text)
                        self.assertEqual(facts(restored),facts(model))
                        self.assertEqual(portable(model).nodes['O'],model.nodes['O'])
        model=created('TIDAL');model.curves.update('Tide',kind='STORAGE')
        self.assertIn('resource.wrong_purpose',{d.code for d in model.validate().errors})
        model=created('TIMESERIES');model.nodes.update('O',boundary=n.SeriesBoundary(series=Ref(collection='swmm:timeseries',key='Rain')))
        self.assertIn('resource.conflicting_dimensions',{d.code for d in model.validate().errors})
        before=model.to_json_document()
        with self.assertRaises(ValidationError):model.convert_units('CMS')
        self.assertEqual(model.to_json_document(),before)
        EVIDENCE.append(dict(gate='stage-extremes-resource-roles',stages=[-1,0,8,9,14],units=6,roles_checked=True))


if __name__=='__main__':unittest.main()
