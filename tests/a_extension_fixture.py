"""Independent A experiment: new parameter types using existing SWMM physics."""
from dataclasses import dataclass, replace
from datetime import date, time, timedelta
from typing import ClassVar

from easysewer.io.inp.geometry import CrossSectionCodec
from easysewer.io.inp.network import NetworkCodec, default_schema
from easysewer.io.json import JsonField, JsonType
from easysewer.model import Model, Ref
from easysewer.model import geometry as g, network as n
from easysewer.model.fields import number, reference
from easysewer.model.resources import Curve, CurvePoint
from easysewer.model.report import ReportSelection
from easysewer.model.units import UnitTransform
from easysewer.schema.structured import ModelSchema


@dataclass(frozen=True, kw_only=True)
class SlopeTriangle(g.Geometry):
    kind: ClassVar[str] = 'TRIANGULAR'
    depth: float = number('depth', positive=True)
    side_slope: float = number('run/rise', positive=True)


@dataclass(frozen=True, kw_only=True)
class SquareLaw(n.OutletRating):
    coefficient: float = number('outlet_coefficient', minimum=0)


@dataclass(frozen=True, kw_only=True)
class CurveLaw(n.OutletRating):
    curve: Ref = reference('swmm:curves')


class TriangleCodec(CrossSectionCodec):
    def parse(self, values):
        section = super().parse(values)
        shape = section.geometry
        if type(shape) is g.Triangular:
            section = replace(section, geometry=SlopeTriangle(depth=shape.full_depth,
                side_slope=shape.top_width/(2*shape.full_depth)))
        return section

    def format(self, section):
        if type(section.geometry) is SlopeTriangle:
            value = section.geometry
            section = replace(section, geometry=g.Triangular(full_depth=value.depth,
                top_width=2*value.depth*value.side_slope))
        return super().format(section)

    def field_layout(self, values, *, regulator=False):
        coverage, assignments = super().field_layout(values, regulator=regulator)
        if values[0].upper() != 'TRIANGULAR':
            return coverage, assignments
        def path(value):
            return {('geometry', 'full_depth'): ('geometry', 'depth'),
                    ('geometry', 'top_width'): ('geometry', 'side_slope')}.get(value, value)
        assignments = tuple((path(p), (1, 2) if p == ('geometry', 'top_width') else indexes,
            'derived' if p == ('geometry', 'top_width') else role, contributes)
            for p, indexes, role, contributes in assignments)
        return tuple(path(p) for p in coverage), assignments


def square_units(value, context):
    return replace(value, coefficient=value.coefficient*context.number(1, 'flow')/context.number(1, 'length')**2)


class ExtensionNetworkCodec(NetworkCodec):
    unit_transforms = NetworkCodec.unit_transforms + (UnitTransform(value_type=SquareLaw, convert=square_units),)

    def __init__(self):
        super().__init__(cross_sections=TriangleCodec())

    @staticmethod
    def project(store):
        result = store.clone()
        links = result.collection('swmm:links')
        for key, row in tuple(links.items()):
            if type(row) is n.Outlet:
                rating = row.rating
                if type(rating) is SquareLaw:
                    links.replace(key, replace(row, rating=n.FunctionalRating(basis=rating.basis,
                        coefficient=rating.coefficient, exponent=2)))
                elif type(rating) is CurveLaw:
                    links.replace(key, replace(row, rating=n.TabularRating(basis=rating.basis, curve=rating.curve)))
        return result

    def decode(self, document, profile):
        decoded = super().decode(document, profile)
        records, quadratic = [], set()
        for entry in decoded.value.records:
            row = entry.value
            if type(row) is n.Outlet:
                rating = row.rating
                if type(rating) is n.FunctionalRating and rating.exponent == 2:
                    row = replace(row, rating=SquareLaw(basis=rating.basis, coefficient=rating.coefficient))
                    quadratic.add(Ref(collection=entry.collection, key=row.id).canonical)
                elif type(rating) is n.TabularRating:
                    row = replace(row, rating=CurveLaw(basis=rating.basis, curve=rating.curve))
            records.append(replace(entry, value=row))
        # Exponent 2 is fixed by SquareLaw's type, not a mutable field.
        def retained(binding):
            return not (binding.owner.canonical in quadratic and binding.path == ('rating', 'exponent'))
        value = replace(decoded.value, records=tuple(records),
            field_bindings=tuple(b for b in decoded.value.field_bindings if retained(b)),
            field_coverage=tuple(b for b in decoded.value.field_coverage if retained(b)))
        return replace(decoded, value=value)

    def encode(self, store, profile):
        return super().encode(self.project(store), profile)

    def validate(self, store, profile):
        return super().validate(self.project(store), profile)

    def validate_run(self, store, profile):
        return super().validate_run(self.project(store), profile)

    def resource_uses(self, store, profile):
        return super().resource_uses(self.project(store), profile)


