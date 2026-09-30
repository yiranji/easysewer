"""Additional native summaries, with explicit aggregate and deployment identities."""

from collections import defaultdict
import re

from ..model.identity import Ref, canonical_key
from ..results.tables import ResultCell, ResultRow
from .report import ReportLayoutError
from ..validation._cooperative import checkpointed
from .report_document import _splitlines
from ._report_tables import RowReader, col, tabular, numeric_tail, _FLOW, _NUMBER


def normalize(text):return ' '.join(text.split())


def pollutant_names(text, count, context, source, *, width=14, grouped=False):
    """Resolve touching native minimum-width labels only with asserted identities."""
    if context.pollutants is not None:
        encoding='utf-8' if source.encoding in (None,'utf-8-sig') else source.encoding
        def rendered(names):
            return normalize(''.join(' '*max(0,width-len(name.encode(encoding)))+name for name in names))
        candidates=([context.pollutants[i:i+5] for i in range(0,len(context.pollutants),5)]
                    if grouped else [context.pollutants])
        matches=[names for names in candidates if len(names)==count and rendered(names)==normalize(text)]
        if len(matches)!=1:raise ReportLayoutError('Pollutant header differs from asserted producer identities')
        return tuple(matches[0])
    names=text.split()
    if len(names)!=count:raise ReportLayoutError('Touching pollutant labels require an explicit ordered ReportContext.pollutants')
    if len({canonical_key(name) for name in names})!=len(names):
        raise ReportLayoutError('Duplicate pollutant column identity')
    return tuple(names)


def missing(column, reason='not_printed'):
    return ResultCell(value=None,raw='',unit=column.unit,precision=None,span=None,missing_reason=reason)


def rows_for(lines,columns,collection,source,precisions):
    rows=[]
    for number,text in checkpointed(lines):
        reader=RowReader(number,text,source);name=reader.word().value
        cells=numeric_tail(reader,columns,precisions)
        reader.end()
        rows.append(ResultRow(key=name,label=name,target=Ref(collection=collection,key=name),cells=cells))
    return tuple(rows)


def scalar_summaries(block,context,source):
    title=block.title
    if title=='Subcatchment Runoff Summary':
        pattern=r'Total Total Total Total Imperv Perv Total Total Peak Runoff Precip Runon Evap Infil Runoff Runoff Runoff Runoff Runoff Coeff Subcatchment (in|mm) \1 \1 \1 \1 \1 \1 (10\^6 (?:gal|ltr)) '+_FLOW
        header,lines=tabular(block,pattern)
        names=('precipitation','runon','evaporation','infiltration','impervious_runoff','pervious_runoff','runoff_depth','runoff_volume','peak_runoff','runoff_coefficient')
        units=(header[1],)*7+(header[2],header[3],None)
        columns=tuple(col(key,unit,'native whole-run subcatchment '+key) for key,unit in zip(names,units))
        collection='swmm:subcatchments'
    elif title=='Groundwater Summary':
        pattern=r'Total Total Maximum Average Average Final Final Total Total Lower Lateral Lateral Upper Water Upper Water Infil Evap Seepage Outflow Outflow Moist\. Table Moist\. Table Subcatchment (in|mm) \1 \1 \1 '+_FLOW+r' (ft|m) \3'
        header,lines=tabular(block,pattern)
        names=('infiltration','evaporation','deep_seepage','lateral_outflow','maximum_lateral_flow','mean_upper_moisture','mean_water_table','final_upper_moisture','final_water_table')
        units=(header[1],)*4+(header[2],None,header[3],None,header[3])
        columns=tuple(col(key,unit,'native whole-run groundwater '+key+'; means weighted by runoff time') for key,unit in zip(names,units))
        collection='swmm:subcatchments'
    elif title=='Flow Classification Summary':
        header,lines=tabular(block,r'Adjusted ---------- Fraction of Time in Flow Class ---------- /Actual Up Down Sub Sup Up Down Norm Inlet Conduit Length Dry Dry Dry Crit Crit Crit Crit Ltd Ctrl')
        names=('adjusted_length_ratio','dry_fraction','upstream_dry_fraction','downstream_dry_fraction','subcritical_fraction','supercritical_fraction','upstream_critical_fraction','downstream_critical_fraction','normal_limited_fraction','inlet_control_fraction')
        columns=tuple(col(key,None,'modified / original conduit length' if i==0 else 'native duration / RoutingTimeSpan') for i,key in enumerate(names))
        collection='swmm:links'
    elif title=='Conduit Surcharge Summary':
        header,lines=tabular(block,r'Hours Hours --------- Hours Full -------- Above Full Capacity Conduit Both Ends Upstream Dnstream Normal Flow Limited',empty='No conduits were surcharged.')
        if header is None:return (),()
        names=('both_ends_full','upstream_full','downstream_full','above_full_normal_flow','capacity_limited')
        columns=tuple(col(key,'h','native duration; every emitted cell clipped to at least 0.01 h, including a zero duration') for key in names)
        collection='swmm:links'
    elif title=='Pumping Summary':
        pattern=r'Min Avg Max Total Power % Time Off Percent Number of Flow Flow Flow Volume Usage Pump Curve Pump Utilized Start-Ups '+_FLOW+r' \1 \1 (10\^6 (?:gal|ltr)) Kw-hr Low High'
        header,lines=tabular(block,pattern)
        names=('utilized_percent','startups','minimum_flow','mean_flow','maximum_flow','pumped_volume','energy','off_curve_low_percent','off_curve_high_percent')
        units=('%',None,header[1],header[1],header[1],header[2],'kW-h','%','%')
        columns=tuple(col(key,unit,'native pump '+key+'; mean flow is arithmetic over active pump periods') for key,unit in zip(names,units))
        collection='swmm:links'
    else:raise ReportLayoutError('Unrecognized summary title')
    precisions=((2,)*9+(3,) if title=='Subcatchment Runoff Summary' else
                (2,0,2,2,2,3,2,1,1) if title=='Pumping Summary' else (2,)*len(columns))
    return columns,rows_for(lines,columns,collection,source,precisions)


