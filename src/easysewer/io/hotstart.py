"""SWMM hotstart versions 1–4, with explicit positional layout ownership.

The supported binary representation is little-endian int32/float32/float64.
State uses the engine's internal units, including in SI projects. A layout is
an interpretation supplied by the caller, not evidence of the file's origin.
"""

from dataclasses import dataclass, replace
import math
from pathlib import Path
import struct

from ..model.identity import canonical_key, validate_identifier
from ..validation._cooperative import checkpointed
from ._atomic import write_bytes
from .routing import FLOW_UNITS

_STAMP = b'SWMM5-HOTSTART'
_METHOD_FIELDS = {
    'HORTON': ('tp', 'Fe', 'reserved_2', 'reserved_3', 'reserved_4', 'reserved_5'),
    'MODIFIED_HORTON': ('tp', 'Fe', 'reserved_2', 'reserved_3', 'reserved_4', 'reserved_5'),
    'GREEN_AMPT': ('IMD', 'F', 'Fu', 'Sat', 'T', 'reserved_5'),
    'MODIFIED_GREEN_AMPT': ('IMD', 'F', 'Fu', 'Sat', 'T', 'reserved_5'),
    'CURVE_NUMBER': ('S', 'P', 'F', 'T', 'Se', 'f'),
}


def _number(value, *, single=False):
    if type(value) not in (int, float):
        raise TypeError('State values must be finite real numbers, not booleans')
    try:
        value = float(value)
        if single:
            value, = struct.unpack('<f', struct.pack('<f', value))
    except (OverflowError, struct.error) as error:
        raise ValueError('State value exceeds its native floating point range') from error
    if not math.isfinite(value):
        raise ValueError('State values must be finite')
    return value


def _vector(values, count=None, *, single=False):
    if type(values) is not tuple:
        raise TypeError('State arrays must be immutable tuples')
    if count is not None and len(values) != count:
        raise ValueError(f'Expected {count} state values, received {len(values)}')
    return tuple(_number(v, single=single) for v in checkpointed(values))


def _rows(values, kind):
    if type(values) is not tuple or any(type(v) is not kind for v in checkpointed(values)):
        raise TypeError(f'Expected an immutable tuple of {kind.__name__}')
    ids = tuple(v.id for v in checkpointed(values))
    _ids(ids)


def _ids(values):
    if type(values) is not tuple:
        raise TypeError('Object IDs must be an immutable tuple')
    for value in checkpointed(values):
        validate_identifier(value)
    if len({canonical_key(v) for v in checkpointed(values)}) != len(values):
        raise ValueError('Duplicate positional object identity')


@dataclass(frozen=True, kw_only=True)
class StateObject:
    id: str
    kind: str

    def __post_init__(self):
        validate_identifier(self.id)
        if self.kind not in ('JUNCTION', 'OUTFALL', 'STORAGE', 'DIVIDER',
                             'CONDUIT', 'PUMP', 'ORIFICE', 'WEIR', 'OUTLET'):
            raise ValueError('Unknown SWMM state object type')


@dataclass(frozen=True, kw_only=True)
class StatePollutant:
    id: str
    units: str

    def __post_init__(self):
        validate_identifier(self.id)
        if self.units not in ('MG/L', 'UG/L', '#/L'):
            raise ValueError('Unknown pollutant concentration units')


@dataclass(frozen=True, kw_only=True)
class CatchmentLayout:
    id: str
    infiltration: str
    # Bindings identify the interpretation as well as optional block presence.
    groundwater: str | None = None
    snowpack: str | None = None

    def __post_init__(self):
        validate_identifier(self.id)
        if self.infiltration not in _METHOD_FIELDS:
            raise ValueError('Unknown infiltration state method')
        for value in checkpointed((self.groundwater, self.snowpack)):
            if value is not None:
                validate_identifier(value)