def extension_schema():
    original, result = default_schema(), ModelSchema()
    for descriptor, codec in original.bindings:
        result.register(descriptor, ExtensionNetworkCodec() if descriptor.key == 'swmm:network' else codec)
    result.register_json(*original.json_types.declarations)
    field = lambda name, shape: JsonField(name=name, attribute=name, shape=shape)
    result.register_json(
        JsonType(key='test:a-slope-triangle', value_type=SlopeTriangle, bases=('swmm:geometry.geometry',),
            fields=(field('depth', ('number',)), field('side_slope', ('number',)))),
        JsonType(key='test:a-square-law', value_type=SquareLaw, bases=('swmm:network.outlet_rating',),
            fields=(field('basis', ('literal', 'HEAD', 'DEPTH')), field('coefficient', ('number',)))),
        JsonType(key='test:a-curve-law', value_type=CurveLaw, bases=('swmm:network.outlet_rating',),
            fields=(field('basis', ('literal', 'HEAD', 'DEPTH')), field('curve', ('object', 'core:ref')))),
    )
    return result


def model():
    result = Model(schema=extension_schema())
    result.update_options(flow_units='CMS', flow_routing='DYNWAVE', start_date=date(2020, 1, 1),
        end_date=date(2020, 1, 1), start_time=time(0), end_time=time(0, 5),
        report_step=timedelta(seconds=15), routing_step=timedelta(seconds=1), variable_step=0)
    result.update_report(nodes=ReportSelection(mode='ALL'), links=ReportSelection(mode='ALL'))
    result.nodes.add(n.Storage(id='S', elevation=1, max_depth=5, initial_depth=1.5,
        shape=n.FunctionalStorage(coefficient=0, exponent=1, constant=1000)))
    result.nodes.add(n.Junction(id='J', elevation=.5, max_depth=5, initial_depth=0))
    result.nodes.add(n.Outfall(id='O', elevation=0, boundary=n.FreeBoundary()))
    result.nodes.add(n.Outfall(id='O2', elevation=0, boundary=n.FreeBoundary()))
    ref = lambda key: Ref(collection='swmm:nodes', key=key)
    result.links.add(n.Conduit(id='P', inlet=ref('S'), outlet=ref('J'), length=50, roughness=.013,
        inlet_offset=0, outlet_offset=0, section=g.CrossSection(geometry=SlopeTriangle(depth=2, side_slope=1))))
    result.links.add(n.Outlet(id='Q', inlet=ref('J'), outlet=ref('O'), offset=0,
        rating=SquareLaw(basis='HEAD', coefficient=.4)))
    result.curves.add(Curve(id='Rating', kind='RATING', points=tuple(CurvePoint(x=x, y=y)
        for x, y in ((0, 0), (.5, .1), (1, .3), (3, 1.5)))))
    result.links.add(n.Outlet(id='T', inlet=ref('J'), outlet=ref('O2'), offset=0,
        rating=CurveLaw(basis='DEPTH', curve=Ref(collection='swmm:curves', key='Rating'))))
    return result


ORACLE = '''[OPTIONS]
FLOW_UNITS CMS
FLOW_ROUTING DYNWAVE
START_DATE 01/01/2020
START_TIME 00:00
END_DATE 01/01/2020
END_TIME 00:05
REPORT_STEP 00:00:15
ROUTING_STEP 1
VARIABLE_STEP 0
[STORAGE]
S 1 5 1.5 FUNCTIONAL 0 1 1000
[JUNCTIONS]
J .5 5 0
[OUTFALLS]
O 0 FREE
O2 0 FREE
[CONDUITS]
P S J 50 .013 0 0
[XSECTIONS]
P TRIANGULAR 2 4 0 0
[OUTLETS]
Q J O 0 FUNCTIONAL/HEAD .4 2
T J O2 0 TABULAR/DEPTH Rating
[CURVES]
Rating RATING 0 0
Rating .5 .1
Rating 1 .3
Rating 3 1.5
[REPORT]
NODES ALL
LINKS ALL
'''
