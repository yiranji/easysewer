"""Printed time-series tables, including dry-compressed LID report files."""

from datetime import datetime
from dataclasses import replace
import re

from ..model.identity import Ref
from ..results import ResultSource, ResultTable, ResultRow
from ..results.applicability import ResultContext, qualify_table
from ..validation import Diagnostic, Severity, ValidationReport
from ..validation._cooperative import checkpointed, checkpoint_scope
from .report_document import _splitlines, _sha256
from .report import ReportContext, ReportLayoutError
from .report_document import ReportDocument, ReportBlock, DEFAULT_REPORT_LIMIT
from ._report_tables import RowReader, col, _FLOW
from ._report_summaries import normalize, pollutant_names


def timestamp(reader):
    date=reader.take();clock=reader.take()
    raw=reader.text[date.start():clock.end()]
    if not re.fullmatch(r'\d{2}/\d{2}/\d{4}',date[0]) or not re.fullmatch(r'\d{2}:\d{2}:\d{2}',clock[0]):
        raise ReportLayoutError('Unrecognized printed calendar format')
    try:value=datetime.strptime(date[0]+' '+clock[0],'%m/%d/%Y %H:%M:%S')
    except ValueError as error:raise ReportLayoutError('Invalid printed calendar date') from error
    from ..results import ResultCell
    return ResultCell(value=value,raw=raw,unit=None,precision='second:1',span=reader.span(date.start(),clock.end()))


def detail_columns(block,context,source):
    lines=_splitlines(block.text);borders=[i for i,line in enumerate(checkpointed(lines)) if re.fullmatch(r'\s*-{3,}\s*',line)]
    if len(borders)!=2 or borders[1]-borders[0]!=3:raise ReportLayoutError('Unrecognized detail header')
    a,b=(normalize(line) for line in lines[borders[0]+1:borders[1]])
    base=block.target.collection
    if base=='swmm:nodes':
        match=re.fullmatch(r'Date Time '+_FLOW+r' \1 (feet|meters) \2(.*)',b)
        prefix='Inflow Flooding Depth Head'
        if not match or not a.startswith(prefix):raise ReportLayoutError('Unknown node detail columns')
        length={'feet':'ft','meters':'m'}[match[2]]
        names=('inflow','overflow','depth','head');units=(match[1],match[1],length,length)
        remaining=a[len(prefix):];quality_units=match[3].split()
    elif base=='swmm:links':
        match=re.fullmatch(r'Date Time '+_FLOW+r' (ft/sec|m/sec) (feet|meters) Setting(.*)',b)
        prefix='Flow Velocity Depth Capacity/'
        if not match or not a.startswith(prefix):raise ReportLayoutError('Unknown link detail columns')
        names=('flow','velocity','depth','capacity');units=(match[1],match[2],{'feet':'ft','meters':'m'}[match[3]],None)
        remaining=a[len(prefix):];quality_units=match[4].split()
    else:
        prefix='Date Time Precip. Losses Runoff'
        if not a.startswith(prefix):raise ReportLayoutError('Unknown subcatchment detail columns')
        remaining=a[len(prefix):].strip();tokens=b.split()
        if len(tokens)<3 or tokens[0] not in ('in/hr','mm/hr') or tokens[1]!=tokens[0] or not re.fullmatch(_FLOW,tokens[2]):
            raise ReportLayoutError('Unknown subcatchment detail units')
        names=['rainfall','losses','runoff'];units=tokens[:3];tokens=tokens[3:]
        if remaining.startswith('Snow Depth'):
            remaining=remaining[len('Snow Depth'):].strip()
            if not tokens or tokens[0] not in ('inches','mmeters'):raise ReportLayoutError('Unknown snow depth unit')
            names.append('snow_depth');units.append('in' if tokens.pop(0)=='inches' else 'mm')
        if remaining.startswith('GW Elev. GW Flow'):
            remaining=remaining[len('GW Elev. GW Flow'):].strip()
            if len(tokens)<2 or tokens[0] not in ('feet','meters') or not re.fullmatch(_FLOW,tokens[1]):raise ReportLayoutError('Unknown groundwater detail units')
            names.extend(('groundwater_elevation','groundwater_flow'));units.extend(({'feet':'ft','meters':'m'}[tokens[0]],tokens[1]));tokens=tokens[2:]
        quality_units=tokens
    if any(unit not in ('MG/L','UG/L','#/L') for unit in quality_units):raise ReportLayoutError('Unknown detail quality unit')
    pollutants=pollutant_names(remaining,len(quality_units),context,source,width=10) if quality_units else ()
    if not quality_units and remaining.strip():raise ReportLayoutError('Unknown extra detail labels')
    columns=(col('time',None,'native saved OUT timestamp printed to a second',kind='datetime'),)
    for name,unit in zip(names,units):
        meaning='printed saved OUT '+name+'; '+context.semantics
        if name=='losses':meaning='printed OUT evaporation / 24 plus infiltration, in the header rainfall-rate unit'
        if name=='capacity':meaning+='; conduit capacity or regulator/pump setting according to native link type'
        if name=='overflow' and context.accounting=='discrete-volume:1':meaning+='; adjusted external/system flooding, not retained ponding'
        columns+=(col(name,unit,meaning),)
    columns+=tuple(col('concentration',unit,
        'printed saved OUT concentration',pollutant=Ref(collection='swmm:pollutants',key=name)) for name,unit in zip(pollutants,quality_units))
    precisions=tuple(4 if base=='swmm:subcatchments' and c.key in ('swmm:runoff','swmm:groundwater_flow') else 3 for c in columns[1:])
    return columns,tuple((block.start_line+i,line) for i,line in enumerate(checkpointed(lines)) if i>borders[1] and line.strip()),precisions