def quality_summaries(block,context,source):
    lines=_splitlines(block.text);title=block.title
    borders=[i for i,line in enumerate(checkpointed(lines)) if re.fullmatch(r'\s*-{3,}\s*',line)]
    aggregate=title in ('Subcatchment Washoff Summary','Outfall Loading Summary')
    if len(borders)!=(3 if aggregate else 2):raise ReportLayoutError('Unrecognized load table borders')
    first,end=borders[:2];headers=lines[first+1:end]
    if title=='Outfall Loading Summary':
        if len(headers)!=3:raise ReportLayoutError('Unrecognized outfall header height')
        h1=normalize(headers[0]);h2=normalize(headers[1]);h3=normalize(headers[2])
        prefix='Freq Flow Flow Volume'
        if not h2.startswith(prefix):raise ReportLayoutError('Unrecognized outfall columns')
        match=re.fullmatch(r'Outfall Node Pcnt '+_FLOW+r' \1 (10\^6 (?:gal|ltr))(.*)',h3)
        if not match:raise ReportLayoutError('Unrecognized outfall units')
        units=match[3].split();names=pollutant_names(h2[len(prefix):],len(units),context,source)
        if h1!='Flow Avg Max Total'+(' Total'*len(units)):raise ReportLayoutError('Unknown outfall load header')
        columns=(col('flow_frequency','%','flowing routing periods / reporting periods; system is mean across outfalls'),
            col('mean_flow',match[1],'arithmetic mean over flowing periods; system is sum of per-outfall means'),
            col('maximum_flow',match[1],'native outfall flow peak; system is simultaneous total peak'),
            col('total_volume',match[2],'native whole-run NodeInflow volume'))
        collection='swmm:nodes'
    else:
        if len(headers)!=2:raise ReportLayoutError('Unrecognized load header height')
        label='Subcatchment' if title=='Subcatchment Washoff Summary' else 'Link'
        unit_header=normalize(headers[1])
        if not unit_header.startswith(label+' '):raise ReportLayoutError('Unrecognized load units')
        units=unit_header[len(label):].split();names=pollutant_names(headers[0],len(units),context,source)
        columns=();collection='swmm:subcatchments' if label=='Subcatchment' else 'swmm:links'
    if any(unit not in ('lbs','kg','LogN') for unit in units):raise ReportLayoutError('Unknown load unit')
    columns+=tuple(col('load',unit,'native log10 count; zero also represents nonpositive count' if unit=='LogN' else 'native integrated pollutant mass',
        pollutant=Ref(collection='swmm:pollutants',key=name)) for name,unit in zip(names,units))
    rows=[]
    for i,line in enumerate(checkpointed(lines)):
        if i<=end or not line.strip() or i in borders:continue
        total=aggregate and i>borders[2]
        reader=RowReader(block.start_line+i,line,source);name=reader.word().value
        if total and name!='System':raise ReportLayoutError('Unrecognized aggregate row')
        if title=='Outfall Loading Summary':
            precisions=(2,3 if columns[1].unit in ('MGD','CMS') else 2,3 if columns[2].unit in ('MGD','CMS') else 2)+(3,)*(len(columns)-3)
        else:precisions=(3,)*len(columns)
        cells=numeric_tail(reader,columns,precisions);reader.end()
        rows.append(ResultRow(key=('swmm:system',) if total else name,label=name,
            target=None if total else Ref(collection=collection,key=name),cells=cells))
    if aggregate and sum(row.key==('swmm:system',) for row in checkpointed(rows))!=1:raise ReportLayoutError('Missing or duplicate system aggregate')
    return columns,tuple(rows)


