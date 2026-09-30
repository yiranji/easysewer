"""Layered LID definitions and ordered, independently identified deployments."""

from dataclasses import dataclass, replace
from typing import ClassVar, Literal

from ..validation import Diagnostic, DiagnosticSubject, Severity, ValidationReport
from .fields import number, reference, validate_fields
from .identity import Ref, canonical_key, validate_identifier
from .network import Entity
from .options import get_options
from .pollutant_units import PollutantUnitConversion, PollutantUnitTransform
from .store import CollectionSpec
from .units import UnitContext, UnitTransform
from .usage import ResourceUse
from .values import FileReference

LID_KINDS = ('BC', 'RG', 'GR', 'IT', 'PP', 'RB', 'RD', 'VS')


@dataclass(frozen=True, kw_only=True)
class LidSurface:
    storage_depth: float = number('rain_depth', minimum=0)
    vegetation_fraction: float = number('ratio', minimum=0)
    roughness: float = number('manning', minimum=0)
    slope: float = number('percent', minimum=0)
    side_slope: float = number('run/rise', minimum=0)

    def validate_local(self):
        if self.vegetation_fraction >= 1:
            yield Diagnostic(code='lid.vegetation', message='Vegetation must occupy less than the whole surface volume')


@dataclass(frozen=True, kw_only=True)
class LidPavement:
    thickness: float = number('rain_depth', minimum=0)
    void_ratio: float = number('ratio', minimum=0)
    impervious_fraction: float = number('ratio', minimum=0, maximum=1)
    permeability: float = number('rain_intensity', minimum=0)
    clogging_factor: float = number('ratio', minimum=0)
    regeneration_days: float | None = number('days', None, minimum=0)
    regeneration_fraction: float | None = number('ratio', None, minimum=0, maximum=1)


@dataclass(frozen=True, kw_only=True)
class LidSoil:
    thickness: float = number('rain_depth', minimum=0)
    porosity: float = number('ratio', minimum=0, maximum=1)
    field_capacity: float = number('ratio', minimum=0, maximum=1)
    wilting_point: float = number('ratio', minimum=0, maximum=1)
    conductivity: float = number('rain_intensity', minimum=0)
    conductivity_slope: float = number('ratio', minimum=0)
    suction: float = number('rain_depth', minimum=0)

    def validate_local(self):
        if self.thickness > 0 and not (self.wilting_point < self.field_capacity < self.porosity and self.conductivity > 0):
            yield Diagnostic(code='lid.soil', message='Positive soil thickness requires wilting point < field capacity < porosity and positive conductivity')


@dataclass(frozen=True, kw_only=True)
class LidStorage:
    thickness: float = number('rain_depth', minimum=0)
    void_ratio: float = number('ratio', minimum=0)
    seepage_rate: float = number('rain_intensity', minimum=0)
    clogging_factor: float = number('ratio', minimum=0)
    covered: bool | None = None


@dataclass(frozen=True, kw_only=True)
class LidDrain:
    coefficient: float = number('lid_drain_coefficient', minimum=0)
    exponent: float = number('ratio', minimum=0)
    offset: float = number('rain_depth', minimum=0)
    delay: float = number('hours', minimum=0)
    open_head: float | None = number('rain_depth', None, minimum=0)
    close_head: float | None = number('rain_depth', None, minimum=0)
    curve: Ref | None = reference('swmm:curves', None)

    def validate_local(self):
        if (self.open_head or 0) > 0 and self.open_head <= (self.close_head or 0):
            yield Diagnostic(code='lid.drain_heads', message='A positive drain opening head must exceed the closing head')


@dataclass(frozen=True, kw_only=True)
class LidDrainMat:
    thickness: float = number('rain_depth', minimum=0)
    void_fraction: float = number('ratio', minimum=0, maximum=1)
    roughness: float = number('manning', minimum=0)


@dataclass(frozen=True, kw_only=True)
class LidRemoval:
    pollutant: Ref = reference('swmm:pollutants')
    percent: float = number('percent', minimum=0, maximum=100)


