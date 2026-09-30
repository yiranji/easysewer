"""Editable little-endian RUNOFF and RDII caches, independent of native code."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
import struct

from ..model.identity import validate_identifier, canonical_key
from ..validation._cooperative import checkpointed
from ._atomic import write_bytes
from .hotstart import StatePollutant, _number, _vector, _ids, _rows
from .routing import FLOW_UNITS


def _export(model, normalize):
    from ._hotstart_model import LayoutUnavailable
    if type(normalize) is not bool:
        raise TypeError('normalize must be boolean')
    if model.profile.engine_version != '5.2.4' or model._store.opaque_constraints:
        raise LayoutUnavailable('Cache identity requires understood Model records and the fixed 5.2.4 profile')
    return model.to_document(normalize=normalize)


def _ordered(document, sections, collection):
    ids = tuple(line.values[0] for line in checkpointed(document.lines) if line.kind == 'data' and line.section in sections)
    _ids(ids)
    if {canonical_key(v) for v in checkpointed(ids)} != {canonical_key(v) for v in checkpointed(collection)}:
        raise ValueError('Exported identities do not match the Model collection')
    return ids


@dataclass(frozen=True, kw_only=True)
class RunoffLayout:
    flow_units: str
    subcatchments: tuple[str, ...]
    pollutants: tuple[StatePollutant, ...] = ()

    def __post_init__(self):
        if self.flow_units not in FLOW_UNITS:
            raise ValueError('Unknown runoff flow units')
        _ids(self.subcatchments); _rows(self.pollutants, StatePollutant)

    @classmethod
    def from_model(cls, model, *, normalize=False):
        from ._hotstart_model import LayoutUnavailable
        from ..model.quality import Pollutant
        document = _export(model, normalize)
        pollutants = _ordered(document, {'POLLUTANTS'}, model.pollutants)
        if any(type(model.pollutants[id]) is not Pollutant for id in checkpointed(pollutants)):
            raise LayoutUnavailable('Runoff pollutant variant needs its own layout adapter')
        return cls(flow_units=model.units.flow_units, subcatchments=_ordered(document, {'SUBCATCHMENTS'}, model.subcatchments),
            pollutants=tuple(StatePollutant(id=id,units=model.pollutants[id].units) for id in checkpointed(pollutants)))


@dataclass(frozen=True, kw_only=True)
class RdiiBindingIdentity:
    node: str
    hydrograph: str

    def __post_init__(self):
        validate_identifier(self.node); validate_identifier(self.hydrograph)


@dataclass(frozen=True, kw_only=True)
class RdiiLayout:
    nodes: tuple[str, ...]
    bindings: tuple[RdiiBindingIdentity, ...]

    def __post_init__(self):
        _ids(self.nodes)
        if type(self.bindings) is not tuple or any(type(v) is not RdiiBindingIdentity for v in checkpointed(self.bindings)):
            raise TypeError('RDII bindings must be an immutable tuple of RdiiBindingIdentity')
        _ids(tuple(v.node for v in checkpointed(self.bindings)))
        if {canonical_key(v.node) for v in checkpointed(self.bindings)} - {canonical_key(v) for v in checkpointed(self.nodes)}:
            raise ValueError('RDII binding node is missing from the declared native node order')

    @classmethod
    def from_model(cls, model, *, normalize=False):
        document = _export(model, normalize)
        nodes = _ordered(document, {'JUNCTIONS', 'OUTFALLS', 'STORAGE', 'DIVIDERS'}, model.nodes)
        bindings = []
        for node in checkpointed(nodes):
            if node in model.rdii:
                row = model.rdii[node]
                if row.hydrograph.key not in model.hydrographs:
                    raise ValueError('RDII relation references a missing hydrograph group')
                bindings.append(RdiiBindingIdentity(node=node, hydrograph=row.hydrograph.key))
        if len(bindings) != len(model.rdii):
            raise ValueError('RDII relation references a missing node')
        return cls(nodes=nodes, bindings=tuple(bindings))


@dataclass(frozen=True, kw_only=True)
class RunoffSample:
    id: str
    rainfall: float
    snow_depth: float
    evaporation: float
    infiltration: float
    runoff: float
    groundwater_flow: float
    groundwater_elevation: float
    soil_moisture: float
    quality: tuple[float, ...] = ()

    def __post_init__(self):
        validate_identifier(self.id)
        for name in checkpointed(_RUNOFF_FIELDS):
            object.__setattr__(self, name, _number(getattr(self, name), single=True))
        object.__setattr__(self, 'quality', _vector(self.quality, single=True))

    @property
    def values(self):
        return (*(getattr(self, field) for field in checkpointed(_RUNOFF_FIELDS)), *self.quality)


_RUNOFF_FIELDS = ('rainfall', 'snow_depth', 'evaporation', 'infiltration', 'runoff',
                  'groundwater_flow', 'groundwater_elevation', 'soil_moisture')


@dataclass(frozen=True, kw_only=True)
class RunoffFrame:
    step_seconds: float
    samples: tuple[RunoffSample, ...]

    def __post_init__(self):
        object.__setattr__(self, 'step_seconds', _number(self.step_seconds, single=True))
        if self.step_seconds <= 0:
            raise ValueError('Runoff cache time step must be positive')
        _rows(self.samples, RunoffSample)


@dataclass(frozen=True, kw_only=True)
class RunoffData:
    layout: RunoffLayout
    frames: tuple[RunoffFrame, ...]

    def __post_init__(self):
        if type(self.layout) is not RunoffLayout or type(self.frames) is not tuple or not self.frames or any(type(f) is not RunoffFrame for f in checkpointed(self.frames)):
            raise TypeError('RUNOFF requires an explicit layout and nonempty immutable RunoffFrame tuple')
        if max(len(self.frames), len(self.layout.subcatchments), len(self.layout.pollutants)) > 2147483647:
            raise ValueError('Runoff counts exceed signed int32')
        elapsed_ms = 0.0
        for frame in checkpointed(self.frames):
            if tuple(s.id for s in checkpointed(frame.samples)) != self.layout.subcatchments or any(len(s.quality) != len(self.layout.pollutants) for s in checkpointed(frame.samples)):
                raise ValueError('Runoff samples do not match the declared identity/quality layout')
            next_ms = elapsed_ms + frame.step_seconds * 1000.0
            if next_ms <= elapsed_ms:
                raise ValueError('Runoff step does not advance the native elapsed clock')
            elapsed_ms = next_ms

    @property
    def duration_seconds(self):
        return sum(frame.step_seconds for frame in checkpointed(self.frames))

    @classmethod
    def from_bytes(cls, data, *, layout):
        if not isinstance(data, bytes) or type(layout) is not RunoffLayout:
            raise TypeError('Runoff reading requires immutable bytes and a RunoffLayout')
        stamp = b'SWMM5-RUNOFF'
        if not data.startswith(stamp) or len(data) < len(stamp)+16:
            raise ValueError('Invalid/truncated runoff header')
        nc, np, units, count = struct.unpack_from('<4i', data, len(stamp))
        if (nc, np, units) != (len(layout.subcatchments), len(layout.pollutants), FLOW_UNITS.index(layout.flow_units)) or count <= 0:
            raise ValueError('Runoff header differs from layout or has no steps')
        offset, size = len(stamp)+16, 4+nc*(8+np)*4
        if len(data) != offset+count*size:
            raise ValueError('Runoff cache is truncated or has trailing data')
        frames = []
        for _ in checkpointed(range(count)):
            step, = struct.unpack_from('<f', data, offset); offset += 4
            samples = []
            for id in checkpointed(layout.subcatchments):
                values = struct.unpack_from('<'+'f'*(8+np), data, offset); offset += (8+np)*4
                samples.append(RunoffSample(id=id, **dict(zip(_RUNOFF_FIELDS, values[:8])), quality=values[8:]))
            frames.append(RunoffFrame(step_seconds=step, samples=tuple(samples)))
        return cls(layout=layout, frames=tuple(frames))

    @classmethod
    def read(cls, path, *, layout):
        return cls.from_bytes(Path(path).read_bytes(), layout=layout)

    def to_bytes(self):
        layout = self.layout
        data = bytearray(b'SWMM5-RUNOFF'+struct.pack('<4i', len(layout.subcatchments), len(layout.pollutants), FLOW_UNITS.index(layout.flow_units), len(self.frames)))
        for frame in checkpointed(self.frames):
            data.extend(struct.pack('<f', frame.step_seconds))
            for sample in checkpointed(frame.samples):
                data.extend(struct.pack('<'+'f'*len(sample.values), *sample.values))
        return bytes(data)

    def write(self, path):
        return write_bytes(path, self.to_bytes())


@dataclass(frozen=True, kw_only=True)
class RdiiFrame:
    native_time: float
    flows: tuple[float, ...]

    def __post_init__(self):
        object.__setattr__(self, 'native_time', _number(self.native_time))
        object.__setattr__(self, 'flows', _vector(self.flows, single=True))
        try:
            self.time
        except OverflowError as error:
            raise ValueError('RDII time falls outside the supported calendar range') from error

    @property
    def time(self):
        """Convenience calendar view; native_time retains exact binary days."""
        return datetime(1899, 12, 30) + timedelta(days=self.native_time)


@dataclass(frozen=True, kw_only=True)
class RdiiData:
    step: timedelta
    node_indices: tuple[int, ...]
    frames: tuple[RdiiFrame, ...]

    def __post_init__(self):
        if type(self.step) is not timedelta or self.step.microseconds or not 1 <= self.step.total_seconds() <= 2147483647:
            raise ValueError('RDII step requires positive native whole seconds')
        if type(self.node_indices) is not tuple or not self.node_indices or any(type(i) is not int or not 0 <= i <= 2147483647 for i in checkpointed(self.node_indices)):
            raise ValueError('RDII indices must be nonnegative int32 values')
        if len(self.node_indices) != len(set(self.node_indices)) or len(self.node_indices) > 2147483647:
            raise ValueError('Duplicate RDII native node indices or excessive count')
        if type(self.frames) is not tuple or any(type(f) is not RdiiFrame for f in checkpointed(self.frames)):
            raise TypeError('RDII frames must be an immutable tuple')
        previous = None
        for frame in checkpointed(self.frames):
            if len(frame.flows) != len(self.node_indices):
                raise ValueError('RDII frame width differs from header node count')
            if previous is not None and frame.native_time < previous+self.step.total_seconds()/86400-1e-9:
                raise ValueError('RDII intervals overlap or are out of order')
            previous = frame.native_time

    def validate_layout(self, layout):
        if type(layout) is not RdiiLayout:
            raise TypeError('RDII identity requires an explicit RdiiLayout')
        bound = {canonical_key(v.node) for v in checkpointed(layout.bindings)}
        if any(i >= len(layout.nodes) or canonical_key(layout.nodes[i]) not in bound for i in checkpointed(self.node_indices)):
            raise ValueError('RDII cache index is out of range or its node has no RDII binding')

    @classmethod
    def from_bytes(cls, data):
        if not isinstance(data, bytes):
            raise TypeError('RDII source must be immutable bytes')
        stamp = b'SWMM5-RDII'
        if not data.startswith(stamp) or len(data) < len(stamp)+8:
            raise ValueError('Invalid/truncated RDII header')
        step, count = struct.unpack_from('<2i', data, len(stamp))
        if step <= 0 or count <= 0 or len(data) < len(stamp)+8+count*4:
            raise ValueError('Invalid RDII step/count or truncated indices')
        indices = struct.unpack_from('<'+'i'*count, data, len(stamp)+8)
        offset, size = len(stamp)+8+count*4, 8+4*count
        if (len(data)-offset) % size:
            raise ValueError('RDII cache has an incomplete final frame')
        frames = []
        for pos in checkpointed(range(offset, len(data), size)):
            values = struct.unpack_from('<d'+'f'*count, data, pos)
            frames.append(RdiiFrame(native_time=values[0], flows=values[1:]))
        # A valid dry native simulation saves no frames, only this header.
        return cls(step=timedelta(seconds=step), node_indices=indices, frames=tuple(frames))

    @classmethod
    def read(cls, path):
        return cls.from_bytes(Path(path).read_bytes())

    def to_bytes(self):
        data = bytearray(b'SWMM5-RDII'+struct.pack('<2i', int(self.step.total_seconds()), len(self.node_indices)))
        data.extend(struct.pack('<'+'i'*len(self.node_indices), *self.node_indices))
        for frame in checkpointed(self.frames):
            data.extend(struct.pack('<d'+'f'*len(frame.flows), frame.native_time, *frame.flows))
        return bytes(data)

    def write(self, path):
        return write_bytes(path, self.to_bytes())