def lid_performance(block,context,source):
    header,lines=tabular(block,r'Total Evap Infil Surface Drain Initial Final Continuity Inflow Loss Loss Outflow Outflow Storage Storage Error Subcatchment LID Control (in|mm) \1 \1 \1 \1 \1 \1 %')
    columns=(col('subcatchment_id',None,'native subcatchment identity',kind='text'),col('lid_control_id',None,'native LID definition identity',kind='text'))
    names=('inflow','evaporation','infiltration','surface_outflow','drain_outflow','initial_storage','final_storage','continuity_error')
    columns+=tuple(col(key,'%' if i==7 else header[1],'whole-run LID depth balance over one unit footprint; repeated deployments remain distinct') for i,key in enumerate(names))
    seen=defaultdict(int);rows=[]
    for number,text in checkpointed(lines):
        reader=RowReader(number,text,source);sub=reader.word();lid=reader.word()
        seen[(sub.value,lid.value)]+=1;ordinal=seen[(sub.value,lid.value)]
        cells=(sub,lid)+numeric_tail(reader,columns[2:],(2,)*8);reader.end()
        # RPT does not contain easysewer's record_id. Never invent that binding.
        rows.append(ResultRow(key=('swmm:lid_occurrence',sub.value,lid.value,str(ordinal)),
            label=sub.value+'/'+lid.value+f' #{ordinal}',target=None,cells=cells))
    return columns,tuple(rows)


def streets(block,context,source):
    pattern=r'Peak Avg\. Bypass Back Peak Peak Peak Maximum Maximum Flow Flow Flow Flow Capture Bypass Flow Spread Depth Inlet Inlet Inlet Capture Capture Freq Freq / Inlet Flow Street Conduit '+_FLOW+r' (ft|m) \2 Design Location(?: Count)? Pcnt Pcnt Pcnt Pcnt \1 \1'
    header,lines=tabular(block,pattern)
    columns=(col('peak_flow',header[1],'native street maximum flow'),col('maximum_spread',header[2],'width at maximum depth, per street side and capped at street width'),
        col('maximum_depth',header[2],'native street depth peak'),col('inlet_id',None,'inlet design identity',kind='text'),
        col('inlet_location',None,'native evaluated ON-GRADE/ON-SAG placement',kind='text'),col('inlet_count',None,'inlet count'))
    columns+=tuple(col(key,unit,'native street inlet '+key) for key,unit in (
        ('peak_capture_percent','%'),('mean_capture_percent','%'),('bypass_frequency','%'),('backflow_frequency','%'),
        ('peak_capture_per_inlet',header[1]),('peak_bypass_flow',header[1])))
    rows=[]
    for number,text in checkpointed(lines):
        reader=RowReader(number,text,source);name=reader.word().value
        if len(reader.tokens)-reader.i not in (3,6,12):raise ReportLayoutError('Unknown street optional field count')
        cells=[]
        for column in columns:
            if reader.i==len(reader.tokens):cells.append(missing(column,'inlet_or_flow_statistics_not_printed'))
            else:cells.append(reader.word() if column.kind=='text' else reader.numeric(column.unit))
        reader.end();rows.append(ResultRow(key=name,label=name,target=Ref(collection='swmm:links',key=name),cells=tuple(cells)))
    return columns,tuple(rows)