@dataclass(frozen=True, kw_only=True)
class HotstartLayout:
    flow_units: str
    nodes: tuple[StateObject, ...] = ()
    links: tuple[StateObject, ...] = ()
    subcatchments: tuple[CatchmentLayout, ...] = ()
    pollutants: tuple[StatePollutant, ...] = ()
    landuses: tuple[str, ...] = ()

    def __post_init__(self):
        if self.flow_units not in FLOW_UNITS:
            raise ValueError('Unknown hotstart flow units')
        _rows(self.nodes, StateObject); _rows(self.links, StateObject)
        _rows(self.subcatchments, CatchmentLayout); _rows(self.pollutants, StatePollutant)
        _ids(self.landuses)
        if any(v.kind not in ('JUNCTION', 'OUTFALL', 'STORAGE', 'DIVIDER') for v in checkpointed(self.nodes)):
            raise ValueError('Node layout contains a link type')
        if any(v.kind not in ('CONDUIT', 'PUMP', 'ORIFICE', 'WEIR', 'OUTLET') for v in checkpointed(self.links)):
            raise ValueError('Link layout contains a node type')
        if any(len(v) > 2147483647 for v in checkpointed((self.nodes, self.links, self.subcatchments, self.pollutants, self.landuses))):
            raise ValueError('Hotstart counts exceed native int32')

    @classmethod
    def from_model(cls, model, *, normalize=False):
        """Derive only supported, fully understood layout dependencies.

        Order follows model.to_document(normalize=normalize). It
        can differ from collection order. This performs no filesystem I/O.
        """
        from ._hotstart_model import model_layout
        return model_layout(model, normalize=normalize)

    def byte_length(self, version=4):
        _version(version)
        np = len(self.pollutants)
        size = len(_STAMP) + (version != 1) + 4 * (4 + (version >= 2) + (version >= 3))
        if version >= 3:
            for catchment in checkpointed(self.subcatchments):
                size += 8 * (10 + 4*(catchment.groundwater is not None) + 15*(catchment.snowpack is not None)
                             + (2*np + len(self.landuses)*(np+1) if np else 0))
        if version == 2:
            size += 8 * len(self.subcatchments)
        size += 4 * sum(2 + np + (np if version <= 2 else 0) + (version >= 4 and n.kind == 'STORAGE') for n in checkpointed(self.nodes))
        return size + 4 * len(self.links) * (3 + np)


def _version(version):
    if type(version) is not int or version not in (1, 2, 3, 4):
        raise ValueError('Supported hotstart versions are 1, 2, 3 and 4')


def read_header(data):
    """Return (version, header values, payload offset), without guessing IDs."""
    if not isinstance(data, bytes):
        raise TypeError('Hotstart source must be immutable bytes')
    if not data.startswith(_STAMP):
        raise ValueError('Invalid hotstart signature')
    offset, version = len(_STAMP), 1
    if data[offset:offset+1] in (b'2', b'3', b'4'):
        version = int(chr(data[offset])); offset += 1
    fields = ('nodes', 'links', 'pollutants', 'units')
    if version >= 2:
        fields = ('subcatchments', *fields)
    if version >= 3:
        fields = ('subcatchments', 'landuses', *fields[1:])
    if len(data) < offset + 4*len(fields):
        raise ValueError('Truncated hotstart header')
    values = dict(zip(fields, struct.unpack_from('<'+'i'*len(fields), data, offset)))
    if any(v < 0 for v in checkpointed(values.values())) or values['units'] not in range(6):
        raise ValueError('Invalid hotstart counts or flow units')
    return version, values, offset + 4*len(fields)


@dataclass(frozen=True, kw_only=True)
class InfiltrationState:
    method: str
    values: tuple[float, ...]

    def __post_init__(self):
        if self.method not in _METHOD_FIELDS:
            raise ValueError('Unknown infiltration state method')
        object.__setattr__(self, 'values', _vector(self.values, 6))
        if self.method in ('GREEN_AMPT', 'MODIFIED_GREEN_AMPT') and self.values[3] not in (0, 1):
            raise ValueError('Green-Ampt Sat state must be zero or one')

    @property
    def named_values(self):
        return dict(zip(_METHOD_FIELDS[self.method], self.values))

    def with_values(self, **changes):
        values = self.named_values
        if changes.keys() - values.keys():
            raise ValueError('Unknown infiltration state field')
        values.update(changes)
        return replace(self, values=tuple(values.values()))


