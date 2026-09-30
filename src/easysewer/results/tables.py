"""Portable, immutable report tables with cell-level provenance and missingness."""

from dataclasses import dataclass
from datetime import datetime, timedelta
import math

from ..io.json import JsonDocument
from ..model.identity import Ref, canonical_key, namespace_key
from ..validation import Diagnostic, SourceSpan, ValidationReport
from ..validation.diagnostics import diagnostic_from_data
from ..validation._cooperative import checkpointed, checkpoint_scope
from ..io._record_work import record_asdict, record_dumps
from .series import ResultSource
from .applicability import ResultApplicability


@dataclass(frozen=True, kw_only=True)
class ResultColumn:
    key: str
    unit: str | None
    kind: str
    semantics: str
    pollutant: Ref | None = None
    applicability: ResultApplicability = ResultApplicability()

    def __post_init__(self):
        if not isinstance(self.applicability,ResultApplicability):raise TypeError('Expected column applicability')
        namespace_key(self.key)
        if self.kind not in ('number', 'text', 'elapsed', 'datetime'):
            raise ValueError('Unknown result column kind')
        if type(self.semantics) is not str or not self.semantics:
            raise ValueError('Result columns require explicit semantics')
        if self.unit is not None and (type(self.unit) is not str or not self.unit):
            raise ValueError('Invalid column unit')
        if self.pollutant is not None and (not isinstance(self.pollutant, Ref) or self.pollutant.collection != 'swmm:pollutants'):
            raise TypeError('Expected a pollutant reference')


@dataclass(frozen=True, kw_only=True)
class ResultCell:
    value: float | str | timedelta | datetime | None
    raw: str
    unit: str | None
    precision: str | None
    span: SourceSpan | None
    missing_reason: str | None = None
    qualifier: str = 'equal'

    def __post_init__(self):
        if type(self.raw) is not str:
            raise TypeError('Cells require their original text')
        if self.qualifier not in ('equal', 'greater_than', 'less_than'):
            raise ValueError('Unknown report value qualifier')
        if self.span is not None and not isinstance(self.span, SourceSpan):
            raise TypeError('Invalid cell source position')
        if self.value is None:
            if type(self.missing_reason) is not str or not self.missing_reason:
                raise ValueError('Missing cells require a reason')
        elif self.missing_reason is not None:
            raise ValueError('Present cells cannot have a missing reason')
        elif type(self.value) in (int, float):
            if not math.isfinite(self.value):
                raise ValueError('Nonfinite report value')
        elif type(self.value) is datetime and self.value.tzinfo is not None:
            raise ValueError('Report dates use the timezone-free model calendar')
        elif type(self.value) not in (str, timedelta, datetime):
            raise TypeError('Unsupported report value')
        if self.qualifier != 'equal' and type(self.value) not in (int,float):
            raise ValueError('Only numeric observations may carry inequality qualifiers')
        for name in ('unit', 'precision'):
            value=getattr(self, name)
            if value is not None and (type(value) is not str or not value):
                raise ValueError('Invalid cell '+name)


@dataclass(frozen=True, kw_only=True)
class ResultRow:
    key: str | tuple[str, ...]
    label: str
    target: Ref | None
    cells: tuple[ResultCell, ...]

    def __post_init__(self):
        canonical_key(self.key)
        if type(self.key) not in (str,tuple) or type(self.label) is not str:
            raise TypeError('Rows require a string/composite identity and a string label')
        if self.target is not None and not isinstance(self.target, Ref):
            raise TypeError('Expected a row object reference')
        if type(self.cells) is not tuple or any(not isinstance(cell, ResultCell) for cell in self.cells):
            raise TypeError('Rows require immutable result cells')


