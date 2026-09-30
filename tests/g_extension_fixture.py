"""Test-only two-generation feature; registered solely through public contracts."""
from dataclasses import dataclass

from easysewer.io.inp.network import default_schema
from easysewer.io.json import JsonField, JsonType
from easysewer.model import CollectionSpec, Ref
from easysewer.model.fields import number, reference, validate_fields
from easysewer.schema import DecodedFeature, FeatureDescriptor
from easysewer.schema.structured import EncodedRow, FeatureData, RecordEntry, SourceBinding
from easysewer.validation import Diagnostic, Severity, ValidationReport

COLLECTION = 'test:g-probes'
SECTION = 'G_PROBES'
DESCRIPTOR = FeatureDescriptor(key=COLLECTION, sections={SECTION},
                               ordered_sections={SECTION}, atomic_write=True)


@dataclass(frozen=True, kw_only=True)
class Limit:
    pass


@dataclass(frozen=True, kw_only=True)
class FixedLimit(Limit):
    depth: float = number('depth', minimum=0)


@dataclass(frozen=True, kw_only=True)
class BandLimit(Limit):
    lower: float = number('depth', minimum=0)
    upper: float = number('depth', minimum=0)

    def validate_local(self):
        if self.lower > self.upper:
            yield Diagnostic(code='test.g_band_order', message='Lower bound exceeds upper bound')


@dataclass(frozen=True, kw_only=True)
class Probe:
    id: str
    node: Ref = reference('swmm:nodes')
    limit: Limit


@dataclass(frozen=True, kw_only=True)
class FutureProbe(Probe):
    backup: Ref | None = reference('swmm:nodes', None)


class ProbeCodec:
    collections = (CollectionSpec(key=COLLECTION, record_type=Probe,
        key_of=lambda row: row.id, identity_field='id', validate=validate_fields),)

    def __init__(self, generation):
        self.generation = generation

    def decode(self, document, profile):
        records, bindings, issues = [], [], []
        for line in document.records(SECTION):
            values = line.values
            fixed = len(values) >= 3 and values[2] == 'FIXED'
            band = len(values) >= 3 and values[2] == 'BAND'
            width = 4 if fixed else 5
            supported = ((fixed and len(values) == 4) if self.generation == 1 else
                         ((fixed or band) and len(values) in (width, width + 1)))
            if not supported:
                issues.append(Diagnostic(code='test.g_unknown', severity=Severity.WARNING,
                    message='Unknown probe syntax is retained by the document layer'))
                continue
            try:
                limit = FixedLimit(depth=float(values[3])) if fixed else BandLimit(
                    lower=float(values[3]), upper=float(values[4]))
                options = dict(id=values[0], node=Ref(collection='swmm:nodes', key=values[1]), limit=limit)
                if self.generation == 2:
                    options['backup'] = Ref(collection='swmm:nodes', key=values[-1]) if len(values) > width else None
                record = (Probe if self.generation == 1 else FutureProbe)(**options)
            except ValueError as error:
                issues.append(Diagnostic(code='test.g_invalid', message=str(error)))
                continue
            records.append(RecordEntry(collection=COLLECTION, value=record))
            bindings.append(SourceBinding(line=line.number, key=(record.id,)))
        return DecodedFeature(value=FeatureData(records=tuple(records), bindings=tuple(bindings)),
            claimed_lines=frozenset(row.line for row in bindings), report=ValidationReport(diagnostics=tuple(issues)))

    def encode(self, store, profile):
        for row in store.collection(COLLECTION).values():
            values = (row.id, row.node.key)
            if isinstance(row.limit, FixedLimit):
                values += ('FIXED', str(row.limit.depth))
            else:
                values += ('BAND', str(row.limit.lower), str(row.limit.upper))
            if isinstance(row, FutureProbe) and row.backup is not None:
                values += (row.backup.key,)
            yield EncodedRow(key=(row.id,), section=SECTION, values=values,
                             owners=(Ref(collection=COLLECTION, key=row.id),))

    def validate(self, store, profile):
        return ()


def schema(generation):
    result = default_schema()
    result.register(DESCRIPTOR, ProbeCodec(generation))
    field = lambda name, shape: JsonField(name=name, attribute=name, shape=shape)
    result.register_json(JsonType(key='test:g-fixed', value_type=FixedLimit, bases=('test:g-limit',),
        fields=(field('depth', ('number',)),)))
    fields = (field('id', ('string',)), field('node', ('object', 'core:ref')),
              field('limit', ('object', 'test:g-limit')))
    if generation == 2:
        result.register_json(JsonType(key='test:g-band', value_type=BandLimit, bases=('test:g-limit',),
            fields=(field('lower', ('number',)), field('upper', ('number',)))))
        fields += (field('backup', ('union', ('null',), ('object', 'core:ref'))),)
    result.register_json(JsonType(key='test:g-probe', value_type=Probe if generation == 1 else FutureProbe,
        fields=fields, defaults=() if generation == 1 else (('backup', None),)))
    return result


SOURCE = ('; future extension 中文\r\n[JUNCTIONS]\r\nJ 10\r\nSpare 2\r\n'
    '[G_PROBES]\r\nFIRST J FIXED 1.5 Spare ; new backup field\r\n'
    '[G_PROBES]\r\nMIDDLE J BAND 2 4 Spare ; new nested variant\r\n'
    'LAST Spare FIXED .5 ; known row\r\n')
