"""Node input syntax, contextual units/defaults and configured effective facts."""
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import timedelta
import unittest

from easysewer.io.inp import InpDocument
from easysewer.model import Model,Ref,Point,FileReference
from easysewer.model import network as n
from easysewer.model.resources import InlineTimeSeries,FileTimeSeries,SeriesPoint
from test_nodes_v2 import storage_model,divider_model,SHAPES,CURVE
from test_regulators_v2 import curve
from test_network_fields_v2 import NODE,PIPE
from test_scenario_v2 import portable

OUTFALL=Ref(collection='swmm:nodes',key='O')
UNITS=('CFS','GPM','MGD','CMS','LPS','MLD')


def load(text):
    return Model.from_document(InpDocument.from_text(text,source='node-fields.inp'),strict=True)


def storage_source(kind='FUNCTIONAL',tail='',units='CFS',routing='DYNWAVE'):
    model=storage_model();model.reinterpret_units(units);model.update_options(flow_routing=routing)
    text=model.to_document().text
    native='PARABOLIC' if kind=='PARABOLOID' else kind
    text=text.replace('J 10 5 2 FUNCTIONAL 2 1.7 50',f'J 10 5 2 {native} {SHAPES[kind]}{tail}')
    return text+(CURVE if kind=='TABULAR' else '')+'[MAP]\nUNITS METERS\n'


def outfall_model(kind='FIXED'):
    model=storage_model()
    if kind=='TIDAL':
        model.curves.add(curve('Tide','TIDAL',((0,12),(24,12))))
    if kind=='TIMESERIES':
        model.timeseries.add(InlineTimeSeries(id='Stage',points=tuple(SeriesPoint(time=timedelta(hours=t),value=12) for t in (0,24))))
    boundary={'FREE':n.FreeBoundary(),'NORMAL':n.NormalBoundary(),'FIXED':n.FixedBoundary(stage=12),
              'TIDAL':n.TidalBoundary(curve=Ref(collection='swmm:curves',key='Tide')),
              'TIMESERIES':n.SeriesBoundary(series=Ref(collection='swmm:timeseries',key='Stage'))}[kind]
    model.nodes.update('O',boundary=boundary)
    return model


def queries(model,owner=NODE):
    paths=[]
    def walk(value,path=()):
        if is_dataclass(value):
            for f in fields(value):
                p=(*path,f.name);paths.append(p);walk(getattr(value,f.name),p)
        elif isinstance(value,tuple):
            for i,v in enumerate(value):walk(v,(*path,i))
    walk(model.nodes[owner.key])
    return tuple(model.inspect_field(owner,p) for p in paths)


