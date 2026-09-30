"""Explicit report table registration; unfamiliar layouts remain visible as text."""

from dataclasses import dataclass, replace
from datetime import datetime

from ..model.identity import canonical_key, namespace_key
from ..results import ResultSource
from ..results.tables import ResultTable
from ..results.applicability import ResultContext, qualify_table
from ..validation import Diagnostic, Severity, ValidationReport
from ..validation._cooperative import checkpointed, checkpoint_scope
from .report_document import ReportDocument, _sha256


class ReportLayoutError(ValueError):
    """The source cannot be assigned the registered column meanings safely."""


@dataclass(frozen=True, kw_only=True)
class ReportContext:
    producer: str | None = None
    numerical_policy: str | None = None
    report_start: datetime | None = None
    averages: bool | None = None
    accounting: str | None = None
    pollutants: tuple[str, ...] | None = None
    flow_units: str | None = None
    rain_gage_sources: tuple[tuple[str, str], ...] | None = None
    result_context: ResultContext = ResultContext()

    def __post_init__(self):
        if not isinstance(self.result_context,ResultContext):raise TypeError('Expected execution result context')
        if self.flow_units is not None:
            from ..model.units import UnitContext
            UnitContext(flow_units=self.flow_units)
        if self.rain_gage_sources is not None:
            if type(self.rain_gage_sources) is not tuple or any(type(row) is not tuple or len(row)!=2 or type(row[0]) is not str or row[1] not in ('FILE','TIMESERIES') for row in self.rain_gage_sources):
                raise TypeError('Rain gage source context requires immutable name/kind pairs')
            if len({canonical_key(row[0]) for row in self.rain_gage_sources})!=len(self.rain_gage_sources):
                raise ValueError('Duplicate rain gage identity')
        if self.producer is not None:namespace_key(self.producer)
        if self.numerical_policy is not None and type(self.numerical_policy) is not str:
            raise TypeError('Invalid numerical policy')
        if self.report_start is not None and (type(self.report_start) is not datetime or self.report_start.tzinfo is not None):
            raise ValueError('Report start must use the timezone-free model calendar')
        if self.averages is not None and type(self.averages) is not bool:
            raise TypeError('Invalid report averaging context')
        if self.accounting is not None and type(self.accounting) is not str:
            raise TypeError('Invalid report accounting context')
        if self.pollutants is not None:
            if type(self.pollutants) is not tuple or any(type(p) is not str for p in self.pollutants):
                raise TypeError('Pollutant context requires an ordered tuple of names')
            if len({canonical_key(p) for p in self.pollutants}) != len(self.pollutants):
                raise ValueError('Duplicate pollutant identity')

    @property
    def semantics(self):
        return ('swmm:printed-native-statistics; producer='+str(self.producer or 'unspecified')+
                '; policy='+str(self.numerical_policy or 'unspecified')+'; averages='+str(self.averages)+
                '; accounting='+str(self.accounting or 'unspecified'))


@dataclass(frozen=True, kw_only=True)
class ReportTableCodec:
    key: str
    title: str
    parser: object  # trusted callable(block, context, source) -> (columns, rows)
    multiple: str = 'reject'
    block_kind: str = 'section'

    def __post_init__(self):
        namespace_key(self.key)
        if type(self.title) is not str or not self.title or not callable(self.parser):
            raise TypeError('Report codecs require a title and parser callable')
        if self.multiple not in ('reject','columns'):
            raise ValueError('Unknown report multi-block policy')
        if self.block_kind not in ('section','detail','preamble','timing'):
            raise ValueError('Unknown report block kind')


@dataclass(frozen=True)
class ReportTables:
    codecs: tuple[ReportTableCodec, ...] = ()

    def __post_init__(self):
        if type(self.codecs) is not tuple or any(not isinstance(codec, ReportTableCodec) for codec in self.codecs):
            raise TypeError('Report registry requires immutable codecs')
        if len({codec.key for codec in self.codecs}) != len(self.codecs) or len({(codec.block_kind,codec.title) for codec in self.codecs}) != len(self.codecs):
            raise ValueError('Duplicate report key or source title')

    def with_codecs(self, *codecs):
        return ReportTables(self.codecs+tuple(codecs))

    def get(self, key):
        for codec in self.codecs:
            if codec.key == key:return codec
        raise KeyError('Unregistered report table: '+str(key))

    @property
    def keys(self):
        return tuple(codec.key for codec in self.codecs)


def swmm_report_tables():
    from ._report_tables import builtins
    from ._report_summaries import builtins as summaries
    from ._report_status import builtins as status
    return ReportTables(tuple(ReportTableCodec(key=key, title=title, parser=parser,
        multiple='columns' if key in ('swmm:runoff_quality_continuity','swmm:quality_routing_continuity') else 'reject')
        for key, title, parser in (*builtins(),*summaries()))+tuple(status()))


def _context(document,context):
    import re
    units=[m[1] for block in checkpointed(document.blocks) if block.kind=='section' and block.title=='Analysis Options'
           for m in re.finditer(r'^\s*Flow Units\s+\.{2,}\s+(CFS|GPM|MGD|CMS|LPS|MLD)\s*$',block.text,re.M)]
    if context.flow_units is not None and any(unit!=context.flow_units for unit in units):
        raise ValueError('Report flow units differ from the asserted source context')
    if context.flow_units is None and len(units)==1:context=replace(context,flow_units=units[0])
    replay = any(block.kind=='section' and block.title=='Runoff Quantity Continuity'
                 and re.search(r'^\s*Runoff supplied by interface file .+\s*$', block.text, re.M)
                 for block in checkpointed(document.blocks))
    if replay:
        known=context.result_context.fact('swmm:runoff')
        if known not in (None,'replayed'):raise ValueError('RPT replay marker conflicts with execution context')
        if known is None:
            evidence=('report-sha256:'+_sha256(document.raw),)
            context=replace(context,result_context=ResultContext(policy='swmm:result-context:1',
                facts=(('swmm:runoff','replayed'),),evidence=evidence))
    return context


