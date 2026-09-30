"""EPA SWMM 5.2.4 printed layouts. Reject unknown headers instead of shifting cells."""

from datetime import timedelta
import math
import re

from ..model.identity import Ref
from ..results.tables import ResultCell, ResultColumn, ResultRow
from ..validation import SourceSpan
from ..validation._cooperative import checkpointed
from .report_document import _splitlines
from .report import ReportLayoutError


_FLOW=r'(CFS|GPM|MGD|CMS|LPS|MLD)'
_NUMBER=r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?'
_ELAPSED='native-peak elapsed from ReportStart, clamped at zero; native rounds to seconds then omits seconds'


def col(key, unit, semantics, kind='number', pollutant=None):
    return ResultColumn(key='swmm:'+key,unit=unit,kind=kind,semantics=semantics,pollutant=pollutant)


def tabular(block, pattern, empty=None):
    lines=_splitlines(block.text)
    if empty and [line.strip() for line in lines[3:] if line.strip()] == [empty]:return None, ()
    borders=[i for i,line in enumerate(checkpointed(lines)) if re.fullmatch(r'\s*-{3,}\s*',line)]
    if len(borders) != 2:raise ReportLayoutError('Expected two table header borders')
    first,last=borders
    header=' '.join(' '.join(line.split()) for line in lines[first+1:last])
    match=re.fullmatch(pattern,header)
    if not match:raise ReportLayoutError('Unrecognized column header: '+header)
    return match, tuple((block.start_line+i,line) for i,line in enumerate(checkpointed(lines)) if i>last and line.strip())


class RowReader:
    def __init__(self, number, text, source):
        self.number=number;self.text=text;self.source=source;self.tokens=list(re.finditer(r'\S+',text));self.i=0

    def take(self):
        if self.i == len(self.tokens):raise ReportLayoutError('Missing printed row field')
        match=self.tokens[self.i];self.i+=1
        return match

    def span(self, start, end):
        return SourceSpan(line=self.number,column=start+1,end_column=end+1,source=self.source.path)

    def word(self):
        match=self.take()
        return ResultCell(value=match[0],raw=match[0],unit=None,precision='text',span=self.span(match.start(),match.end()))

    def numeric(self, unit, precision=None):
        match=self.take()
        return self.number_cell(match[0],unit,match.start(),match.end(),precision)

    def number_cell(self, raw, unit, start, end, precision=None):
        match=re.fullmatch(r'([<>]?)('+_NUMBER+')',raw)
        if not match:raise ReportLayoutError('Invalid/nonfinite printed number: '+raw)
        value=float(match[2])
        if not math.isfinite(value):raise ReportLayoutError('Nonfinite printed number')
        if precision is None:
            mantissa=re.split('[eE]',match[2])[0]
            precision='decimal:'+str(len(mantissa.partition('.')[2]))
            if re.search('[eE]',match[2]):precision='printed-scientific'
        return ResultCell(value=value,raw=raw,unit=unit,precision=precision,span=self.span(start,end),
            qualifier={'':'equal','>':'greater_than','<':'less_than'}[match[1]])

    def elapsed(self):
        days=self.take();clock=self.take()
        if not re.fullmatch(r'\d+',days[0]) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',clock[0]):
            raise ReportLayoutError('Invalid days hr:min value')
        h,m=map(int,clock[0].split(':'))
        try:value=timedelta(days=int(days[0]),hours=h,minutes=m)
        except (OverflowError,ValueError) as error:raise ReportLayoutError('Elapsed time is out of range') from error
        return ResultCell(value=value,raw=self.text[days.start():clock.end()],unit='min',precision='minute:1',
            span=self.span(days.start(),clock.end()))

    def end(self):
        if self.i != len(self.tokens):raise ReportLayoutError('Extra printed row fields')


def numeric_tail(reader, columns, precisions):
    """Read known decimal fields, including expanded native fields that touch."""
    if len(columns)!=len(precisions):raise ValueError('Missing native field precision')
    start=reader.tokens[reader.i].start() if reader.i<len(reader.tokens) else len(reader.text)
    if len(reader.tokens)-reader.i==len(columns):
        return tuple(reader.numeric(column.unit) for column in columns)
    values=[];position=start
    for column,digits in zip(columns,precisions):
        pattern=r'\s*([+-]?\d+'+(r'\.\d{'+str(digits)+r'}' if digits else '')+r')'
        match=re.match(pattern,reader.text[position:])
        if not match:raise ReportLayoutError('Unrecognized expanded numeric field')
        values.append(reader.number_cell(match[1],column.unit,position+match.start(1),position+match.end(1),'decimal:'+str(digits)))
        position+=match.end()
    if reader.text[position:].strip():raise ReportLayoutError('Extra expanded numeric fields')
    reader.i=len(reader.tokens)
    return tuple(values)