@dataclass(frozen=True, kw_only=True)
class LidControl(Entity):
    kind: Literal['BC', 'RG', 'GR', 'IT', 'PP', 'RB', 'RD', 'VS']
    surface: LidSurface | None = None
    pavement: LidPavement | None = None
    soil: LidSoil | None = None
    storage: LidStorage | None = None
    drain: LidDrain | None = None
    drain_mat: LidDrainMat | None = None
    removals: tuple[LidRemoval, ...] = ()

    def validate_local(self):
        for name, cls in LAYERS.values():
            if getattr(self, name) is not None and type(getattr(self, name)) is not cls:
                yield Diagnostic(code='lid.layer_variant', message=f'Unknown {name} variant needs an explicit adapter', field=name)
        if any(type(item) is not LidRemoval for item in self.removals):
            yield Diagnostic(code='lid.removal_variant', message='Unknown removal variant needs an explicit adapter')
        if len({item.pollutant.canonical for item in self.removals}) != len(self.removals):
            yield Diagnostic(code='lid.duplicate_removal', message='Each pollutant has one effective removal percentage')
        required = {'BC': ('soil',), 'RG': ('soil',), 'GR': ('soil', 'drain_mat'),
                    'PP': ('pavement',), 'IT': ('storage',)}.get(self.kind, ())
        for name in required:
            if getattr(self, name) is None or getattr(self, name).thickness <= 0:
                yield Diagnostic(code='lid.required_layer', message=f'{self.kind} requires positive {name} thickness', field=name)
        if self.kind == 'PP' and self.pavement and (self.pavement.permeability <= 0 or self.pavement.void_ratio <= 0):
            yield Diagnostic(code='lid.pavement', message='Permeable pavement requires positive permeability and void ratio')
        if self.storage and self.storage.thickness > 0 and self.storage.void_ratio <= 0:
            yield Diagnostic(code='lid.storage', message='Native validates a positive storage void ratio, including rain barrels before its later override')
        if self.kind == 'VS' and (self.surface is None or min(self.surface.storage_depth, self.surface.roughness, self.surface.slope) <= 0):
            yield Diagnostic(code='lid.swale', message='Swales require positive surface depth, slope and roughness')
        if self.drain_mat and self.kind != 'GR':
            yield Diagnostic(code='lid.drain_mat_kind', message='Native only reads DRAINMAT after a GR declaration')
        if self.kind == 'GR' and self.drain_mat and self.drain_mat.void_fraction <= 0:
            yield Diagnostic(code='lid.drain_mat_void', message='A drainage mat requires a positive void fraction')


@dataclass(frozen=True, kw_only=True)
class LidUsage:
    # SWMM has no ID for this row. This identity belongs to the Model/JSON API;
    # equal subcatchment/control pairs remain separate ordered deployments.
    record_id: str
    subcatchment: Ref = reference('swmm:subcatchments')
    control: Ref = reference('swmm:lid_controls')
    area: float = number('area', minimum=0)
    width: float = number('length', minimum=0)
    initial_saturation: float = number('percent', minimum=0, maximum=100)
    from_impervious: float = number('percent', minimum=0, maximum=100)
    to_pervious: bool
    report_file: FileReference | None = None
    drain_to: Ref | None = None
    from_pervious: float | None = number('percent', None, minimum=0, maximum=100)
    number: int = number('count', minimum=1, maximum=2_147_483_647, integer=True)

    def __post_init__(self):
        validate_identifier(self.record_id)

    def validate_local(self):
        if self.number and self.area <= 0:
            yield Diagnostic(code='lid.usage_area', message='An active LID deployment requires positive area')
        if self.drain_to and self.drain_to.collection not in ('swmm:nodes', 'swmm:subcatchments'):
            yield Diagnostic(code='lid.drain_target', message='A drain target must be a node or subcatchment')
        if self.drain_to and self.drain_to.key == '*':
            yield Diagnostic(code='lid.drain_placeholder', message='An explicit drain target cannot be the native * placeholder')
        if self.report_file and self.report_file.direction != 'output':
            yield Diagnostic(code='lid.file_direction', message='The LID detail report is an output file')
        if self.report_file and self.report_file.path == '*':
            yield Diagnostic(code='lid.file_placeholder', message='An explicit report path cannot be the native * placeholder')