def time_rows(lines,columns,source,target=None,*,precisions):
    rows=[];last=None
    for number,text in checkpointed(lines):
        reader=RowReader(number,text,source);when=timestamp(reader)
        if last is not None and when.value<last:raise ReportLayoutError('Printed times move backwards')
        last=when.value
        # Native %f fields may touch or exceed their minimum width. Their
        # explicit fractional digit counts still delimit each printed value.
        position=reader.tokens[1].end();values=[]
        for column,digits in zip(columns[1:],precisions):
            match=re.compile(r'\s*([+-]?\d+\.\d{'+str(digits)+r'})').match(text,position)
            if not match:raise ReportLayoutError('Invalid fixed-decimal observation')
            values.append(reader.number_cell(match[1],column.unit,match.start(1),match.end(1),'decimal:'+str(digits)))
            position=match.end()
        if text[position:].strip():raise ReportLayoutError('Extra printed observation fields')
        cells=(when,)+tuple(values)
        # Repeated rounded timestamps remain separate observations.
        rows.append(ResultRow(key=('swmm:observation',str(len(rows))),label=when.raw,target=target,cells=cells))
    return tuple(rows)


def read_detail(document,target,*,source,context,on_error='preserve',checkpoint=None):
    with checkpoint_scope(checkpoint):
        return _read_detail(document,target,source=source,context=context,on_error=on_error)


def _read_detail(document,target,*,source,context,on_error):
    if not isinstance(target,Ref) or target.collection not in ('swmm:nodes','swmm:links','swmm:subcatchments'):
        raise TypeError('Expected a subcatchment, node or link Ref')
    matches=[b for b in checkpointed(document.blocks) if b.kind=='detail' and b.target.canonical==target.canonical]
    key={'swmm:nodes':'swmm:node_detail','swmm:links':'swmm:link_detail','swmm:subcatchments':'swmm:subcatchment_detail'}[target.collection]
    status='present';reason=None;columns=rows=();issues=()
    try:
        if document.text is None:
            status='undecoded';reason='report_text_unavailable';raise ReportLayoutError('Report bytes could not be decoded')
        if not matches:status='absent';reason='object_detail_not_emitted'
        elif len(matches)!=1:raise ReportLayoutError('Multiple detail blocks for the same object')
        else:
            columns,lines,precisions=detail_columns(matches[0],context,source)
            rows=time_rows(lines,columns,source,matches[0].target,precisions=precisions)
            if not rows:status='empty';reason='no_observations_reported'
    except ReportLayoutError as error:
        if on_error=='raise':raise
        if status!='undecoded':status='unsupported_layout';reason='unrecognized_layout'
        columns=rows=();issues=(Diagnostic(code='report.'+status,severity=Severity.WARNING,message=str(error)),)
    columns=tuple(replace(column,applicability=context.result_context.output(target.collection,column.key,target=target))
                  if column.kind=='number' else column for column in columns)
    rows=tuple(replace(row,cells=tuple(replace(cell,value=None,missing_reason=column.applicability.reasons[0],qualifier='equal')
                                      if column.applicability.unavailable else cell for column,cell in zip(columns,row.cells))) for row in checkpointed(rows))
    return ResultTable(key=key,title=matches[0].title if matches else str(target.key),source=source,
        status=status,reason=reason,columns=columns,rows=rows,semantics='swmm:printed-out-observations; '+context.semantics,
        raw_text=''.join(b.text for b in matches),diagnostics=ValidationReport(diagnostics=issues),
        time_origin=context.report_start,decoding_strategy=document.decoding_strategy,
        target=matches[0].target if len(matches)==1 else target)


