"""Native status records: options, rainfall/RDII, ordered actions and wall time."""

from datetime import datetime, timedelta
import re

from ..model.identity import Ref
from ..results import ResultCell, ResultRow
from .report import ReportLayoutError, ReportTableCodec
from ..validation._cooperative import checkpointed
from .report_document import _splitlines
from ._report_tables import RowReader, col, numeric_tail
from ._report_summaries import missing, normalize


def content(block):
    return tuple((block.start_line+i,line) for i,line in enumerate(checkpointed(_splitlines(block.text))) if i>=3 and line.strip())


def text_cell(reader, start, end):
    raw=reader.text[start:end]
    return ResultCell(value=raw,raw=raw,unit=None,precision='text',span=reader.span(start,end))


def calendar(reader,start,end):
    raw=reader.text[start:end]
    if not re.fullmatch(r'\d{2}/\d{2}/\d{4} \d{2}:\d{2}:\d{2}',raw):
        raise ReportLayoutError('Unknown printed calendar date')
    try:value=datetime.strptime(raw,'%m/%d/%Y %H:%M:%S')
    except ValueError as error:raise ReportLayoutError('Invalid printed calendar date') from error
    return ResultCell(value=value,raw=raw,unit=None,precision='second:1',span=reader.span(start,end))


def analysis_options(block,context,source):
    text_fields={'Flow Units':'flow_units','Rainfall/Runoff':'rainfall_runoff','RDII':'rdii','Snowmelt':'snowmelt',
        'Groundwater':'groundwater','Flow Routing':'flow_routing','Ponding Allowed':'ponding_allowed',
        'Water Quality':'water_quality','Infiltration Method':'infiltration_method','Flow Routing Method':'flow_routing_method',
        'Surcharge Method':'surcharge_method','Variable Time Step':'variable_time_step'}
    numbers={'Antecedent Dry Days':('antecedent_dry_days','d'),'Routing Time Step':('routing_time_step','s'),
        'Maximum Trials':('maximum_trials',None),'Number of Threads':('number_of_threads',None),'Head Tolerance':('head_tolerance',None)}
    clocks={'Report Time Step':'report_time_step','Wet Time Step':'wet_time_step','Dry Time Step':'dry_time_step'}
    dates={'Starting Date':'start','Ending Date':'end'}
    columns=(col('text',None,'printed effective engine option',kind='text'),
        col('number',None,'printed effective option; cell unit is authoritative'),
        col('calendar',None,'effective model calendar date',kind='datetime'),
        col('duration','s','printed clock duration, not a wall clock',kind='elapsed'))
    rows=[];seen=set();process=False
    for number,line in checkpointed(content(block)):
        if line.strip()=='Process Models:':
            if process:raise ReportLayoutError('Repeated process model heading')
            process=True;continue
        match=re.fullmatch(r'\s*(.+?)\s+\.{2,}\s*(\S.*?)(?:\s*)',line)
        if not match:raise ReportLayoutError('Unknown effective option layout')
        label,raw=match[1],match[2];reader=RowReader(number,line,source)
        start,end=match.span(2);cells=[missing(column,'not_applicable_to_option_kind') for column in columns]
        if label in text_fields:
            key=text_fields[label];cells[0]=text_cell(reader,start,end)
        elif label in numbers:
            key,unit=numbers[label]
            value=re.fullmatch(r'([+-]?\d+(?:\.\d+)?)(?: (sec|ft|m))?',raw)
            if not value:raise ReportLayoutError('Unknown option number/unit')
            if label=='Routing Time Step' and value[2]!='sec':raise ReportLayoutError('Missing routing step unit')
            if label=='Head Tolerance':
                if value[2] not in ('ft','m'):raise ReportLayoutError('Missing head tolerance unit')
                unit=value[2]
                if context.flow_units is not None and (unit=='m')!=(context.flow_units in ('CMS','LPS','MLD')):
                    raise ReportLayoutError('Head tolerance unit differs from Flow Units')
            elif label!='Routing Time Step' and value[2]:raise ReportLayoutError('Extra option unit')
            if label in ('Maximum Trials','Number of Threads') and not value[1].isdigit():
                raise ReportLayoutError('Expected an integer option count')
            cells[1]=reader.number_cell(value[1],unit,start,start+len(value[1]))
        elif label in dates:
            key=dates[label];cells[2]=calendar(reader,start,end)
        elif label in clocks:
            key=clocks[label];match_time=re.fullmatch(r'(\d{2,}):([0-5]\d):([0-5]\d)',raw)
            if not match_time:raise ReportLayoutError('Invalid printed option duration')
            h,m,s=map(int,match_time.groups())
            try:value=timedelta(hours=h,minutes=m,seconds=s)
            except OverflowError as error:raise ReportLayoutError('Option duration out of range') from error
            cells[3]=ResultCell(value=value,raw=raw,unit='s',precision='second:1',span=reader.span(start,end))
        else:raise ReportLayoutError('Unknown effective option '+label)
        if key in seen:raise ReportLayoutError('Repeated effective option')
        seen.add(key);rows.append(ResultRow(key='swmm:'+key,label=label,target=None,cells=tuple(cells)))
    required={'flow_units','rainfall_runoff','rdii','snowmelt','groundwater','flow_routing','water_quality','start','end','antecedent_dry_days','report_time_step'}
    if not process or not required<=seen:raise ReportLayoutError('Incomplete effective options')
    return columns,tuple(rows)