@dataclass(frozen=True, kw_only=True)
class DisabledLidUsage:
    """Zero replicates: native checks two identities, then ignores every token.

    Inert tokens are retained without pretending they have validated dimensions,
    file access or references. Activation requires an explicit LidUsage value.
    """
    record_id: str
    subcatchment: Ref = reference('swmm:subcatchments')
    control: Ref = reference('swmm:lid_controls')
    parameters: tuple[str, ...]
    number: ClassVar[int] = 0

    def __post_init__(self):
        validate_identifier(self.record_id)

    def validate_local(self):
        if len(self.parameters) < 5 or any(not v or any(c in v for c in '\r\n\x00') for v in self.parameters):
            yield Diagnostic(code='lid.disabled_parameters', message='A disabled row retains at least five nonempty ignored tokens')


LAYERS = {'SURFACE': ('surface', LidSurface), 'PAVEMENT': ('pavement', LidPavement),
          'SOIL': ('soil', LidSoil), 'STORAGE': ('storage', LidStorage),
          'DRAIN': ('drain', LidDrain), 'DRAINMAT': ('drain_mat', LidDrainMat)}
LID_COLLECTIONS = (
    CollectionSpec(key='swmm:lid_controls', record_type=LidControl, key_of=lambda row: row.id, identity_field='id', validate=validate_fields),
    CollectionSpec(key='swmm:lid_usage', record_type=(LidUsage, DisabledLidUsage), key_of=lambda row: row.record_id, identity_field='record_id', validate=validate_fields),
)


def validate_lids(store, profile):
    controls = store.collection('swmm:lid_controls')
    catchment_names = {canonical_key(k) for k in store.collection('swmm:subcatchments')}
    totals = {}
    contributors = {}
    def subject(collection, key, *path):
        return DiagnosticSubject(collection='swmm:' + collection, key=key, path=path)
    options = get_options(store)
    if not ValidationReport(diagnostics=tuple(validate_fields(options))).is_valid:
        return
    units = UnitContext(flow_units=options.flow_units or profile.option_default('flow_units'))
    # v5.2.4 UCF(LANDAREA) maps internal ft2 to acre/ha; UCF(LENGTH)
    # maps deployment lengths. Native constants are deliberately rounded.
    area_factor = 1 / 2.2956e-5 if units.system == 'US' else .3048**2 / .92903e-5
    for row in controls.values():
        if type(row) is not LidControl:
            yield Diagnostic(code='lid.record_variant', object_id=row.id, message='An extension control requires an explicit feature adapter',
                subject=subject('lid_controls',row.id))
        if not ValidationReport(diagnostics=tuple(validate_fields(row))).is_valid:
            continue
        if row.kind == 'RB' and row.storage and row.storage.covered is None:
            yield Diagnostic(code='lid.barrel_cover_default', severity=Severity.WARNING, object_id=row.id,
                subject=subject('lid_controls',row.id,'storage','covered'), related=(subject('lid_controls',row.id,'kind'),),
                message='SWMM 5.2.4 defaults omitted covered to NO; the manual says YES. Set it explicitly')
        # The manual's recommended layout is stricter than native acceptance.
        recommended = {'BC': ('surface','soil','storage'), 'RG': ('surface','soil'), 'GR': ('surface','soil','drain_mat'),
            'IT': ('surface','storage'), 'PP': ('surface','pavement','storage'), 'RB': ('storage','drain'), 'RD': ('surface','drain'), 'VS': ('surface',)}
        for name in recommended.get(row.kind, ()):
            if getattr(row, name) is None:
                yield Diagnostic(code='lid.implicit_layer', severity=Severity.WARNING, object_id=row.id, field=name,
                    subject=subject('lid_controls',row.id,name), related=(subject('lid_controls',row.id,'kind'),),
                    message=f'Manual recommends an explicit {name}; native uses an implicit zero layer')
    for row in store.collection('swmm:lid_usage').values():
        if type(row) not in (LidUsage, DisabledLidUsage):
            yield Diagnostic(code='lid.usage_variant', object_id=row.record_id, message='An extension usage requires an explicit feature adapter',
                subject=subject('lid_usage',row.record_id))
        if not ValidationReport(diagnostics=tuple(validate_fields(row))).is_valid:
            continue
        if isinstance(row, DisabledLidUsage):
            continue
        if row.drain_to and row.drain_to.collection == 'swmm:nodes' and canonical_key(row.drain_to.key) in catchment_names:
            yield Diagnostic(code='lid.shadowed_drain', object_id=row.record_id, message='Native resolves this drain name to a subcatchment before the requested node',
                subject=subject('lid_usage',row.record_id,'drain_to','key'),
                related=(subject('nodes',row.drain_to.key),subject('subcatchments',row.drain_to.key)))
        if not row.number:
            continue
        if store.contains(row.control) and controls[row.control.key].kind == 'VS' and row.width <= 0:
            yield Diagnostic(code='lid.swale_width', object_id=row.record_id, message='Swale deployments require positive width',
                subject=subject('lid_usage',row.record_id,'width'), related=(subject('lid_controls',row.control.key,'kind'),))
        values = totals.setdefault(row.subcatchment.canonical, [0., 0., 0.])
        contributors.setdefault(row.subcatchment.canonical, []).append(row)
        for i, value in enumerate((row.number * row.area, row.from_impervious, row.from_pervious or 0)):
            values[i] += value
    for target, (area, imperv, perv) in totals.items():
        key = target.key
        if imperv > 100.1 or perv > 100.1:
            yield Diagnostic(code='lid.capture_total', object_id=key, message='Total capture from each non-LID subarea exceeds the native 100.1% tolerance',
                subject=subject('subcatchments',key), related=tuple(subject('lid_usage',row.record_id,name)
                    for row in contributors[target] for name,value in (('from_impervious',imperv),('from_pervious',perv)) if value>100.1))
        catchments = store.collection('swmm:subcatchments')
        if key in catchments and area > catchments[key].area * area_factor * 1.001:
            yield Diagnostic(code='lid.area_total', object_id=key, message='Total replicated LID area exceeds the subcatchment area and native 0.1% tolerance',
                subject=subject('subcatchments',key,'area'), related=tuple(subject('lid_usage',row.record_id,name)
                    for row in contributors[target] for name in ('number','area')) + (subject('options','settings','flow_units'),))


