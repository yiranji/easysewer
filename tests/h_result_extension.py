"""Independent H extension: differences of consecutive printed node depths.

This test plugin uses only public result/registry types. It deliberately accepts
one explicit node-detail grammar; unfamiliar columns fail without guessing.
"""
from datetime import datetime
from dataclasses import replace
from decimal import Decimal
import re
from easysewer.io.report import ReportLayoutError,ReportTableCodec,swmm_report_tables
from easysewer.results import ResultCell,ResultColumn,ResultRow
from easysewer.validation import SourceSpan

KEY='acceptance:node_depth_changes'
METRIC='acceptance:depth_change'
TARGET='J'
MEANING=('current minus previous printed depth; difference of rounded saved observations, '
         'not an instantaneous rate or an internal-step extreme')

def parse_depth_changes(block,context,source):
    lines=block.text.splitlines()
    borders=[i for i,line in enumerate(lines) if re.fullmatch(r'\s*-{3,}\s*',line)]
    if len(borders)!=2 or borders[1]-borders[0]!=3:raise ReportLayoutError('Unsupported extension detail header')
    first,second=(' '.join(line.split()) for line in lines[borders[0]+1:borders[1]])
    if first!='Inflow Flooding Depth Head':raise ReportLayoutError('Unknown extension columns')
    match=re.fullmatch(r'Date Time (CFS|GPM|MGD|CMS|LPS|MLD) \1 (feet|meters) \2',second)
    if not match:raise ReportLayoutError('Unknown extension units')
    unit={'feet':'ft','meters':'m'}[match[2]]
    assessment=context.result_context.output('swmm:nodes','swmm:depth',target=block.target)
    columns=(ResultColumn(key='swmm:time',unit=None,kind='datetime',semantics='printed model-calendar saved time'),
        ResultColumn(key='acceptance:printed_depth',unit=unit,kind='number',semantics='current printed saved depth'),
        ResultColumn(key='acceptance:previous_depth',unit=unit,kind='number',semantics='previous printed saved depth'),
        ResultColumn(key=METRIC,unit=unit,kind='number',semantics=MEANING+'; '+context.semantics))
    columns=(columns[0],)+tuple(replace(column,applicability=assessment) for column in columns[1:])
    rows=[];previous=None;previous_time=None
    number=re.compile(r'\s*([+-]?\d+\.\d{3})')
    for offset,line in enumerate(lines):
        if offset<=borders[1] or not line.strip():continue
        clock=re.match(r'\s*(\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2}:\d{2})',line)
        if clock is None:raise ReportLayoutError('Invalid extension timestamp')
        try:when=datetime.strptime(' '.join(clock[1].split()),'%m/%d/%Y %H:%M:%S')
        except ValueError as error:raise ReportLayoutError('Invalid extension calendar') from error
        if previous_time is not None and when<previous_time:raise ReportLayoutError('Extension times move backwards')
        position=clock.end();values=[]
        for _ in range(4):
            item=number.match(line,position)
            if item is None:raise ReportLayoutError('Invalid extension observation')
            values.append(item);position=item.end()
        if line[position:].strip():raise ReportLayoutError('Unexpected extension field')
        span=lambda start,end:SourceSpan(line=block.start_line+offset,column=start+1,end_column=end+1,source=source.path)
        timestamp=ResultCell(value=when,raw=clock[1],unit=None,precision='second:1',span=span(clock.start(1),clock.end(1)))
        item=values[2]
        current=ResultCell(value=float(item[1]),raw=item[1],unit=unit,precision='decimal:3',span=span(item.start(1),item.end(1)))
        missing='acceptance:no_previous_observation'
        prior=previous or ResultCell(value=None,raw='',unit=unit,precision=None,span=None,missing_reason=missing)
        delta=ResultCell(value=float(Decimal(current.raw)-Decimal(previous.raw)) if previous else None,
            raw='',unit=unit,precision='difference-of-decimal:3',span=None,missing_reason=None if previous else missing)
        cells=(current,prior,delta)
        if assessment.unavailable:
            cells=tuple(replace(cell,value=None,missing_reason=assessment.reasons[0]) for cell in cells)
        rows.append(ResultRow(key=('acceptance:observation',str(len(rows))),label=clock[1],target=block.target,
            cells=(timestamp,)+cells))
        previous,previous_time=current,when
    return columns,tuple(rows)

def codec(parser=parse_depth_changes):
    return ReportTableCodec(key=KEY,title='Node '+TARGET,block_kind='detail',parser=parser)

def registry(parser=parse_depth_changes):return swmm_report_tables().with_codecs(codec(parser))

LITERAL='''  <<< Node J >>>
  ----------------------------------------------------------------
  Inflow Flooding Depth Head
  Date Time CFS CFS feet feet
  ----------------------------------------------------------------
  01/31/2020 23:59:00 2.000 0.000 1.250 2.500
  02/01/2020 00:00:00 3.000 0.000 1.500 2.750
  02/01/2020 00:01:00 0.000 0.000 0.750 2.000
'''