@dataclass(frozen=True, kw_only=True)
class ResultTable:
    key: str
    title: str
    source: ResultSource
    columns: tuple[ResultColumn, ...]
    rows: tuple[ResultRow, ...]
    status: str
    semantics: str
    raw_text: str = ''
    reason: str | None = None
    diagnostics: ValidationReport = ValidationReport()
    time_origin: datetime | None = None
    decoding_strategy: str = 'strict'
    target: Ref | None = None
    applicability: ResultApplicability = ResultApplicability()

    def __post_init__(self):
        if not isinstance(self.applicability,ResultApplicability):raise TypeError('Expected table applicability')
        namespace_key(self.key)
        if not isinstance(self.source, ResultSource) or not isinstance(self.diagnostics, ValidationReport):
            raise TypeError('Invalid table source or diagnostics')
        if self.target is not None and not isinstance(self.target,Ref):
            raise TypeError('Expected a table object reference')
        for name in ('columns', 'rows'):
            if type(getattr(self, name)) is not tuple:
                raise TypeError('Result tables require immutable tuples')
        if any(not isinstance(c, ResultColumn) for c in self.columns) or any(not isinstance(r, ResultRow) for r in checkpointed(self.rows)):
            raise TypeError('Invalid table columns or rows')
        identities=[(c.key, c.pollutant.canonical if c.pollutant else None) for c in self.columns]
        row_ids=[canonical_key(row.key) for row in checkpointed(self.rows)]
        if len(set(identities)) != len(identities) or len(set(row_ids)) != len(row_ids):
            raise ValueError('Duplicate table column or row identity')
        if self.status not in ('present', 'empty', 'absent', 'undecoded', 'unsupported_layout'):
            raise ValueError('Unknown report table status')
        if self.status != 'present' and (self.rows or not self.reason):
            raise ValueError('Unavailable/empty tables need a reason and no rows')
        if self.status == 'present' and (not self.rows or self.reason is not None):
            raise ValueError('Present table requires rows and no missing reason')
        if self.reason is not None and type(self.reason) is not str:
            raise TypeError('Table missing reason requires a string')
        if self.time_origin is not None and (type(self.time_origin) is not datetime or self.time_origin.tzinfo is not None):
            raise ValueError('Report time origin is a timezone-free model date')
        for name in ('title', 'semantics', 'decoding_strategy'):
            if type(getattr(self, name)) is not str or not getattr(self, name):
                raise ValueError('Missing table '+name)
        if type(self.raw_text) is not str:
            raise TypeError('Raw table text must be a string')
        for row in checkpointed(self.rows):
            if self.target is not None and row.target is not None and row.target.canonical!=self.target.canonical:
                raise ValueError('Row target differs from its object table')
            if len(row.cells) != len(self.columns):
                raise ValueError('Table row width differs from its columns')
            for column, cell in zip(self.columns, row.cells):
                if column.kind == 'number' and column.applicability.unavailable and cell.value is not None:
                    raise ValueError('Uncomputed report numbers must retain only their raw tokens')
                if cell.value is not None and not (
                    (column.kind == 'number' and type(cell.value) in (int, float)) or
                    (column.kind == 'text' and type(cell.value) is str) or
                    (column.kind == 'elapsed' and type(cell.value) is timedelta) or
                    (column.kind == 'datetime' and type(cell.value) is datetime)):
                    raise TypeError('Cell value differs from column kind')

    def row(self, key):
        if isinstance(key, Ref):
            matches=[row for row in self.rows if row.target is not None and row.target.canonical == key.canonical]
        else:
            matches=[row for row in self.rows if canonical_key(row.key) == canonical_key(key)]
        if len(matches) != 1:
            raise KeyError(f'Expected one report row for {key!r}, found {len(matches)} ({self.status})')
        return matches[0]

    def cell(self, row, column, *, pollutant=None):
        if pollutant is not None and not isinstance(pollutant, Ref):
            raise TypeError('Expected a pollutant reference')
        identity=(column, pollutant.canonical if pollutant else None)
        for index, definition in enumerate(self.columns):
            if (definition.key, definition.pollutant.canonical if definition.pollutant else None) == identity:
                return self.row(row).cells[index]
        raise KeyError(f'No report column {identity!r}')

    def to_json_document(self, *, version='1.2', checkpoint=None):
        with checkpoint_scope(checkpoint):
            return self._to_json_document(version=version)

    def _to_json_document(self, *, version):
        if version not in ('1.1','1.2'):
            raise ValueError('Unsupported result-table output version')
        data=record_asdict(self)
        if version == '1.1':
            for diagnostic in checkpointed(data['diagnostics']['diagnostics']):
                if diagnostic['subject'] is not None or diagnostic['related'] or diagnostic['locations']:
                    raise ValueError('Result-table 1.1 cannot retain diagnostic locations')
                for name in ('subject','related','locations'):del diagnostic[name]
        for row in checkpointed(data['rows']):
            for cell in checkpointed(row['cells']):
                if isinstance(cell['value'], timedelta):
                    cell['value']={'kind': 'elapsed', 'microseconds': cell['value']//timedelta(microseconds=1)}
                elif isinstance(cell['value'], datetime):
                    cell['value']={'kind': 'datetime', 'value': cell['value'].isoformat(timespec='microseconds')}
        data['time_origin']=self.time_origin.isoformat() if self.time_origin else None
        data.update(schema_version=version, kind='easysewer:result-table')
        return JsonDocument.from_text(record_dumps(data)+'\n')

    @classmethod
    def from_json_document(cls, document):
        data=document.data
        expected={'key','title','source','columns','rows','status','semantics','raw_text','reason','diagnostics',
                  'time_origin','decoding_strategy','schema_version','kind'}
        if type(data) is not dict or data.get('schema_version') not in ('1.0','1.1','1.2') or data.get('kind') != 'easysewer:result-table':
            raise ValueError('Unsupported result-table JSON contract')
        current=data['schema_version']!='1.0'
        locations=data['schema_version']=='1.2'
        if current:expected.add('applicability')
        if set(data)-{'target'} != expected:raise ValueError('Invalid result-table fields')
        if any(('applicability' in column) != current for column in data['columns']):
            raise ValueError('Column applicability differs from the table JSON contract')
        data=dict(data);data.pop('kind');data.pop('schema_version')
        data['applicability']=ResultApplicability.from_data(data['applicability']) if 'applicability' in data else ResultApplicability()
        def ref(value):
            if value is None:return None
            if type(value) is not dict or set(value) != {'collection','key'}:
                raise ValueError('Invalid result reference')
            return Ref(collection=value['collection'],key=tuple(value['key']) if isinstance(value['key'],list) else value['key'])
        def span(value):return None if value is None else SourceSpan(**value)
        def cell(value):
            value=dict(value)
            if type(value['value']) is dict:
                duration=value['value']
                if set(duration)=={'kind','value'} and duration['kind']=='datetime':
                    value['value']=datetime.fromisoformat(duration['value'])
                elif set(duration)=={'kind','microseconds'} and duration['kind']=='elapsed' and type(duration['microseconds']) is int:
                    value['value']=timedelta(microseconds=duration['microseconds'])
                else:raise ValueError('Invalid temporal value')
            value['span']=span(value['span'])
            return ResultCell(**value)
        data['source']=ResultSource(**data['source'])
        data['target']=ref(data.get('target'))
        data['columns']=tuple(ResultColumn(**dict(c,pollutant=ref(c['pollutant']),
            applicability=ResultApplicability.from_data(c['applicability']) if 'applicability' in c else ResultApplicability())) for c in data['columns'])
        data['rows']=tuple(ResultRow(**dict(r,key=tuple(r['key']) if type(r['key']) is list else r['key'],
            target=ref(r['target']),cells=tuple(cell(c) for c in r['cells']))) for r in data['rows'])
        if (type(data['diagnostics']) is not dict or set(data['diagnostics']) != {'diagnostics'} or
                type(data['diagnostics']['diagnostics']) is not list):
            raise ValueError('Invalid result-table diagnostics')
        data['diagnostics']=ValidationReport(diagnostics=tuple(diagnostic_from_data(d, locations=locations) for d in data['diagnostics']['diagnostics']))
        data['time_origin']=datetime.fromisoformat(data['time_origin']) if data['time_origin'] else None
        return cls(**data)