@dataclass(frozen=True, kw_only=True)
class GroundwaterState:
    moisture: float
    water_table: float
    flow: float
    maximum_infiltration_volume: float

    def __post_init__(self):
        for name in checkpointed(('moisture', 'water_table', 'flow', 'maximum_infiltration_volume')):
            object.__setattr__(self, name, _number(getattr(self, name)))


@dataclass(frozen=True, kw_only=True)
class SnowState:
    snow_depth: float
    free_water: float
    cold_content: float
    antecedent_temperature: float
    antecedent_snow: float

    def __post_init__(self):
        for name in checkpointed(('snow_depth', 'free_water', 'cold_content', 'antecedent_temperature', 'antecedent_snow')):
            object.__setattr__(self, name, _number(getattr(self, name)))


@dataclass(frozen=True, kw_only=True)
class LanduseState:
    id: str
    buildup: tuple[float, ...]
    last_swept: float

    def __post_init__(self):
        validate_identifier(self.id)
        object.__setattr__(self, 'buildup', _vector(self.buildup))
        object.__setattr__(self, 'last_swept', _number(self.last_swept))


@dataclass(frozen=True, kw_only=True)
class CatchmentState:
    id: str
    ponded_depths: tuple[float, float, float]
    runoff: float
    infiltration: InfiltrationState
    groundwater: GroundwaterState | None = None
    snow: tuple[SnowState, ...] = ()
    runoff_quality: tuple[float, ...] = ()
    ponded_quality: tuple[float, ...] = ()
    landuses: tuple[LanduseState, ...] = ()

    def __post_init__(self):
        validate_identifier(self.id)
        object.__setattr__(self, 'ponded_depths', _vector(self.ponded_depths, 3))
        object.__setattr__(self, 'runoff', _number(self.runoff))
        if type(self.infiltration) is not InfiltrationState or self.groundwater is not None and type(self.groundwater) is not GroundwaterState:
            raise TypeError('Expected typed infiltration/groundwater state')
        if type(self.snow) is not tuple or len(self.snow) not in (0, 3) or any(type(s) is not SnowState for s in checkpointed(self.snow)):
            raise ValueError('Snow state contains zero or three ordered surfaces')
        for name in checkpointed(('runoff_quality', 'ponded_quality')):
            object.__setattr__(self, name, _vector(getattr(self, name)))
        _rows(self.landuses, LanduseState)


@dataclass(frozen=True, kw_only=True)
class NodeState:
    id: str
    depth: float
    lateral_inflow: float
    residence_time: float | None = None
    quality: tuple[float, ...] = ()
    legacy_quality: tuple[float, ...] = ()

    def __post_init__(self):
        validate_identifier(self.id)
        for name in checkpointed(('depth', 'lateral_inflow', 'residence_time')):
            value = getattr(self, name)
            if name != 'residence_time' or value is not None:
                object.__setattr__(self, name, _number(value, single=True))
        for name in checkpointed(('quality', 'legacy_quality')):
            object.__setattr__(self, name, _vector(getattr(self, name), single=True))


@dataclass(frozen=True, kw_only=True)
class LinkState:
    id: str
    flow: float
    depth: float
    setting: float
    quality: tuple[float, ...] = ()

    def __post_init__(self):
        validate_identifier(self.id)
        for name in checkpointed(('flow', 'depth', 'setting')):
            object.__setattr__(self, name, _number(getattr(self, name), single=True))
        object.__setattr__(self, 'quality', _vector(self.quality, single=True))


@dataclass(frozen=True, kw_only=True)
class LegacyGroundwaterState:
    id: str
    moisture: float
    water_table: float

    def __post_init__(self):
        validate_identifier(self.id)
        for name in checkpointed(('moisture', 'water_table')):
            object.__setattr__(self, name, _number(getattr(self, name), single=True))