def routing_steps(block,context,source):
    metrics={
        'Minimum Time Step':('minimum_time_step','s'),'Average Time Step':('mean_time_step','s'),
        'Maximum Time Step':('maximum_time_step','s'),'% of Time in Steady State':('steady_state_percent','%'),
        'Average Iterations per Step':('mean_iterations',None),'% of Steps Not Converging':('nonconverging_percent','%')}
    columns=(col('value',None,'printed routing metric; effective cell unit is authoritative'),
             col('lower_bound','s','frequency bin lower bound'),col('upper_bound','s','frequency bin upper bound'))
    rows=[];seen=set();frequency=False
    for i,line in enumerate(checkpointed(_splitlines(block.text)[3:]),3):
        if not line.strip():continue
        label,sep,tail=line.partition(':');label=label.strip()
        if not sep:raise ReportLayoutError('Missing routing metric separator')
        if label=='Time Step Frequencies' and not tail.strip() and not frequency:
            frequency=True;continue
        reader=RowReader(block.start_line+i,line,source)
        if label in metrics and not frequency:
            key,unit=metrics[label];seen.add(label)
            reader.i=next(j for j,m in enumerate(reader.tokens) if m.start()>line.index(':'))
            value=reader.numeric(unit)
            if unit=='s' and reader.take()[0]!='sec':raise ReportLayoutError('Unknown routing time unit')
            reader.end();cells=(value,missing(columns[1],'not_a_frequency_bin'),missing(columns[2],'not_a_frequency_bin'))
            key='swmm:'+key
        else:
            if not frequency:raise ReportLayoutError('Unknown routing metric')
            lower=reader.numeric('s')
            if reader.take()[0]!='-':raise ReportLayoutError('Unknown frequency range')
            upper=reader.numeric('s')
            if reader.take()[0]!='sec' or reader.take()[0]!=':':raise ReportLayoutError('Unknown frequency units')
            value=reader.numeric('%')
            if reader.take()[0]!='%':raise ReportLayoutError('Unknown frequency unit')
            reader.end();cells=(value,lower,upper);key=('swmm:frequency',lower.raw,upper.raw)
        rows.append(ResultRow(key=key,label=label,target=None,cells=cells))
    if seen!=set(metrics):raise ReportLayoutError('Incomplete routing metrics')
    return columns,tuple(rows)


def rankings(block,context,source):
    absent=('All links are stable.','Convergence obtained at all time steps.','None')
    lines=[(block.start_line+i,line) for i,line in enumerate(checkpointed(_splitlines(block.text))) if i>=3 and line.strip()]
    unit=None if block.title=='Highest Flow Instability Indexes' else '%'
    columns=(col('value',unit,'native ranked diagnostic statistic'),)
    if len(lines)==1 and lines[0][1].strip() in absent:return columns,()
    rows=[]
    for number,line in checkpointed(lines):
        match=re.fullmatch(r'\s*(Node|Link) (\S+) \(('+_NUMBER+r')(%?)\)\s*',line)
        if not match or bool(match[4])!=(unit=='%'):raise ReportLayoutError('Unrecognized diagnostic ranking')
        collection='swmm:nodes' if match[1]=='Node' else 'swmm:links'
        reader=RowReader(number,line,source)
        value=reader.number_cell(match[3],unit,match.start(3),match.end(3))
        rows.append(ResultRow(key=(collection,match[2]),label=match[2],target=Ref(collection=collection,key=match[2]),cells=(value,)))
    return columns,tuple(rows)


def builtins():
    for key,title in (('subcatchment_runoff','Subcatchment Runoff Summary'),('groundwater','Groundwater Summary'),
        ('flow_classification','Flow Classification Summary'),('conduit_surcharge','Conduit Surcharge Summary'),('pumping','Pumping Summary')):
        yield 'swmm:'+key,title,scalar_summaries
    for key,title in (('subcatchment_washoff','Subcatchment Washoff Summary'),('outfall_loading','Outfall Loading Summary'),('link_pollutant_load','Link Pollutant Load Summary')):
        yield 'swmm:'+key,title,quality_summaries
    yield 'swmm:lid_performance','LID Performance Summary',lid_performance
    yield 'swmm:street_flow','Street Flow Summary',streets
    yield 'swmm:routing_time_step','Routing Time Step Summary',routing_steps
    for key,title in (('highest_continuity_errors','Highest Continuity Errors'),('time_step_critical_elements','Time-Step Critical Elements'),
        ('highest_flow_instability','Highest Flow Instability Indexes'),('nonconverging_nodes','Most Frequent Nonconverging Nodes')):
        yield 'swmm:'+key,title,rankings