def hydraulic(block, context, source):
    key=block.title
    patterns={
        'Node Depth Summary':r'Average Maximum Maximum Time of Max Reported Depth Depth HGL Occurrence Max Depth Node Type (Feet|Meters) \1 \1 days hr:min \1',
        'Node Inflow Summary':r'Maximum Maximum Lateral Total Flow Lateral Total Time of Max Inflow Inflow Balance Inflow Inflow Occurrence Volume Volume Error Node Type '+_FLOW+r' \1 days hr:min (10\^6 (?:gal|ltr)) \2 Percent',
        'Node Flooding Summary':r'Total Maximum Maximum Time of Max Flood Ponded Hours Rate Occurrence Volume (Depth|Volume) Node Flooded '+_FLOW+r' days hr:min (10\^6 (?:gal|ltr)) (Feet|Meters|1000 ft³|1000 m³)',
        'Node Surcharge Summary':r'Max\. Height Min\. Depth Hours Above Crown Below Rim Node Type Surcharged (Feet|Meters) \1',
        'Storage Volume Summary':r'Average Avg Evap Exfil Maximum Max Time of Max Maximum Volume Pcnt Pcnt Pcnt Volume Pcnt Occurrence Outflow Storage Unit (1000 (?:ft|m)³) Full Loss Loss \1 Full days hr:min '+_FLOW,
        'Link Flow Summary':r'Maximum Time of Max Maximum Max/ Max/ \|Flow\| Occurrence \|Veloc\| Full Full Link Type '+_FLOW+r' days hr:min (ft/sec|m/sec) Flow Depth',
    }
    empty={'Node Flooding Summary':'No nodes were flooded.','Node Surcharge Summary':'No nodes were surcharged.'}
    header, lines=tabular(block,patterns[key],empty.get(key))
    if header is None:return (),()
    typecol=col('type',None,'printed native object type',kind='text')
    elapsed=col('peak_time','min',_ELAPSED,kind='elapsed')
    def volume_unit(value):return value.replace('³','3')
    if key == 'Node Depth Summary':
        unit={'Feet':'ft','Meters':'m'}[header[1]]
        columns=(typecol,col('mean_depth',unit,'arithmetic mean of routing-step endpoint depths'),
            col('maximum_depth',unit,'native routing-step depth peak'),col('maximum_hgl',unit,'native maximum depth plus invert elevation'),elapsed,
            col('reported_maximum_depth',unit,'native maxRptDepth; with AVERAGES, endpoint depth at output saves, not maximum saved average'))
    elif key == 'Node Inflow Summary':
        flow,vol=header[1],header[2]
        columns=(typecol,col('maximum_lateral_inflow',flow,'native lateral inflow peak'),col('maximum_total_inflow',flow,'native total inflow peak'),elapsed,
            col('lateral_inflow_volume',vol,'native cumulative lateral inflow'),col('total_inflow_volume',vol,'native cumulative total inflow'),
            col('flow_balance_error','%','native balance percent; cell unit gal/ltr instead when absolute native outflow < 1 ft3'))
    elif key == 'Node Flooding Summary':
        variant,flow,vol,last=header.groups()
        if (variant=='Depth') != (last in ('Feet','Meters')):raise ReportLayoutError('Ponded quantity disagrees with its unit')
        unit={'Feet':'ft','Meters':'m'}.get(last,volume_unit(last))
        flooding=('adjusted system flooding including discrete external removal' if context.producer=='easysewer:flexible-ponding'
                  and context.accounting=='discrete-volume:1' else 'producer-specific native flooding; consult table provenance')
        columns=(col('flooded_hours','h','native flooded duration; positive durations clipped to at least 0.01 h'),
            col('maximum_flooding_flow',flow,flooding+' peak'),elapsed,col('flooding_volume',vol,flooding+' cumulative volume'),
            col('maximum_ponded_depth' if variant=='Depth' else 'maximum_ponded_volume',unit,
                'native maximum depth minus full depth' if variant=='Depth' else 'native maximum ponded volume'))
    elif key == 'Node Surcharge Summary':
        unit={'Feet':'ft','Meters':'m'}[header[1]]
        columns=(typecol,col('surcharged_hours','h','positive native surcharge duration clipped to at least 0.01 h'),
            col('maximum_above_crown',unit,'maximum height above highest conduit crown, clamped at zero'),
            col('minimum_below_rim',unit,'full depth minus maximum depth, clamped at zero'))
    elif key == 'Storage Volume Summary':
        unit=volume_unit(header[1]);flow=header[2]
        columns=(col('mean_volume',unit,'routing-step arithmetic mean stored volume'),col('mean_full_percent','%','mean volume / full volume'),
            col('evaporation_loss_percent','%','evaporation loss / total inflow'),col('exfiltration_loss_percent','%','exfiltration loss / total inflow'),
            col('maximum_volume',unit,'native stored volume peak'),col('maximum_full_percent','%','maximum volume / full volume'),elapsed,
            col('maximum_outflow',flow,'native outflow peak'))
    else:
        columns=(typecol,col('maximum_flow',header[1],'maximum absolute native link flow'),elapsed,
            col('maximum_velocity',header[2],'maximum absolute conduit velocity; >50 is a lower bound'),
            col('maximum_full_flow',None,'maximum / full flow, including barrel count for conduits'),
            col('maximum_full_depth',None,'maximum / full depth'))
    rows=[]
    for number,text in checkpointed(lines):
        reader=RowReader(number,text,source);identifier=reader.word().value;cells=[]
        for i,column in enumerate(columns):
            if key=='Link Flow Summary' and i==3:
                # Native prints optional columns in three fixed slots AFTER the
                # timestamp. This remains correct for IDs wider than 20 bytes.
                start=reader.tokens[reader.i-1].end();tail=text[start:]
                if len(tail)>26:raise ReportLayoutError('Link value exceeds its printed slot width')
                for width,optional in zip((10,8,8),columns[3:]):
                    field=tail[:width];tail=tail[width:]
                    if not field.strip():
                        cells.append(ResultCell(value=None,raw=field,unit=optional.unit,precision=None,
                            span=reader.span(start,start+len(field)),missing_reason='not_printed_for_this_link'))
                    else:
                        match=re.fullmatch(r'\s{2,}(\S+)\s*',field)
                        if not match:raise ReportLayoutError('Ambiguous optional link column')
                        cells.append(reader.number_cell(match[1],optional.unit,start+match.start(1),start+match.end(1)))
                    start+=len(field)
                reader.i=len(reader.tokens)
                break
            if column.kind=='text':cells.append(reader.word())
            elif column.kind=='elapsed':cells.append(reader.elapsed())
            else:
                precision='significant:3' if key=='Node Inflow Summary' and i in (4,5) else None
                if key=='Node Inflow Summary' and i==6 and len(reader.tokens)-reader.i==2:
                    raw=reader.numeric(None);unit=reader.take()[0]
                    if unit not in ('gal','ltr'):raise ReportLayoutError('Unrecognized absolute flow balance unit')
                    from dataclasses import replace
                    cells.append(replace(raw,unit=unit))
                else:cells.append(reader.numeric(column.unit,precision))
        reader.end()
        rows.append(ResultRow(key=identifier,label=identifier,target=Ref(collection='swmm:links' if key=='Link Flow Summary' else 'swmm:nodes',key=identifier),cells=tuple(cells)))
    return columns,tuple(rows)