def _same_ids(states, layout):
    if tuple(s.id for s in checkpointed(states)) != tuple(s.id for s in checkpointed(layout)):
        raise ValueError('State IDs/order do not match the declared layout')


@dataclass(frozen=True, kw_only=True)
class HotstartData:
    layout: HotstartLayout
    nodes: tuple[NodeState, ...]
    links: tuple[LinkState, ...]
    version: int = 4
    subcatchments: tuple[CatchmentState, ...] = ()
    legacy_groundwater: tuple[LegacyGroundwaterState, ...] = ()

    def __post_init__(self):
        _version(self.version)
        if type(self.layout) is not HotstartLayout:
            raise TypeError('Hotstart data requires an explicit layout')
        if self.version == 1 and len(self.layout.nodes) % 256 in (50, 51, 52):
            raise ValueError('Version 1 node count aliases a later version signature; use an explicit later version')
        for values, kind in checkpointed(((self.nodes, NodeState), (self.links, LinkState),
                             (self.subcatchments, CatchmentState), (self.legacy_groundwater, LegacyGroundwaterState))):
            _rows(values, kind)
        _same_ids(self.nodes, self.layout.nodes); _same_ids(self.links, self.layout.links)
        _same_ids(self.subcatchments, self.layout.subcatchments if self.version >= 3 else ())
        _same_ids(self.legacy_groundwater, self.layout.subcatchments if self.version == 2 else ())
        np = len(self.layout.pollutants)
        for node, spec in checkpointed(zip(self.nodes, self.layout.nodes)):
            if (node.residence_time is not None) != (self.version >= 4 and spec.kind == 'STORAGE'):
                raise ValueError('Storage residence time presence differs from the version/type layout')
            if len(node.quality) != np or len(node.legacy_quality) != (np if self.version <= 2 else 0):
                raise ValueError('Node quality length differs from the version/pollutant layout')
        if any(len(link.quality) != np for link in checkpointed(self.links)):
            raise ValueError('Link quality length differs from the pollutant layout')
        for catchment, spec in checkpointed(zip(self.subcatchments, self.layout.subcatchments)):
            if catchment.infiltration.method != spec.infiltration:
                raise ValueError('Infiltration state method differs from the layout')
            if (catchment.groundwater is not None) != (spec.groundwater is not None) or bool(catchment.snow) != (spec.snowpack is not None):
                raise ValueError('Groundwater/snow state presence differs from the layout')
            if len(catchment.runoff_quality) != np or len(catchment.ponded_quality) != np:
                raise ValueError('Catchment quality length differs from the pollutant layout')
            if tuple(s.id for s in checkpointed(catchment.landuses)) != (self.layout.landuses if np else ()) or any(len(s.buildup) != np for s in checkpointed(catchment.landuses)):
                raise ValueError('Landuse state differs from the landuse/pollutant layout')

    @classmethod
    def from_bytes(cls, data, *, layout):
        if type(layout) is not HotstartLayout:
            raise TypeError('Hotstart reading requires an explicit layout')
        version, header, offset = read_header(data)
        for name, count in checkpointed(header.items()):
            expected = FLOW_UNITS.index(layout.flow_units) if name == 'units' else len(getattr(layout, name))
            if count != expected:
                raise ValueError(f'Hotstart {name} does not match its layout')
        if len(data) != layout.byte_length(version):
            raise ValueError('Hotstart is truncated, has trailing data, or its state layout is wrong')
        def take(count, single=False):
            nonlocal offset
            values = struct.unpack_from('<'+('f' if single else 'd')*count, data, offset)
            offset += count*(4 if single else 8)
            return values
        np = len(layout.pollutants)
        catchments, nodes, links, legacy = [], [], [], []
        if version >= 3:
            for spec in checkpointed(layout.subcatchments):
                ponded, runoff = take(3), take(1)[0]
                infiltration = InfiltrationState(method=spec.infiltration, values=take(6))
                groundwater = None
                if spec.groundwater is not None:
                    v = take(4)
                    groundwater = GroundwaterState(moisture=v[0], water_table=v[1], flow=v[2], maximum_infiltration_volume=v[3])
                snow = []
                if spec.snowpack is not None:
                    for _ in checkpointed(range(3)):
                        v = take(5)
                        snow.append(SnowState(snow_depth=v[0], free_water=v[1], cold_content=v[2], antecedent_temperature=v[3], antecedent_snow=v[4]))
                runoff_quality, ponded_quality = take(np), take(np)
                landuses = tuple(LanduseState(id=id, buildup=take(np), last_swept=take(1)[0]) for id in checkpointed(layout.landuses)) if np else ()
                catchments.append(CatchmentState(id=spec.id, ponded_depths=ponded, runoff=runoff, infiltration=infiltration,
                    groundwater=groundwater, snow=tuple(snow), runoff_quality=runoff_quality, ponded_quality=ponded_quality, landuses=landuses))
        if version == 2:
            for spec in checkpointed(layout.subcatchments):
                moisture, table = take(2, True)
                legacy.append(LegacyGroundwaterState(id=spec.id, moisture=moisture, water_table=table))
        for spec in checkpointed(layout.nodes):
            depth, lateral = take(2, True)
            residence = take(1, True)[0] if version >= 4 and spec.kind == 'STORAGE' else None
            nodes.append(NodeState(id=spec.id, depth=depth, lateral_inflow=lateral, residence_time=residence,
                                  quality=take(np, True), legacy_quality=take(np, True) if version <= 2 else ()))
        for spec in checkpointed(layout.links):
            flow, depth, setting = take(3, True)
            links.append(LinkState(id=spec.id, flow=flow, depth=depth, setting=setting, quality=take(np, True)))
        return cls(layout=layout, version=version, nodes=tuple(nodes), links=tuple(links),
                   subcatchments=tuple(catchments), legacy_groundwater=tuple(legacy))

    @classmethod
    def read(cls, path, *, layout):
        return cls.from_bytes(Path(path).read_bytes(), layout=layout)

    def to_bytes(self):
        layout, version = self.layout, self.version
        result = bytearray(_STAMP + (str(version).encode('ascii') if version != 1 else b''))
        counts = (len(layout.nodes), len(layout.links), len(layout.pollutants), FLOW_UNITS.index(layout.flow_units))
        if version >= 2:
            counts = (len(layout.subcatchments), *counts)
        if version >= 3:
            counts = (counts[0], len(layout.landuses), *counts[1:])
        result.extend(struct.pack('<'+'i'*len(counts), *counts))
        def put(values, single=False):
            result.extend(struct.pack('<'+('f' if single else 'd')*len(values), *values))
        for state in checkpointed(self.subcatchments):
            put((*state.ponded_depths, state.runoff, *state.infiltration.values))
            if state.groundwater is not None:
                g = state.groundwater
                put((g.moisture, g.water_table, g.flow, g.maximum_infiltration_volume))
            for snow in checkpointed(state.snow):
                put((snow.snow_depth, snow.free_water, snow.cold_content, snow.antecedent_temperature, snow.antecedent_snow))
            put((*state.runoff_quality, *state.ponded_quality))
            for landuse in checkpointed(state.landuses):
                put((*landuse.buildup, landuse.last_swept))
        for state in checkpointed(self.legacy_groundwater):
            put((state.moisture, state.water_table), True)
        for state in checkpointed(self.nodes):
            put((state.depth, state.lateral_inflow), True)
            if state.residence_time is not None:
                put((state.residence_time,), True)
            put((*state.quality, *state.legacy_quality), True)
        for state in checkpointed(self.links):
            put((state.flow, state.depth, state.setting, *state.quality), True)
        return bytes(result)

    def write(self, path):
        return write_bytes(path, self.to_bytes())
