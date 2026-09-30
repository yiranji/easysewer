"""Pump/regulator declarations, context-dependent facts and source identity."""

from dataclasses import dataclass, fields, replace
from datetime import timedelta
import unittest

from easysewer.io.inp import InpDocument
from easysewer.model import Model, Ref, Point
from easysewer.model import network as n
from easysewer.model.geometry import CrossSection, Circular
from easysewer.model.values import Offset
from test_regulators_v2 import regulator_model, curve, KINDS
from test_network_fields_v2 import NODE, PIPE
from test_scenario_v2 import portable


def fixture(kind='SIDE', *, units='CFS', routing='DYNWAVE', mode='DEPTH', offset=.2,
            downstream=9., storage=False, surcharge=None, max_depth=0.):
    model=regulator_model(kind,storage=storage)
    model.nodes.update('J',max_depth=max_depth,initial_depth=0,surcharge_depth=surcharge)
    model.nodes.update('O',elevation=downstream)
    model.reinterpret_units(units)
    model.update_options(flow_routing=routing)
    model.reinterpret_link_offsets(mode)
    if type(model.links['P']) is not n.Pump:
        model.links.update('P',**{('crest_height' if type(model.links['P']) is n.Weir else 'offset'):offset})
    return model.to_document().text


def load(text):
    return Model.from_document(InpDocument.from_text(text,source='regulator-source.inp'))


def queries(model):
    paths=[]
    def walk(value,path=()):
        for field in fields(value):
            child=getattr(value,field.name);current=(*path,field.name)
            paths.append(current)
            if hasattr(child,'__dataclass_fields__'):
                walk(child,current)
    walk(model.links['P'])
    return tuple(model.inspect_field(PIPE,path) for path in paths)


