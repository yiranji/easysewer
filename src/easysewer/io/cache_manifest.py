"""Explicit caller assertions for positional RUNOFF and binary RDII caches."""

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

from ..model.identity import canonical_key
from ._atomic import write_bytes
from .hotstart import StatePollutant
from .hotstart_manifest import _hash, _fields
from .json.document import parse
from .runoff_cache import RunoffLayout, RunoffData, RdiiLayout, RdiiBindingIdentity, RdiiData
from ..validation._cooperative import checkpointed
from ._record_work import record_asdict as asdict, record_dumps, record_digest


def _identity(layout):
    if type(layout) is RunoffLayout:
        return (layout.flow_units, tuple(canonical_key(v) for v in checkpointed(layout.subcatchments)),
                tuple((canonical_key(v.id), v.units) for v in checkpointed(layout.pollutants)))
    return (tuple(canonical_key(v) for v in checkpointed(layout.nodes)),
            tuple(sorted((canonical_key(v.node), canonical_key(v.hydrograph)) for v in checkpointed(layout.bindings))))


def _read(data, layout):
    if type(layout) is RunoffLayout:
        return RunoffData.from_bytes(data, layout=layout)
    if type(layout) is not RdiiLayout:
        raise TypeError('Cache evidence requires RunoffLayout or RdiiLayout')
    result = RdiiData.from_bytes(data)
    result.validate_layout(layout)
    return result


@dataclass(frozen=True, kw_only=True)
class CacheManifest:
    sha256: str
    layout: RunoffLayout | RdiiLayout
    producer_input_sha256: str | None = None
    engine_sha256: str | None = None

    def __post_init__(self):
        _hash(self.sha256)
        if type(self.layout) not in (RunoffLayout, RdiiLayout):
            raise TypeError('Cache manifest requires RunoffLayout or RdiiLayout')
        for value in checkpointed((self.producer_input_sha256, self.engine_sha256)):
            if value is not None:
                _hash(value)

    @property
    def kind(self):
        return 'RUNOFF' if type(self.layout) is RunoffLayout else 'RDII'

    @classmethod
    def asserted(cls, data, *, layout, producer_input_sha256=None, engine_sha256=None):
        """Record independently established producer identity; do not infer it."""
        _read(data, layout)
        return cls(sha256=record_digest(data), layout=layout,
                   producer_input_sha256=producer_input_sha256, engine_sha256=engine_sha256)

    def verify(self, data, *, layout):
        if not isinstance(data, bytes) or type(layout) is not type(self.layout):
            raise TypeError('Manifest verification requires immutable bytes and the matching layout type')
        if record_digest(data) != self.sha256:
            raise ValueError('Cache bytes differ from the manifest SHA-256')
        if _identity(layout) != _identity(self.layout):
            raise ValueError('Cache identities/order/units/nodal bindings differ from the target layout')
        return _read(data, layout)

    def to_bytes(self):
        value = {'schema': 'easysewer.cache-manifest/1', 'kind': self.kind, 'sha256': self.sha256,
                 'layout': asdict(self.layout), 'producer_input_sha256': self.producer_input_sha256, 'engine_sha256': self.engine_sha256}
        return (record_dumps(value)+'\n').encode('utf-8')

    @classmethod
    def from_bytes(cls, data):
        if not isinstance(data, bytes):
            raise TypeError('Cache manifest requires immutable UTF-8 bytes')
        value = _fields(parse(data.decode('utf-8')), ('schema','kind','sha256','layout','producer_input_sha256','engine_sha256'))
        if value['schema'] != 'easysewer.cache-manifest/1':
            raise ValueError('Unknown cache manifest schema')
        fields = value['layout']
        if value['kind'] == 'RUNOFF':
            _fields(fields, ('flow_units','subcatchments','pollutants'))
            if type(fields['subcatchments']) is not list or type(fields['pollutants']) is not list:
                raise TypeError('Manifest collections must be JSON arrays')
            layout = RunoffLayout(flow_units=fields['flow_units'], subcatchments=tuple(fields['subcatchments']),
                pollutants=tuple(StatePollutant(**_fields(v, ('id','units'))) for v in checkpointed(fields['pollutants'])))
        elif value['kind'] == 'RDII':
            _fields(fields, ('nodes','bindings'))
            if type(fields['nodes']) is not list or type(fields['bindings']) is not list:
                raise TypeError('Manifest collections must be JSON arrays')
            layout = RdiiLayout(nodes=tuple(fields['nodes']), bindings=tuple(RdiiBindingIdentity(**_fields(v, ('node','hydrograph'))) for v in checkpointed(fields['bindings'])))
        else:
            raise ValueError('Unknown cache manifest kind')
        return cls(sha256=value['sha256'], layout=layout, producer_input_sha256=value['producer_input_sha256'], engine_sha256=value['engine_sha256'])

    @classmethod
    def read(cls, path):
        return cls.from_bytes(Path(path).read_bytes())

    def write(self, path):
        return write_bytes(path, self.to_bytes())
