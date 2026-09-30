"""Caller-asserted hotstart identity, bound to exact bytes by SHA-256.

This is provenance bookkeeping, not a signature or proof that a producer used
the asserted model. A future Runner can create it from its actual run snapshot.
"""

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re

from ..model.identity import canonical_key
from ._atomic import write_bytes
from .hotstart import HotstartData, HotstartLayout, StateObject, StatePollutant, CatchmentLayout
from .json.document import parse
from ..validation._cooperative import checkpointed
from ._record_work import record_asdict as asdict, record_dumps, record_digest

_SCHEMA = 'easysewer.hotstart-manifest/1'


def _hash(value):
    if type(value) is not str or re.fullmatch('[0-9a-f]{64}', value) is None:
        raise ValueError('Expected a lowercase SHA-256 hex digest')


def _fields(value, names):
    if type(value) is not dict or set(value) != set(names):
        raise ValueError('Manifest has missing or unsupported fields')
    return value


def _layout_value(layout):
    return asdict(layout)


def _read_layout(value):
    _fields(value, ('flow_units', 'nodes', 'links', 'subcatchments', 'pollutants', 'landuses'))
    fields = {}
    for name, kind, names in checkpointed((
        ('nodes', StateObject, ('id', 'kind')), ('links', StateObject, ('id', 'kind')),
        ('subcatchments', CatchmentLayout, ('id', 'infiltration', 'groundwater', 'snowpack')),
        ('pollutants', StatePollutant, ('id', 'units')),
    )):
        if type(value[name]) is not list:
            raise TypeError('Manifest collections must be JSON arrays')
        fields[name] = tuple(kind(**_fields(row, names)) for row in checkpointed(value[name]))
    if type(value['landuses']) is not list:
        raise TypeError('Manifest landuses must be a JSON array')
    return HotstartLayout(flow_units=value['flow_units'], landuses=tuple(value['landuses']), **fields)


def _identity(layout):
    # Native identifiers are ASCII case insensitive. Other spelling, order,
    # kinds, methods, pollutant units and optional bindings remain significant.
    return (layout.flow_units,
            tuple((canonical_key(v.id), v.kind) for v in checkpointed(layout.nodes)),
            tuple((canonical_key(v.id), v.kind) for v in checkpointed(layout.links)),
            tuple((canonical_key(v.id), v.infiltration, canonical_key(v.groundwater) if v.groundwater else None,
                   canonical_key(v.snowpack) if v.snowpack else None) for v in checkpointed(layout.subcatchments)),
            tuple((canonical_key(v.id), v.units) for v in checkpointed(layout.pollutants)),
            tuple(canonical_key(v) for v in checkpointed(layout.landuses)))


@dataclass(frozen=True, kw_only=True)
class HotstartManifest:
    sha256: str
    layout: HotstartLayout
    producer_input_sha256: str | None = None
    engine_sha256: str | None = None

    def __post_init__(self):
        _hash(self.sha256)
        if type(self.layout) is not HotstartLayout:
            raise TypeError('Manifest requires an explicit hotstart layout')
        for value in checkpointed((self.producer_input_sha256, self.engine_sha256)):
            if value is not None:
                _hash(value)

    @classmethod
    def asserted(cls, data, *, layout, producer_input_sha256=None, engine_sha256=None):
        """Record the caller's independently established producer/layout claim."""
        HotstartData.from_bytes(data, layout=layout)
        return cls(sha256=record_digest(data), layout=layout,
                   producer_input_sha256=producer_input_sha256, engine_sha256=engine_sha256)

    def verify(self, data, *, layout):
        if type(layout) is not HotstartLayout or not isinstance(data, bytes):
            raise TypeError('Verification requires immutable bytes and a hotstart layout')
        if record_digest(data) != self.sha256:
            raise ValueError('Hotstart bytes do not match the manifest SHA-256')
        if _identity(layout) != _identity(self.layout):
            raise ValueError('Hotstart manifest identities/order/types/methods/bindings/units differ from the target layout')
        return HotstartData.from_bytes(data, layout=layout)

    def to_bytes(self):
        value = {'schema': _SCHEMA, 'sha256': self.sha256, 'layout': _layout_value(self.layout),
                 'producer_input_sha256': self.producer_input_sha256, 'engine_sha256': self.engine_sha256}
        return (record_dumps(value)+'\n').encode('utf-8')

    @classmethod
    def from_bytes(cls, data):
        if not isinstance(data, bytes):
            raise TypeError('Manifest source must be immutable UTF-8 bytes')
        value = _fields(parse(data.decode('utf-8')), ('schema', 'sha256', 'layout', 'producer_input_sha256', 'engine_sha256'))
        if value['schema'] != _SCHEMA:
            raise ValueError('Unsupported hotstart manifest schema')
        return cls(sha256=value['sha256'], layout=_read_layout(value['layout']),
                   producer_input_sha256=value['producer_input_sha256'], engine_sha256=value['engine_sha256'])

    @classmethod
    def read(cls, path):
        return cls.from_bytes(Path(path).read_bytes())

    def write(self, path):
        return write_bytes(path, self.to_bytes())