class RegulatorFieldTests(unittest.TestCase):
    def test_all_variants_have_explicit_contextual_contracts_and_source(self):
        for kind in KINDS:
            for units in ('CFS','GPM','MGD','CMS','LPS','MLD'):
                with self.subTest(kind=kind,units=units):
                    model=load(fixture(kind,units=units)+'[MAP]\nUNITS METERS\n')
                    for info in queries(model):
                        self.assertIn(info.provenance.status,('explicit','derived','omitted'))
                        self.assertNotEqual(info.semantics.unit.status,'unknown')
                        self.assertIn(info.semantics.effective.status,('known','not_applicable'))
                    self.assertEqual(queries(Model.from_json_document(model.to_json_document(),strict=True)),queries(model))

    def test_short_forms_defaults_and_markers_are_distinct(self):
        base='[JUNCTIONS]\nJ 10\n[OUTFALLS]\nO 9 FREE\n'
        for tail,status in (('', 'omitted'), (' *','explicit')):
            model=load(base+'[PUMPS]\nP J O'+tail+'\n')
            info=model.inspect_field(PIPE,'curve')
            self.assertEqual(info.provenance.status,status)
            self.assertIsNone(info.semantics.effective.value)
            self.assertTrue(model.inspect_field(PIPE,'initially_on').semantics.effective.value)
            self.assertEqual(model.inspect_field(PIPE,'startup_depth').semantics.effective.value,0)
        model=load(base+'[WEIRS]\nP J O TRAPEZOIDAL 0 3 * * * *\n[XSECTIONS]\nP TRAPEZOIDAL 3 2 1 1\n')
        for name,value in (('gated',False),('end_contractions',0.),('end_coefficient',0.),('can_surcharge',True)):
            info=model.inspect_field(PIPE,name)
            self.assertEqual(info.semantics.default.value,value)
            self.assertEqual(info.provenance.declarations[0].role,'marker')
        self.assertEqual(model.inspect_field(PIPE,'end_coefficient').semantics.effective.value,0.)
        for qualifier,status in (('FUNCTIONAL','omitted'),('FUNCTIONAL/DEPTH','explicit'),('FUNCTIONAL/HEAD','explicit')):
            model=load(base+f'[OUTLETS]\nP J O 0 {qualifier} 2 1.4\n')
            info=model.inspect_field(PIPE,('rating','basis'))
            self.assertEqual(info.provenance.status,status)
            self.assertEqual(info.semantics.default.value,'DEPTH')

    def test_crest_crowns_and_pump_bottom_orifice_exclusion(self):
        for kind,height in (('SIDE',1.),('TRANSVERSE',3.),('FUNCTIONAL/HEAD',1.e-6)):
            model=load(fixture(kind,downstream=12.,offset=-1))
            name='crest_height' if kind=='TRANSVERSE' else 'offset'
            self.assertEqual(model.inspect_field(PIPE,name).semantics.effective.value,2.)
            self.assertAlmostEqual(model.inspect_field(NODE,'max_depth').semantics.effective.value,2.+height)
            model.update_options(flow_routing='KINWAVE')
            self.assertEqual(model.inspect_field(PIPE,name).semantics.effective.value,0.)
            self.assertAlmostEqual(model.inspect_field(NODE,'max_depth').semantics.effective.value,height)
        for kind in ('BOTTOM','IDEAL','PUMP2'):
            self.assertEqual(load(fixture(kind,downstream=12.)).inspect_field(NODE,'max_depth').semantics.effective.value,0.)
        model=load(fixture('SIDE',mode='ELEVATION',offset=Offset.NODE_INVERT,downstream=12.))
        self.assertEqual(model.inspect_field(PIPE,'offset').semantics.default.status,'required')
        self.assertEqual(model.inspect_field(PIPE,'offset').semantics.effective.value,2.)
        model.links.update('P',offset=None)
        self.assertEqual(model.inspect_field(PIPE,'offset').semantics.effective.status,'invalid')

    def test_downstream_node_and_multiple_incoming_outgoing_links(self):
        model=load(fixture('SIDE'))
        model.nodes.add(n.Junction(id='K',elevation=11))
        model.links.add(n.Orifice(id='Incoming',inlet=Ref(collection='swmm:nodes',key='K'),outlet=NODE,
            orientation='SIDE',offset=2,coefficient=.65,section=CrossSection(geometry=Circular(diameter=8))))
        self.assertAlmostEqual(model.inspect_field(NODE,'max_depth').semantics.effective.value,1.2)
        model.links.add(n.Orifice(id='Outgoing',inlet=NODE,outlet=Ref(collection='swmm:nodes',key='O'),
            orientation='SIDE',offset=3,coefficient=.65,section=CrossSection(geometry=Circular(diameter=2))))
        self.assertEqual(model.inspect_field(NODE,'max_depth').semantics.effective.value,5.)

    def test_weir_parameters_retained_values_and_hydraulic_applicability(self):
        model=load(fixture('ROADWAY'))
        model.links.update('P',gated=True,end_contractions=2,end_coefficient=4,can_surcharge=False,
                           coefficient_curve=Ref(collection='swmm:curves',key='Cd'),road_width=10,road_surface='PAVED')
        model.curves.add(curve('Cd','WEIR',((0,2),(2,3))))
        model=load(model.to_document().text)
        for name in ('gated','end_contractions','end_coefficient','can_surcharge','coefficient_curve','coefficient'):
            info=model.inspect_field(PIPE,name)
            self.assertEqual(info.semantics.effective.status,'not_applicable')
            self.assertEqual(info.provenance.status,'explicit')
            self.assertTrue(info.provenance.declarations[0].contributes)
        text=fixture('TRANSVERSE').replace('P J O TRANSVERSE 0.2 3.1','P J O TRANSVERSE 0.2 3.1 * * * * ignored ignored')
        model=load(text)
        for name in ('road_width','road_surface'):
            info=model.inspect_field(PIPE,name)
            self.assertEqual(info.semantics.effective.status,'not_applicable')
            self.assertEqual(info.provenance.declarations[0].role,'retained')
            self.assertFalse(info.provenance.declarations[0].contributes)
        model=load(fixture('TRANSVERSE'));model.curves.add(curve('Cd','WEIR',((0,2),(2,3))))
        model.links.update('P',coefficient_curve=Ref(collection='swmm:curves',key='Cd'))
        self.assertEqual(model.inspect_field(PIPE,'coefficient').semantics.effective.status,'not_applicable')
        model=load(fixture('V-NOTCH'));model.links.update('P',end_coefficient=2)
        self.assertEqual(model.inspect_field(PIPE,'end_coefficient').semantics.effective.value,2)

    def test_unit_and_duration_contexts(self):
        for units in ('CFS','GPM','MGD','CMS','LPS','MLD'):
            us=units in ('CFS','GPM','MGD')
            model=load(fixture('FUNCTIONAL/HEAD',units=units))
            info=model.inspect_field(PIPE,('rating','coefficient'))
            self.assertEqual(info.semantics.unit.value,f'{units}/{"ft" if us else "m"}^1.4')
            model=load(fixture('SIDEFLOW',units=units))
            self.assertEqual(model.inspect_field(PIPE,'coefficient').semantics.unit.value,'cfs/ft^2.5' if us else 'cms/m^2.5')
        text=fixture('SIDE').replace('P J O SIDE 0.2 0.65','P J O SIDE 0.2 0.65 NO 0.25')
        model=load(text);info=model.inspect_field(PIPE,'opening_time')
        self.assertEqual(info.value,timedelta(minutes=15))
        self.assertEqual(info.semantics.default.value,timedelta())
        self.assertEqual(info.semantics.unit.value,'s')
        self.assertEqual(info.provenance.declarations[0].tokens[0].raw,'0.25')

    def test_invalid_references_limits_shapes_and_duplicate_sources(self):
        model=load(fixture('PUMP2'));model.links.update('P',startup_depth=1,shutoff_depth=2)
        self.assertEqual(model.inspect_field(PIPE,'startup_depth').semantics.effective.status,'invalid')
        model.links.update('P',curve=Ref(collection='swmm:curves',key='missing'))
        self.assertEqual(model.inspect_field(PIPE,'curve').semantics.effective.status,'invalid')
        model=load(fixture('SIDE'));model.links.update('P',outlet=Ref(collection='swmm:nodes',key='missing'))
        self.assertEqual(model.inspect_field(PIPE,'offset').semantics.effective.status,'invalid')
        model=load(fixture('TRANSVERSE'));model.links.update('P',section=CrossSection(geometry=Circular(diameter=1)))
        self.assertEqual(model.inspect_field(PIPE,'section').semantics.effective.status,'invalid')
        self.assertEqual(model.inspect_field(NODE,'max_depth').semantics.effective.status,'invalid')
        model=load(fixture('SIDE')+'[ORIFICES]\nP J O SIDE 0 1\n')
        self.assertEqual(model.field_provenance(PIPE,'coefficient').status,'unknown')
        model=load(fixture('SIDE')+'[ORIFICES]\nP J O SIDE bad 1\n')
        self.assertEqual(model.field_provenance(PIPE,'opening_time').status,'unknown')

    def test_lifecycle_source_identity_and_map_coordinates(self):
        self.assertEqual(load(fixture()).inspect_field(PIPE,'vertices').semantics.unit.status,'unknown')
        model=load(fixture('TABULAR/HEAD')+'[MAP]\nUNITS METERS\n[VERTICES]\nP 1 2\n')
        before=queries(model)
        with self.assertRaises(RuntimeError):
            with model.transaction():
                model.curves.rename('Rating','Renamed')
                raise RuntimeError('rollback')
        self.assertEqual(queries(model),before)
        path=('rating','curve','key')
        old=model.field_provenance(PIPE,path)
        model.curves.rename('Rating','Renamed');model.nodes.rename('J','Upstream')
        self.assertEqual(model.field_provenance(PIPE,path),old)
        self.assertTrue(model.inspect_field(PIPE,path).changed)
        self.assertEqual(model.inspect_field(PIPE,('vertices',0,'x')).semantics.unit.value,'m')
        self.assertEqual(queries(Model.from_json_document(model.to_json_document(),strict=True)),queries(model))
        self.assertEqual(portable(model).field_provenance(PIPE,path).status,'untracked')
        old_link=model.links['P'];model.links.remove('P');model.links.add(old_link)
        self.assertEqual(model.field_provenance(PIPE,path).status,'created')

    def test_extension_rating_does_not_inherit_native_semantics(self):
        @dataclass(frozen=True,kw_only=True)
        class CustomRating(n.FunctionalRating):
            pass
        model=load(fixture('FUNCTIONAL/HEAD'))
        model.links.update('P',rating=CustomRating(basis='HEAD',coefficient=2,exponent=1.4))
        for owner,path in ((PIPE,'rating'),(PIPE,'offset'),(NODE,'max_depth')):
            self.assertEqual(model.inspect_field(owner,path).semantics.effective.status,'unknown')


if __name__=='__main__':
    unittest.main()