def lid_table(document,*,source,owner=None,expected_subcatchment=None,expected_control=None,on_error='preserve'):
    columns=rows=();status='present';reason=None;issues=();title='LID detail report'
    try:
        if document.text is None:
            status='undecoded';reason='report_text_unavailable';raise ReportLayoutError('LID report bytes could not be decoded')
        lines=_splitlines(document.text)
        if not lines or lines[0]!='SWMM5 LID Report File':raise ReportLayoutError('Not a native LID detail report')
        identities=[(i,re.fullmatch(r'LID Unit: (\S+) in Subcatchment (\S+)',line)) for i,line in enumerate(checkpointed(lines))]
        identities=[(i,m) for i,m in identities if m]
        if len(identities)!=1:raise ReportLayoutError('Ambiguous LID report identity')
        identity_line,identity=identities[0];title=identity[1]+' in '+identity[2]
        if expected_control is not None and identity[1]!=expected_control:raise ReportLayoutError('LID control differs from captured owner')
        if expected_subcatchment is not None and identity[2]!=expected_subcatchment:raise ReportLayoutError('LID subcatchment differs from captured owner')
        borders=[i for i,line in enumerate(checkpointed(lines)) if re.fullmatch(r'\s*[-\s]+',line) and '-' in line]
        if len(borders)!=1:raise ReportLayoutError('Unknown LID report border')
        end=borders[0];headers=[normalize(line) for line in lines[identity_line+1:end] if line.strip()]
        if len(headers)!=3 or headers[0]!='Elapsed Total Total Surface Pavement Soil Storage Surface Drain Surface Pavement Soil Storage' or headers[1]!='Time Inflow Evap Infil Perc Perc Exfil Runoff OutFlow Level Level Moisture Level':
            raise ReportLayoutError('Unknown LID report columns')
        match=re.fullmatch(r'Date Time Hours (in/hr|mm/hr) \1 \1 \1 \1 \1 \1 \1 (inches|mm) \2 Content \2',headers[2])
        if not match:raise ReportLayoutError('Unknown LID report units')
        rate=match[1];depth='in' if match[2]=='inches' else 'mm'
        columns=(col('time',None,'runoff-step calendar timestamp printed to a second',kind='datetime'),
                 col('elapsed_hours','h','elapsed simulation time rounded to 0.001 h'))
        names=('inflow','evaporation','surface_infiltration','pavement_percolation','soil_percolation','storage_exfiltration','surface_runoff','drain_outflow','surface_level','pavement_level','soil_moisture','storage_level')
        units=(rate,)*8+(depth,depth,None,depth)
        columns+=tuple(col(name,unit,'native runoff-step LID '+name+'; dry sequences compressed, omitted steps are not observations') for name,unit in zip(names,units))
        rows=time_rows(tuple((i+1,line) for i,line in enumerate(checkpointed(lines)) if i>end and line.strip()),columns,source,owner,
            precisions=(3,3,4)+(3,)*10)
        if not rows:status='empty';reason='no_lid_observations_emitted'
    except ReportLayoutError as error:
        if on_error=='raise':raise
        if status!='undecoded':status='unsupported_layout';reason='unrecognized_layout'
        columns=rows=();issues=(Diagnostic(code='report.'+status,severity=Severity.WARNING,message=str(error)),)
    return ResultTable(key='swmm:lid_detail',title=title,source=source,columns=columns,rows=rows,status=status,reason=reason,
        semantics='swmm:printed-lid-runoff-steps-dry-compressed',raw_text=document.text or '',
        diagnostics=ValidationReport(diagnostics=issues),decoding_strategy=document.decoding_strategy,target=owner)