class NodeFieldTests(unittest.TestCase):
    def assert_contracts(self,model,owner=NODE):
        before=queries(model,owner)
        for info in before:
            self.assertIn(info.provenance.status,('explicit','derived','omitted'),info.path)
            self.assertNotEqual(info.semantics.unit.status,'unknown',info.path)
            self.assertIn(info.semantics.effective.status,('known','not_applicable'),info.path)
        self.assertEqual(queries(Model.from_json_document(model.to_json_document(),strict=True),owner),before)

    def test_storage_all_shapes_six_units_and_variable_optional_tails(self):
        for kind in SHAPES:
            for units in UNITS:
                for tail in ('',' 0',' .5 .7',' .5 .7 .2',' .5 .7 3 .2 .3',' 0 0 0 0 0'):
                    with self.subTest(kind=kind,units=units,tail=tail):
                        self.assert_contracts(load(storage_source(kind,tail,units)))

    def test_divider_all_laws_routing_modes_and_units(self):
        for kind in ('OVERFLOW','CUTOFF','TABULAR','WEIR'):
            for routing in ('STEADY','KINWAVE','DYNWAVE'):
                for units in UNITS:
                    with self.subTest(kind=kind,routing=routing,units=units):
                        model=divider_model(kind,routing);model.reinterpret_units(units)
                        model=load(model.to_document().text+'[MAP]\nUNITS METERS\n')
                        self.assert_contracts(model)
                        self.assertEqual(model.inspect_field(NODE,'law').semantics.effective.status,
                                         'not_applicable' if routing=='DYNWAVE' else 'known')
                        if kind=='WEIR':
                            self.assertEqual(model.inspect_field(NODE,('law','coefficient')).semantics.unit.value,
                                             f'{units}/{"ft" if units in UNITS[:3] else "m"}^1.5')

    def test_outfall_all_boundaries_units_and_consumer_references(self):
        for kind in ('FREE','NORMAL','FIXED','TIDAL','TIMESERIES'):
            for units in UNITS:
                model=outfall_model(kind);model.reinterpret_units(units)
                model=load(model.to_document().text+'[MAP]\nUNITS FEET\n')
                self.assert_contracts(model,OUTFALL)
                self.assertFalse(model.inspect_field(OUTFALL,'gated').semantics.effective.value)
                self.assertIsNone(model.inspect_field(OUTFALL,'route_to').semantics.default.value)
                if kind=='FIXED':
                    info=model.inspect_field(OUTFALL,('boundary','stage'))
                    self.assertEqual(info.semantics.effective.value,12)
                    self.assertEqual(info.provenance.declarations[0].tokens[0].raw,'12')
                    self.assertEqual(info.semantics.unit.value,'ft' if units in UNITS[:3] else 'm')

    def test_source_positions_for_storage_and_discarded_cylinder_parameter(self):
        for kind in ('CYLINDRICAL','TABULAR'):
            text=storage_source(kind,' .5 .7 3 .2 .3')
            if kind=='CYLINDRICAL':text=text.replace('CYLINDRICAL 10 8 0','CYLINDRICAL 10 8 9')
            model=load(text)
            for path,token in ((('surcharge_depth',),'.5'),(('evaporation_fraction',),'.7'),
                               (('seepage','suction'),'3'),(('seepage','conductivity'),'.2'),(('seepage','initial_deficit'),'.3')):
                self.assertEqual(model.field_provenance(NODE,path).declarations[0].tokens[0].raw,token)
            if kind=='CYLINDRICAL':
                declarations=model.field_provenance(NODE,'shape').declarations
                self.assertTrue(declarations[0].contributes)
                self.assertEqual([t.raw for t in declarations[0].tokens],['CYLINDRICAL','10','8'])
                self.assertEqual(declarations[1].role,'retained');self.assertFalse(declarations[1].contributes)
                self.assertEqual(declarations[1].tokens[0].raw,'9')

    def test_defaults_zero_seepage_and_short_divider_inputs(self):
        model=load(storage_source())
        for name,value in (('surcharge_depth',0.),('evaporation_fraction',0.),('seepage',None)):
            info=model.inspect_field(NODE,name)
            self.assertEqual(info.provenance.status,'omitted');self.assertIsNone(info.value)
            self.assertEqual(info.semantics.effective.value,value)
            self.assertEqual(info.semantics.default.value,value)
        model=load(storage_source(tail=' 0 0 3 0 .3'))
        self.assertIsNone(model.inspect_field(NODE,'seepage').semantics.effective.value)
        self.assertEqual(model.inspect_field(NODE,('seepage','conductivity')).semantics.effective.value,0)
        for name in ('suction','initial_deficit'):
            info=model.inspect_field(NODE,('seepage',name))
            self.assertEqual(info.semantics.effective.status,'not_applicable')
            self.assertEqual(info.provenance.status,'explicit')
        for law,params in (('OVERFLOW',''),('CUTOFF',' .4'),('WEIR',' .3 2 1.5'),('TABULAR',' Div')):
            model=load(f'[DIVIDERS]\nJ 10 * {law}{params}\n'+
                       ('[CURVES]\nDiv DIVERSION 0 0\nDiv 1 .5\n' if law=='TABULAR' else ''))
            for name in ('max_depth','initial_depth','surcharge_depth','ponded_area'):
                self.assertEqual(model.inspect_field(NODE,name).semantics.default.value,0.)
                self.assertEqual(model.field_provenance(NODE,name).status,'omitted')
            info=model.inspect_field(NODE,'diverted_link')
            self.assertEqual(info.provenance.declarations[0].role,'marker')
            self.assertEqual(info.semantics.effective.status,'invalid')

    def test_crown_expansion_and_initial_depth_bounds(self):
        for surcharge,maximum in ((None,.5),(0,.5),(.2,1.)):
            model=storage_model(surcharge_depth=surcharge);model.nodes.update('J',max_depth=.5,initial_depth=0)
            self.assertEqual(model.inspect_field(NODE,'max_depth').semantics.effective.value,maximum)
            model.nodes.update('J',initial_depth=maximum+(surcharge or 0)+.1)
            self.assertEqual(model.inspect_field(NODE,'initial_depth').semantics.effective.status,'invalid')
        model=divider_model();model.nodes.update('J',max_depth=None,initial_depth=0)
        self.assertEqual(model.inspect_field(NODE,'max_depth').semantics.effective.value,3.)
        model.nodes.update('J',initial_depth=4)
        self.assertEqual(model.inspect_field(NODE,'initial_depth').semantics.effective.status,'invalid')

    def test_invalid_resources_relations_and_extensions(self):
        model=divider_model('WEIR','DYNWAVE');model.nodes.update('J',law=n.WeirDivider(minimum_flow=100,height=1,coefficient=1))
        self.assertEqual(model.inspect_field(NODE,'law').semantics.effective.status,'invalid')
        model.nodes.update('J',diverted_link=Ref(collection='swmm:links',key='missing'))
        self.assertEqual(model.inspect_field(NODE,'diverted_link').semantics.effective.status,'invalid')
        model=divider_model('OVERFLOW');link=model.links['D'];model.links.update('D',inlet=link.outlet,outlet=link.inlet)
        self.assertEqual(model.inspect_field(NODE,'diverted_link').semantics.effective.status,'invalid')
        model=outfall_model('TIDAL');model.curves.update('Tide',kind='STORAGE')
        self.assertEqual(model.inspect_field(OUTFALL,'boundary').semantics.effective.status,'invalid')
        model=outfall_model('TIMESERIES');model.nodes.update('O',boundary=n.SeriesBoundary(series=Ref(collection='swmm:timeseries',key='missing')))
        self.assertEqual(model.inspect_field(OUTFALL,('boundary','series')).semantics.effective.status,'invalid')
        @dataclass(frozen=True,kw_only=True)
        class CustomShape(n.FunctionalStorage):pass
        model=storage_model(CustomShape(coefficient=1,exponent=1,constant=1))
        self.assertEqual(model.inspect_field(NODE,'shape').semantics.effective.status,'unknown')
        model=storage_model(n.FunctionalStorage(coefficient=1,exponent=-1,constant=1))
        self.assertEqual(model.inspect_field(NODE,('shape','exponent')).semantics.effective.status,'invalid')

    def test_source_identity_rename_rollback_and_map_units(self):
        model=load(storage_source('TABULAR')+'[COORDINATES]\nJ 1 2\n[POLYGONS]\nJ 3 4\nJ 3 4\n')
        before=queries(model)
        with self.assertRaises(RuntimeError):
            with model.transaction():
                model.curves.rename('Area','Renamed');raise RuntimeError('rollback')
        self.assertEqual(queries(model),before)
        source=model.field_provenance(NODE,('shape','curve','key'))
        model.curves.rename('Area','Renamed')
        self.assertEqual(model.field_provenance(NODE,('shape','curve','key')),source)
        self.assertTrue(model.inspect_field(NODE,('shape','curve','key')).changed)
        self.assertEqual(model.inspect_field(NODE,('polygon',1,'x')).semantics.unit.value,'m')
        self.assertEqual(queries(Model.from_json_document(model.to_json_document(),strict=True)),queries(model))
        self.assertEqual(portable(model).field_provenance(NODE,'shape').status,'untracked')
        self.assertEqual(storage_model().inspect_field(NODE,'position').semantics.unit.status,'unknown')
        for extra in ('J 10 5 2 FUNCTIONAL 2 1.7 50','J bad 5 2 FUNCTIONAL 2 1.7 50'):
            bad=Model.from_document(InpDocument.from_text(storage_source()+'[STORAGE]\n'+extra+'\n'))
            self.assertEqual(bad.field_provenance(NODE,'evaporation_fraction').status,'unknown')

    def test_outfall_route_identity_external_series_and_extension_boundaries(self):
        from test_hydrology_v2 import hydrology_model
        model=hydrology_model();model.nodes.update('O',route_to=Ref(collection='swmm:subcatchments',key='S'))
        model=load(model.to_document().text)
        source=model.field_provenance(OUTFALL,('route_to','key'))
        self.assertEqual(source.status,'explicit')
        model.subcatchments.rename('S','Receiver')
        self.assertEqual(model.field_provenance(OUTFALL,('route_to','key')),source)
        self.assertEqual(model.inspect_field(OUTFALL,'route_to').semantics.effective.value.key,'Receiver')
        model.nodes.update('O',route_to=Ref(collection='swmm:subcatchments',key='missing'))
        self.assertEqual(model.inspect_field(OUTFALL,'route_to').semantics.effective.status,'invalid')
        model=outfall_model();model.timeseries.add(FileTimeSeries(id='External',file=FileReference(path='not-present.dat',direction='input')))
        model.nodes.update('O',boundary=n.SeriesBoundary(series=Ref(collection='swmm:timeseries',key='External')))
        self.assertEqual(model.inspect_field(OUTFALL,'boundary').semantics.effective.status,'known')
        self.assertEqual(model.inspect_field(OUTFALL,('boundary','series')).semantics.effective.value.key,'External')
        @dataclass(frozen=True,kw_only=True)
        class CustomBoundary(n.FixedBoundary):pass
        model.nodes.update('O',boundary=CustomBoundary(stage=12))
        self.assertEqual(model.inspect_field(OUTFALL,'boundary').semantics.effective.status,'unknown')


if __name__=='__main__':unittest.main()