_METRICS={label:'swmm:'+key for label,key in (
    ('Initial LID Storage','initial_lid_storage'),('LID Drainage','lid_drainage'),('Outfall Runon','outfall_runon'),
    ('Total Precipitation','total_precipitation'),('Initial Snow Cover','initial_snow_cover'),('Final Snow Cover','final_snow_cover'),
    ('Snow Removed','snow_removed'),('Initial Storage','initial_storage'),('Final Storage','final_storage'),
    ('Infiltration','infiltration'),('Upper Zone ET','upper_zone_et'),('Lower Zone ET','lower_zone_et'),
    ('Deep Percolation','deep_percolation'),('Groundwater Flow','groundwater_flow'),
    ('Evaporation Loss','evaporation_loss'),('Infiltration Loss','infiltration_loss'),('Surface Runoff','surface_runoff'),
    ('Continuity Error (%)','continuity_error'),('Dry Weather Inflow','dry_weather_inflow'),('Wet Weather Inflow','wet_weather_inflow'),
    ('Groundwater Inflow','groundwater_inflow'),('RDII Inflow','rdii_inflow'),('External Inflow','external_inflow'),
    ('External Outflow','external_outflow'),('Flooding Loss','flooding_loss'),('Exfiltration Loss','exfiltration_loss'),
    ('Initial Stored Volume','initial_stored_volume'),('Final Stored Volume','final_stored_volume'),
    ('Initial Buildup','initial_buildup'),('Surface Buildup','surface_buildup'),('Wet Deposition','wet_deposition'),
    ('Sweeping Removal','sweeping_removal'),('BMP Removal','bmp_removal'),('Remaining Buildup','remaining_buildup'),
    ('Mass Reacted','mass_reacted'),('Initial Stored Mass','initial_stored_mass'),('Final Stored Mass','final_stored_mass'))}