def lid_resource_uses(store):
    for row in store.collection('swmm:lid_controls').values():
        if not ValidationReport(diagnostics=tuple(validate_fields(row))).is_valid:
            continue
        if row.drain and row.drain.curve:
            yield ResourceUse(owner=Ref(collection='swmm:lid_controls', key=row.id), target=row.drain.curve,
                path=('drain','curve'), role='LID drain multiplier', dimensions=('rain_depth','ratio'), accepted_kinds=('CONTROL',))


def _convert_control(row, context):
    if any(type(getattr(row, name)) is not cls for name, cls in LAYERS.values() if getattr(row, name) is not None) or any(type(item) is not LidRemoval for item in row.removals):
        raise ValueError('Unknown LID layer/removal requires an explicit unit adapter')
    layers = {name: context.convert_value(getattr(row, name), path=(name,)) for name, _ in LAYERS.values() if name != 'drain' and getattr(row, name) is not None}
    if row.drain:
        d = row.drain
        # RD ignores the exponent and interprets C as gutter capacity directly.
        factor = context.number(1., 'rain_intensity') / context.number(1., 'rain_depth') ** (0 if row.kind == 'RD' else d.exponent)
        layers['drain'] = replace(d, coefficient=d.coefficient * factor, offset=context.number(d.offset, 'rain_depth'),
            open_head=context.number(d.open_head, 'rain_depth') if d.open_head is not None else None,
            close_head=context.number(d.close_head, 'rain_depth') if d.close_head is not None else None)
    return replace(row, **layers)


def _convert_pollutant(row, context):
    if any(type(item) is not LidRemoval for item in row.removals):
        raise ValueError('Unknown LID removal requires an explicit pollutant unit adapter')
    return PollutantUnitConversion(value=row)  # percentages, not concentrations


LID_TRANSFORMS = (UnitTransform(value_type=LidControl, convert=_convert_control),)
LID_POLLUTANT_TRANSFORMS = (PollutantUnitTransform(value_type=LidControl, convert=_convert_pollutant),)