def read_lid_report(path,*,encoding=None,max_bytes=DEFAULT_REPORT_LIMIT,on_decode_error='preserve',on_error='preserve',
                    owner=None,expected_subcatchment=None,expected_control=None,expected_sha256=None,expected_size=None,
                    run_id=None,input_sha256=None,backend_sha256=None,engine_version=None,result_context=ResultContext(),
                    checkpoint=None):
    with checkpoint_scope(checkpoint):
        return _read_lid_report(path,encoding=encoding,max_bytes=max_bytes,on_decode_error=on_decode_error,
            on_error=on_error,owner=owner,expected_subcatchment=expected_subcatchment,
            expected_control=expected_control,expected_sha256=expected_sha256,expected_size=expected_size,
            run_id=run_id,input_sha256=input_sha256,backend_sha256=backend_sha256,
            engine_version=engine_version,result_context=result_context)


def _read_lid_report(path,*,encoding=None,max_bytes=DEFAULT_REPORT_LIMIT,on_decode_error='preserve',on_error='preserve',
                    owner=None,expected_subcatchment=None,expected_control=None,expected_sha256=None,expected_size=None,
                    run_id=None,input_sha256=None,backend_sha256=None,engine_version=None,result_context=ResultContext()):
    if on_error not in ('preserve','raise'):raise ValueError('Unknown report error policy')
    if owner is not None and (not isinstance(owner,Ref) or owner.collection!='swmm:lid_usage'):
        raise TypeError('LID file owner must be a deployment reference')
    document=ReportDocument.read(path,encoding=encoding,max_bytes=max_bytes,on_decode_error=on_decode_error)
    sha=_sha256(document.raw)
    if (expected_sha256 is not None and sha!=expected_sha256) or (expected_size is not None and len(document.raw)!=expected_size):
        raise ValueError('LID report changed since the captured run')
    source=ResultSource(format='swmm:lid-report',path=str(path),sha256=sha,encoding=document.encoding,
        run_id=run_id,input_sha256=input_sha256,backend_sha256=backend_sha256,engine_version=engine_version)
    if not isinstance(result_context,ResultContext):raise TypeError('Expected execution result context')
    return qualify_table(lid_table(document,source=source,owner=owner,expected_subcatchment=expected_subcatchment,
                                  expected_control=expected_control,on_error=on_error),result_context)


def table_series(table,variable,*,pollutant=None,target=None,checkpoint=None):
    with checkpoint_scope(checkpoint):
        return _table_series(table,variable,pollutant=pollutant,target=target)


def _table_series(table,variable,*,pollutant=None,target=None):
    """Project a printed time table; do not fill gaps or invent subsecond times."""
    from ..results import ResultSeries
    from .output_metadata import NotRecordedError
    if table.status=='absent':raise NotRecordedError('No printed observations for this object')
    if table.status not in ('present','empty'):raise ReportLayoutError('Table is not decoded and structurally valid')
    if pollutant is not None and (not isinstance(pollutant,Ref) or pollutant.collection!='swmm:pollutants'):
        raise TypeError('Expected a pollutant Ref')
    identity=variable,pollutant.canonical if pollutant else None
    matches=[(i,c) for i,c in enumerate(table.columns) if (c.key,c.pollutant.canonical if c.pollutant else None)==identity]
    if len(matches)!=1 or matches[0][1].kind!='number':raise KeyError('No numeric printed column for '+str(identity))
    index,column=matches[0]
    if not table.columns or table.columns[0].kind!='datetime':raise TypeError('Expected a printed time table')
    times=tuple(row.cells[0].value for row in checkpointed(table.rows))
    if any(b<=a for a,b in zip(times,times[1:])):
        raise ReportLayoutError('Printed dates are not strictly increasing; use table rows to retain rounded duplicate timestamps')
    if target is not None and table.target is not None and target.canonical!=table.target.canonical:
        raise ValueError('Requested series target differs from its captured table')
    if table.target is not None:target=table.target
    if target is None and table.rows:
        targets={row.target for row in checkpointed(table.rows)}
        if len(targets)==1:target=targets.pop()
    return ResultSeries(source=table.source,target=target,variable=variable,pollutant=column.pollutant,unit=column.unit,
        applicability=column.applicability,
        periods=tuple(range(len(times))),times=times,values=tuple(row.cells[index].value for row in checkpointed(table.rows)),
        missing=tuple(row.cells[index].missing_reason for row in checkpointed(table.rows)),sampling=table.semantics,
        semantics=column.semantics+'; printed precision retained in table cells; omitted intervals are not interpolated')
