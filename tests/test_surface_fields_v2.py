"""Surface field contracts and original stateful/overridden token sources."""
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import timedelta
import unittest

from easysewer.model import Model, Ref
from easysewer.model import surface as s
from easysewer.model.geometry import CrossSection, Irregular, Circular
from easysewer.io.inp import InpDocument
from easysewer.validation import ValidationError
from test_surface_v2 import INLETS, transect
from test_native_v2_surface import surface_network
from test_options_v2 import network

UNITS=('CFS','GPM','MGD','CMS','LPS','MLD')
GRATES=('P_BAR-50','P_BAR-50X100','P_BAR-30','CURVED_VANE','TILT_BAR-45','TILT_BAR-30','RETICULINE')
KINDS=('G','GG','C','S','DG','DC','Combo','CD','CR',*GRATES[1:])
ref=lambda namespace,key:Ref(collection='swmm:'+namespace,key=key)


def load(text,strict=True):
    return Model.from_document(InpDocument.from_text(text,source='surface-fields.inp'),strict=strict)


def fixture(kind='GG',units='CFS',extra=False):
    if kind in ('transect','inherited','zero-factors'):
        m=network();m.reinterpret_units(units);m.update_options(report_step=timedelta(minutes=1))
        if kind=='inherited':
            source='[TRANSECTS]\nNC 0 0 .02\nX1 T1 3 0 10 0 0 4 1.5 .3\nGR 2 0 0 5 2 10\nNC 0 .05 0\nX1 T 3 0 10 0 0 1 2 .4\nGR 3 0 0 5\nGR 3 10\n[REPORT]\n'
            for row in load(source).transects.values():m.transects.add(row)
        else:
            row=transect()
            if kind=='zero-factors':row=replace(row,meander_factor=0,width_factor=0,elevation_offset=0)
            m.transects.add(row)
        m.links.update('P',section=CrossSection(geometry=Irregular(transect=ref('transects','T'))))
        text=m.to_document().text
        if kind=='inherited':
            text='\n'.join(line.content for line in m.to_document().lines if line.section!='TRANSECTS')+'\n'+source
        return text+'[DWF]\nJ FLOW .5\n'
    designs=load(INLETS)
    design=s.GrateInlet(kind='GRATE',length=2,width=1,grate=s.StandardGrate(kind=kind)) if kind in GRATES else designs.inlets[kind].design
    if type(design) is s.CustomInlet:
        design=replace(design,curve=ref('curves','Rating' if kind=='CR' else 'Diversion'))
    if not extra:
        if type(design) is s.GrateInlet and type(design.grate) is s.GenericGrate:
            design=replace(design,grate=replace(design.grate,splash_velocity=None))
        if type(design) is s.CurbInlet:design=replace(design,throat=None)
        if type(design) is s.CombinationInlet:design=replace(design,curb=replace(design.curb,throat=None))
    shape='RECT_OPEN' if getattr(design,'kind','').startswith('DROP_') else 'STREET'
    m=surface_network(design,shape=shape,sides=1 if extra else 2,placement='ON_SAG' if extra else 'ON_GRADE')
    m.reinterpret_units(units)
    m.update_options(report_step=timedelta(minutes=1))
    if not extra:
        if 'Road' in m.streets:m.streets.update('Road',gutter_depression=None,gutter_width=None,sides=None,backing_width=None,backing_slope=None,backing_roughness=None)
        m.inlet_usage.update('P',count=None,percent_clogged=None,maximum_flow=None,local_depression=None,local_width=None,placement=None)
    return m.to_document().text+'[DWF]\nJ FLOW .5\n'


def queries(m):
    result=[]
    def walk(owner,value,path=()):
        if is_dataclass(value):
            for f in fields(value):
                p=path+(f.name,);result.extend((m.inspect_field(owner,p),m.field_provenance(owner,p)))
                walk(owner,getattr(value,f.name),p)
        elif isinstance(value,tuple):
            for i,child in enumerate(value):
                p=path+(i,);result.extend((m.inspect_field(owner,p),m.field_provenance(owner,p)));walk(owner,child,p)
    for name in ('transects','streets','inlets','inlet_usage'):
        for key,row in m.collection('swmm:'+name).items():walk(ref(name,key),row)
    return tuple(result)