def read_report_tables(document, keys, *, registry=None, source=None, context=None, on_error='preserve', checkpoint=None):
    with checkpoint_scope(checkpoint):
        return _read_report_tables(document,keys,registry=registry,source=source,context=context,on_error=on_error)


def _read_report_tables(document, keys, *, registry, source, context, on_error):
    if not isinstance(document, ReportDocument) or type(keys) is not tuple:
        raise TypeError('Expected a ReportDocument and a tuple of table keys')
    if len(set(keys)) != len(keys):raise ValueError('Duplicate requested report key')
    if on_error not in ('preserve', 'raise'):raise ValueError('Unknown report table error policy')
    registry=swmm_report_tables() if registry is None else registry
    context=ReportContext() if context is None else context
    if not isinstance(registry, ReportTables) or not isinstance(context, ReportContext):
        raise TypeError('Invalid report registry or context')
    context=_context(document,context)
    sha=_sha256(document.raw)
    source=source or ResultSource(format='swmm:rpt',path=document.source,sha256=sha,encoding=document.encoding)
    if not isinstance(source, ResultSource) or source.sha256 != sha or source.encoding != document.encoding:
        raise ValueError('Report source must identify these exact decoded bytes')
    result=[]
    for key in checkpointed(keys,interval=1):
        codec=registry.get(key)
        matches=[block for block in checkpointed(document.blocks) if block.kind == codec.block_kind and block.title == codec.title]
        status='present';reason=None;columns=rows=();issues=()
        raw=''.join(block.text for block in matches)
        try:
            if document.text is None:
                status='undecoded';reason='report_text_unavailable'
                raise ReportLayoutError('Report bytes could not be decoded')
            if not matches:
                status='absent';reason='table_not_emitted'
            elif len(matches) != 1 and codec.multiple=='reject':
                raise ReportLayoutError('Multiple blocks share this table title')
            else:
                columns, rows=codec.parser(matches[0],context,source)
                for block in checkpointed(matches[1:],interval=1):
                    added, right=codec.parser(block,context,source)
                    if tuple((r.key,r.target,r.label) for r in rows)!=tuple((r.key,r.target,r.label) for r in right):
                        raise ReportLayoutError('Repeated column blocks have different row identities')
                    columns+=added
                    rows=tuple(replace(a,cells=a.cells+b.cells) for a,b in checkpointed(zip(rows,right)))
                column_ids=[(c.key,c.pollutant.canonical if c.pollutant else None) for c in columns]
                row_ids=[canonical_key(row.key) for row in checkpointed(rows)]
                if len(set(column_ids)) != len(column_ids) or len(set(row_ids)) != len(row_ids):
                    raise ReportLayoutError('Duplicate printed row or column identity')
                if not rows:status='empty';reason='no_rows_reported'
        except ReportLayoutError as error:
            if on_error == 'raise':raise
            if status != 'undecoded':status='unsupported_layout';reason='unrecognized_layout'
            columns=rows=()
            issues=(Diagnostic(code='report.'+status, severity=Severity.WARNING,
                message=f'{key}: {error}; source text/bytes retained',section=codec.title),)
        table=ResultTable(key=key,title=codec.title,source=source,columns=columns,rows=rows,
            status=status,reason=reason,semantics=context.semantics,raw_text=raw,
            diagnostics=ValidationReport(diagnostics=issues),time_origin=context.report_start,
            decoding_strategy=document.decoding_strategy)
        result.append(qualify_table(table,context.result_context))
    return tuple(result)


class ReportReader:
    """A query view of captured immutable bytes, with no native/file handle."""

    def __init__(self,document,*,registry=None,source=None,context=None,on_error='preserve',checkpoint=None):
        with checkpoint_scope(checkpoint):
            self._initialize(document,registry=registry,source=source,context=context,on_error=on_error)

    def _initialize(self,document,*,registry,source,context,on_error):
        if not isinstance(document,ReportDocument):raise TypeError('Expected a ReportDocument')
        self.document=document
        self.source=source or ResultSource(format='swmm:rpt',path=document.source,
            sha256=_sha256(document.raw),encoding=document.encoding)
        self.context=_context(document,ReportContext() if context is None else context)
        self.registry=swmm_report_tables() if registry is None else registry
        self.on_error=on_error
        read_report_tables(document,(),registry=self.registry,source=self.source,context=self.context,on_error=on_error)

    @property
    def targets(self):
        return tuple(block.target for block in self.document.blocks if block.kind=='detail')

    def table(self,key,*,checkpoint=None):
        return read_report_tables(self.document,(key,),registry=self.registry,source=self.source,
            context=self.context,on_error=self.on_error,checkpoint=checkpoint)[0]

    def detail(self,target,*,checkpoint=None):
        from .report_details import read_detail
        return read_detail(self.document,target,source=self.source,context=self.context,on_error=self.on_error,checkpoint=checkpoint)

    def series(self,target,variable,*,pollutant=None,checkpoint=None):
        from .report_details import table_series
        with checkpoint_scope(checkpoint):
            return table_series(self.detail(target),variable,pollutant=pollutant)