def element_count(block,context,source):
    labels={'rain gages':'raingages','subcatchments':'subcatchments','nodes':'nodes','links':'links','pollutants':'pollutants','land uses':'landuses'}
    columns=(col('count',None,'number of engine input objects'),);rows=[]
    for number,line in checkpointed(content(block)):
        m=re.fullmatch(r'\s*Number of (.+?)\s+\.{2,}\s*(\d+)\s*',line)
        if not m or m[1] not in labels:raise ReportLayoutError('Unknown input object count')
        reader=RowReader(number,line,source)
        rows.append(ResultRow(key='swmm:'+labels[m[1]],label=m[1],target=None,
            cells=(reader.number_cell(m[2],None,m.start(2),m.end(2),'decimal:0'),)))
    if len(rows)!=len(labels):raise ReportLayoutError('Incomplete input object counts')
    return columns,tuple(rows)


def rdii(block,context,source):
    lines=_splitlines(block.text)
    header=' '.join(normalize(line) for line in lines[:3])
    m=re.fullmatch(r'\*+ Volume Volume Rainfall Dependent I/I (acre-feet|hectare-m) (10\^6 (?:gal|ltr)) \*+ -+ -+',header)
    if not m or (m[1]=='acre-feet')!=(m[2]=='10^6 gal'):raise ReportLayoutError('Unknown RDII balance units')
    columns=(col('volume','acre-ft' if m[1]=='acre-feet' else 'ha-m','whole-run RDII quantity; ratio row has no unit'),
             col('scaled_volume',m[2],'whole-run RDII quantity; ratio is not printed in this column'))
    labels={'Sewershed Rainfall':'sewershed_rainfall','RDII Produced':'rdii_produced','RDII Ratio':'rdii_ratio'};rows=[]
    for number,line in checkpointed(content(block)):
        m=re.fullmatch(r'\s*(.+?)\s+\.{2,}\s*(.*)',line)
        if not m or m[1] not in labels:raise ReportLayoutError('Unknown RDII statistic')
        reader=RowReader(number,line,source);reader.tokens=list(re.compile(r'\S+').finditer(line,m.start(2)))
        if m[1]=='RDII Ratio':
            cells=numeric_tail(reader,(col('ratio',None,'RDII/rainfall; native zero if rainfall zero'),),(3,))+(missing(columns[1],'ratio_only_in_first_column'),)
        else:cells=numeric_tail(reader,columns,(3,3))
        reader.end();rows.append(ResultRow(key='swmm:'+labels[m[1]],label=m[1],target=None,cells=cells))
    if len(rows)!=3:raise ReportLayoutError('Incomplete RDII statistics')
    return columns,tuple(rows)


def rainfall_file(block,context,source):
    from ._report_input import header_rows
    lines=header_rows(block,'Station First Last Recording Periods Periods Periods ID Date Date Frequency w/Precip Missing Malfunc.')
    columns=(col('station_id',None,'external station label, not a rain gage identity',kind='text'),
        col('first_date',None,'first source date at midnight, day precision',kind='datetime'),
        col('last_date',None,'last source date at midnight, day precision',kind='datetime'),
        col('recording_interval','min','printed native recording interval'),col('rain_periods',None,'native w/Precip count; accepted zero records may also count'),
        col('missing_periods',None,'periods missing'),col('malfunction_periods',None,'periods flagged as malfunction'))
    rows=[]
    for number,line in checkpointed(lines):
        reader=RowReader(number,line,source);station=reader.word();cells=[station]
        for column in columns[1:3]:
            token=reader.take();raw=token[0]
            if re.fullmatch(r'\*+',raw):
                cells.append(ResultCell(value=None,raw=raw,unit=None,precision='day:1',span=reader.span(token.start(),token.end()),missing_reason='native_date_unavailable'))
            else:
                if not re.fullmatch(r'\d{2}/\d{2}/\d{4}',raw):raise ReportLayoutError('Unknown rainfall file date')
                try:value=datetime.strptime(raw,'%m/%d/%Y')
                except ValueError as error:raise ReportLayoutError('Invalid rainfall file date') from error
                cells.append(ResultCell(value=value,raw=raw,unit=None,precision='day:1',span=reader.span(token.start(),token.end())))
        def integer(unit):
            token=reader.take()
            if not token[0].isdigit():raise ReportLayoutError('Expected a rainfall interval/count integer')
            return reader.number_cell(token[0],unit,token.start(),token.end(),'decimal:0')
        cells.append(integer('min'))
        if reader.take()[0]!='min':raise ReportLayoutError('Unknown rain recording interval unit')
        cells.extend(integer(None) for _ in columns[4:]);reader.end()
        rows.append(ResultRow(key=('swmm:station_occurrence',station.value,str(len(rows)+1)),label=station.value,target=None,cells=tuple(cells)))
    return columns,tuple(rows)