def materialize(m):
    for name,names in (('transects',('meander_factor','width_factor')),('streets',('gutter_depression','gutter_width','sides','backing_width','backing_slope','backing_roughness')),
                       ('inlets',('design',)),('inlet_usage',('count','percent_clogged','maximum_flow','local_depression','local_width','placement'))):
        for key in tuple(m.collection('swmm:'+name)):
            changes={}
            for field in names:
                fact=m.inspect_field(ref(name,key),field).semantics.effective
                if fact.status=='known':changes[field]=fact.value
            m.collection('swmm:'+name).update(key,**changes)


class SurfaceFieldTests(unittest.TestCase):
    def test_variants_units_optional_tails_and_json(self):
        for kind in (*KINDS,'transect','inherited','zero-factors'):
            for units in UNITS:
                for extra in (False,True) if kind in KINDS else (False,):
                    with self.subTest(kind=kind,units=units,extra=extra):
                        m=load(fixture(kind,units,extra));before=queries(m)
                        for info in before[::2]:
                            self.assertIn(info.semantics.effective.status,('known','not_applicable'),info.path)
                            self.assertNotEqual(info.semantics.unit.status,'unknown',info.path)
                            self.assertNotEqual(info.provenance.status,'unknown',info.path)
                        self.assertEqual(queries(Model.from_json_document(m.to_json_document(),strict=True)),before)

    def test_required_slots_zero_sentinels_and_nominal_offset_units(self):
        for units in UNITS:
            m=load(fixture('zero-factors',units));owner=ref('transects','T')
            for name in ('meander_factor','width_factor'):
                info=m.inspect_field(owner,name)
                self.assertEqual(info.value,0);self.assertEqual(info.semantics.default.status,'required')
                self.assertEqual(info.semantics.effective.value,1)
            info=m.inspect_field(owner,'elevation_offset')
            self.assertEqual(info.semantics.unit.value,'ft' if units in UNITS[:3] else 'm')
            self.assertIn('2 times',info.semantics.unit.reason)
        m=load(fixture('transect'));before=m.transects['T']
        m.convert_units('CMS');self.assertAlmostEqual(m.transects['T'].elevation_offset,before.elevation_offset*.3048**2)
        self.assertEqual(m.transects['T'].width_factor,1)

    def test_nc_inheritance_cross_record_dependencies_and_multiple_gr_pairs(self):
        m=load(fixture('inherited'));owner=ref('transects','T')
        self.assertEqual(m.inspect_field(owner,('roughness','channel')).semantics.effective.value,.04)
        source=m.field_provenance(owner,('roughness','channel'))
        active=[(d.tokens[0].value,d.role,d.source_owners) for d in source.declarations if d.contributes]
        self.assertTrue(any(v=='4' and role=='derived' and ref('transects','T1').canonical in owners for v,role,owners in active))
        self.assertTrue(any(v=='.02' and role=='derived' for v,role,_ in active))
        self.assertTrue(any(v=='0' and role=='marker' for v,role,_ in active))
        points=[m.field_provenance(owner,('stations',i,'station')).declarations[0] for i in range(3)]
        self.assertEqual(points[0].line,points[1].line);self.assertNotEqual(points[1].line,points[2].line)
        self.assertEqual([d.tokens[0].value for d in points],['0','5','10'])
        first=ref('transects','T1');left=m.field_provenance(first,('roughness','left'))
        self.assertTrue(any(d.role=='derived' and d.tokens[0].value=='.02' for d in left.declarations))
        self.assertEqual(queries(Model.from_json_document(m.to_json_document(),strict=True)),queries(m))

    def test_duplicate_components_tail_reset_usage_and_ignored_tokens(self):
        source='[INLETS]\nI GRATE 2 1 GENERIC .7 4\nI CURB 4 .5 HORIZONTAL\nI GRATE 3 2 GENERIC .8\nI CURB 5 .6\nD DROP_CURB 2 .4 ignored\nS GRATE 2 1 P_BAR-50 junk junk\n[INLET_USAGE]\nP I N 3 20 4 .1 2 ON_SAG\nP I N\n[STREETS]\nRoad 10 .5 2 .016 0 0 2 0 junk 99\n'
        m=load(source,strict=False);owner=ref('inlets','I')
        path=('design','grate','grate','splash_velocity')
        self.assertEqual(m.inspect_field(owner,path).semantics.effective.value,0)
        self.assertTrue(all(not d.contributes for d in m.field_provenance(owner,path).declarations))
        self.assertEqual([d.contributes for d in m.field_provenance(owner,('design','curb','length')).declarations],[False,True])
        self.assertEqual(m.inspect_field(owner,('design','curb','throat')).semantics.effective.value,'VERTICAL')
        self.assertEqual(m.inspect_field(ref('inlets','D'),('design','throat')).semantics.effective.status,'not_applicable')
        self.assertTrue(all(not d.contributes for d in m.field_provenance(ref('inlets','D'),('design','throat')).declarations))
        self.assertTrue(all(not d.contributes for d in m.field_provenance(ref('inlet_usage','P'),'count').declarations))
        self.assertEqual(m.inspect_field(ref('streets','Road'),'backing_roughness').semantics.effective.status,'not_applicable')
        self.assertEqual(m.field_provenance(ref('streets','Road'),'backing_slope').declarations[0].tokens[0].value,'junk')
        self.assertEqual(queries(Model.from_json_document(m.to_json_document())),queries(m))

    def test_missing_references_curve_kind_extensions_and_shape_rejection(self):
        m=load('[INLET_USAGE]\nP I N\n',strict=False);self.assertEqual(m.inspect_field(ref('inlet_usage','P'),'count').semantics.effective.status,'invalid')
        m=load(fixture('G'));m.links.update('P',section=CrossSection(geometry=Circular(diameter=2)))
        self.assertEqual(m.inspect_field(ref('inlet_usage','P'),'count').semantics.effective.status,'invalid')
        m=load(fixture('CD'));m.curves.update('Diversion',kind='CONTROL')
        self.assertEqual(m.inspect_field(ref('inlets','I'),'design').semantics.effective.status,'invalid')
        @dataclass(frozen=True,kw_only=True)
        class FutureGrate(s.StandardGrate): pass
        m=load(fixture('G'));m.inlets.update('I',design=replace(m.inlets['I'].design,grate=FutureGrate(kind='P_BAR-50')))
        self.assertEqual(m.inspect_field(ref('inlets','I'),'design').semantics.effective.status,'unknown')

    def test_rename_rollback_declared_values_and_automatic_placement(self):
        m=load(fixture('GG'));owner=ref('inlet_usage','P');before=m.field_provenance(owner,'link')
        self.assertEqual(m.inspect_field(owner,'placement').semantics.effective.value,'AUTOMATIC')
        m.links.rename('P','RoadLink');after=m.field_provenance(ref('inlet_usage','RoadLink'),'link')
        self.assertEqual(after.original,before.original);self.assertEqual(after.declarations,before.declarations)
        before=queries(m)
        with self.assertRaises(ValidationError):m.streets.remove('Road')
        self.assertEqual(queries(m),before)
        materialize(m);self.assertEqual(m.inlet_usage['RoadLink'].placement,'AUTOMATIC')
        self.assertEqual(m.inlets['I'].design.grate.splash_velocity,0)

    def test_malformed_state_is_unclaimed_and_finalization_dependencies(self):
        bad='[TRANSECTS]\nNC .03 .04 .02\nX1 T 3 0 10 0 0 0 0 0\nGR 2 0 0\n[REPORT]\n'
        m=Model.from_document(InpDocument.from_text(bad));self.assertFalse(m.transects)
        self.assertEqual(m.document.text,bad)
        source='[TRANSECTS]\nNC .03 .04 .02\nX1 A 3 0 10 0 0 4 1 0\nGR 2 0 0 5 2 10\n[REPORT]\n[TRANSECTS]\nNC 0 0 0\nX1 B 3 0 10 0 0 1 1 0\nGR 2 0 0 5 2 10\n[REPORT]\n'
        m=load(source);self.assertEqual(m.transects['B'].roughness.channel,.08)
        self.assertTrue(any(d.role=='derived' and d.tokens[0].value=='4' for d in m.field_provenance(ref('transects','B'),('roughness','channel')).declarations))
        self.assertEqual(queries(Model.from_json_document(m.to_json_document(),strict=True)),queries(m))


if __name__=='__main__':unittest.main()