def continuity(block, context, source):
    lines=_splitlines(block.text);title=block.title
    if title=='Runoff Quantity Continuity' and len(lines[0].strip())==26:
        data=[(i,line) for i,line in enumerate(checkpointed(lines[3:]),3) if line.strip()]
        if len(data)==1:
            i,line=data[0];match=re.fullmatch(r'\s*Runoff supplied by interface file (.+)',line)
            if match:
                reader=RowReader(block.start_line+i,line,source)
                column=col('runoff_interface',None,'native input file notice; no continuity quantities were emitted',kind='text')
                cell=ResultCell(value=match[1],raw=match[1],unit=None,precision='text',span=reader.span(match.start(1),match.end(1)))
                return (column,),(ResultRow(key='swmm:source',label='Runoff interface',target=None,cells=(cell,)),)
    quality=title in ('Runoff Quality Continuity','Quality Routing Continuity')
    labels=re.sub(r'^\s*\*+\s*','',lines[0]).split()
    unit_text=lines[1].strip().removeprefix(title).strip()
    if quality:
        units=unit_text.split()
        from ._report_summaries import pollutant_names
        prefix=re.match(r'^\s*\*{26}',lines[0])
        if prefix is None:raise ReportLayoutError('Unrecognized quality balance heading')
        labels=pollutant_names(lines[0][prefix.end():],len(units),context,source,grouped=True)
        if not labels or len(labels)!=len(units) or any(u not in ('lbs','kg','LogN') for u in units):
            raise ReportLayoutError('Unknown quality balance columns')
        columns=tuple(col('load',unit,'log10 native count (zero also represents nonpositive count)' if unit=='LogN' else 'native pollutant mass',
            pollutant=Ref(collection='swmm:pollutants',key=label)) for label,unit in zip(labels,units))
    else:
        if title in ('Runoff Quantity Continuity','Groundwater Continuity'):
            options={'acre-feet inches':('acre-ft','in'),'hectare-m mm':('ha-m','mm')}
            wanted=['Volume','Depth'];keys=('volume','depth')
        else:
            options={'acre-feet 10^6 gal':('acre-ft','10^6 gal'),'hectare-m 10^6 ltr':('ha-m','10^6 ltr')}
            wanted=['Volume','Volume'];keys=('volume','scaled_volume')
        normalized=' '.join(unit_text.split())
        if labels!=wanted or normalized not in options:raise ReportLayoutError('Unknown quantity balance columns')
        columns=tuple(col(key,unit,'native balance quantity; continuity_error cells use percent') for key,unit in zip(keys,options[normalized]))
    rows=[]
    for i,text in enumerate(checkpointed(lines[3:]),3):
        if not text.strip():continue
        match=re.fullmatch(r'\s*(.+?)\s+\.{2,}\s*(.*)',text)
        if not match or match[1] not in _METRICS:raise ReportLayoutError('Unknown continuity row; preserving whole block')
        reader=RowReader(block.start_line+i,text,source)
        reader.tokens=list(re.compile(r'\S+').finditer(text,match.start(2)))
        is_error=match[1]=='Continuity Error (%)'
        from dataclasses import replace
        printed=tuple(replace(column,unit='%') if is_error else column for column in (columns[:1] if is_error and not quality else columns))
        cells=list(numeric_tail(reader,printed,(3,)*len(printed)))
        if is_error and not quality:
            cells.extend(ResultCell(value=None,raw='',unit='%',precision=None,span=None,missing_reason='error_only_printed_in_first_column') for _ in columns[1:])
        reader.end()
        rows.append(ResultRow(key=_METRICS[match[1]],label=match[1],target=None,cells=tuple(cells)))
    return columns,tuple(rows)


def builtins():
    for key,title in (('node_depth','Node Depth Summary'),('node_inflow','Node Inflow Summary'),
                      ('node_flooding','Node Flooding Summary'),('node_surcharge','Node Surcharge Summary'),
                      ('storage_volume','Storage Volume Summary'),('link_flow','Link Flow Summary')):
        yield 'swmm:'+key,title,hydraulic
    for key,title in (('runoff_quantity_continuity','Runoff Quantity Continuity'),('flow_routing_continuity','Flow Routing Continuity'),
                      ('groundwater_continuity','Groundwater Continuity'),
                      ('runoff_quality_continuity','Runoff Quality Continuity'),('quality_routing_continuity','Quality Routing Continuity')):
        yield 'swmm:'+key,title,continuity