def control_actions(block,context,source):
    columns=(col('time',None,'actual native action calendar timestamp rounded to a second',kind='datetime'),
        col('setting',None,'printed native setting (two decimals), not necessarily binary status'),
        col('rule_id',None,'native control rule label',kind='text'))
    rows=[]
    for number,line in checkpointed(content(block)):
        m=re.fullmatch(r'\s*(\d{2}/\d{2}/\d{4}): (\d{2}:\d{2}:\d{2}) Link (\S+) setting changed to\s+([+-]?\d+\.\d{2}) by Control (\S+)\s*',line)
        if not m:raise ReportLayoutError('Unrecognized control action')
        reader=RowReader(number,line,source)
        try:when=datetime.strptime(m[1]+' '+m[2],'%m/%d/%Y %H:%M:%S')
        except ValueError as error:raise ReportLayoutError('Invalid control action date') from error
        raw=line[m.start(1):m.end(2)]
        cells=(ResultCell(value=when,raw=raw,unit=None,precision='second:1',span=reader.span(m.start(1),m.end(2))),
            reader.number_cell(m[4],None,m.start(4),m.end(4),'decimal:2'),text_cell(reader,m.start(5),m.end(5)))
        rows.append(ResultRow(key=('swmm:action',str(len(rows))),label=m[3],target=Ref(collection='swmm:links',key=m[3]),cells=cells))
    return columns,tuple(rows)


def timing(block,context,source):
    columns=(col('wall_clock',None,'native machine local clock, unknown timezone; unrelated to model calendar',kind='datetime'),
        col('wall_seconds','s','native processing wall time; less-than qualifier is authoritative'))
    rows=[];months={name:i+1 for i,name in enumerate(('Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'))}
    for i,line in enumerate(checkpointed(_splitlines(block.text))):
        if not line.strip():continue
        m=re.fullmatch(r'\s*(Analysis begun on|Analysis ended on|Total elapsed time):\s*(.*?)\s*',line)
        if not m:raise ReportLayoutError('Unknown wall timing record')
        reader=RowReader(block.start_line+i,line,source);raw=m[2]
        cells=[missing(column,'not_applicable_to_wall_metric') for column in columns]
        if m[1]=='Total elapsed time':
            if raw=='< 1 sec':value=1.;qualifier='less_than'
            else:
                duration=re.fullmatch(r'(?:(\d+)\.)?(\d{2}):([0-5]\d):([0-5]\d)',raw)
                if not duration:raise ReportLayoutError('Unknown wall duration')
                days,hours,minutes,seconds=(int(v or 0) for v in duration.groups())
                value=days*86400+hours*3600+minutes*60+seconds;qualifier='equal'
            cells[1]=ResultCell(value=value,raw=raw,unit='s',precision='second:1',span=reader.span(m.start(2),m.end(2)),qualifier=qualifier)
            key='elapsed'
        else:
            clock=re.fullmatch(r'(Mon|Tue|Wed|Thu|Fri|Sat|Sun) ([A-Z][a-z]{2})\s+(\d{1,2}) (\d{2}):([0-5]\d):([0-5]\d) (\d{4})',raw)
            if not clock or clock[2] not in months:raise ReportLayoutError('Unknown native ctime calendar')
            try:value=datetime(int(clock[7]),months[clock[2]],int(clock[3]),int(clock[4]),int(clock[5]),int(clock[6]))
            except ValueError as error:raise ReportLayoutError('Invalid wall clock') from error
            cells[0]=ResultCell(value=value,raw=raw,unit=None,precision='second:1',span=reader.span(m.start(2),m.end(2)))
            key='started' if m[1]=='Analysis begun on' else 'ended'
        rows.append(ResultRow(key='swmm:'+key,label=m[1],target=None,cells=tuple(cells)))
    if len(rows)!=3:raise ReportLayoutError('Incomplete native wall timing')
    return columns,tuple(rows)


def builtins():
    from ._report_input import builtins as inputs
    for key,title,parser in (('analysis_options','Analysis Options',analysis_options),('element_count','Element Count',element_count),
        ('rdii','Rainfall Dependent I/I',rdii),('rainfall_file','Rainfall File Summary',rainfall_file),
        ('control_actions','Control Actions Taken',control_actions)):
        yield ReportTableCodec(key='swmm:'+key,title=title,parser=parser)
    yield ReportTableCodec(key='swmm:analysis_timing',title='Analysis timing',parser=timing,block_kind='timing')
    for key,title,parser in inputs():yield ReportTableCodec(key=key,title=title,parser=parser)
