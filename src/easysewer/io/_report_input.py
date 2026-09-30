"""Engine-resolved input summaries; never used to reconstruct the source Model."""

from collections import defaultdict
import re

from ..model.identity import Ref, canonical_key
from ..results import ResultCell, ResultRow
from .report import ReportLayoutError
from ..validation._cooperative import checkpointed
from .report_document import _splitlines
from ._report_tables import RowReader, col
from ._report_summaries import missing, normalize


def header_rows(block,expected):
    lines=_splitlines(block.text);borders=[i for i,line in enumerate(checkpointed(lines)) if re.fullmatch(r'\s*-{3,}\s*',line)]
    if len(borders)!=1:raise ReportLayoutError('Expected one input/status header border')
    end=borders[0];header=normalize(' '.join(lines[3:end]))
    if header!=expected:raise ReportLayoutError('Unknown input/status columns: '+header)
    return tuple((block.start_line+i,line) for i,line in enumerate(checkpointed(lines)) if i>end and line.strip())


def units(context):
    if context.flow_units is None:return None,None,None,None
    si=context.flow_units in ('CMS','LPS','MLD')
    return ('m','m2','ha',context.flow_units) if si else ('ft','ft2','acre',context.flow_units)


def text_at(reader,start,end):
    raw=reader.text[start:end]
    return ResultCell(value=raw,raw=raw,unit=None,precision='text',span=reader.span(start,end))


def numbers(reader,start,columns,digits):
    values=[]
    for column,places in zip(columns,digits):
        match=re.compile(r'\s*([+-]?\d+'+(r'\.\d{'+str(places)+r'}' if places else '')+r')').match(reader.text,start)
        if not match:raise ReportLayoutError('Invalid printed input-summary number')
        values.append(reader.number_cell(match[1],column.unit,match.start(1),match.end(1),'decimal:'+str(places)))
        start=match.end()
    return tuple(values),start


def end(reader,position):
    if reader.text[position:].strip():raise ReportLayoutError('Unexpected extra input-summary field')


def input_summaries(block,context,source):
    length,area,landarea,flow=units(context);title=block.title
    if title=='Landuse Summary':
        header='Sweeping Maximum Last Name Interval Removal Swept';collection='swmm:landuses'
        definitions=(('sweeping_interval','d'),('sweeping_removal',None),('initial_days_since_sweeping','d'));digits=(2,2,2)
    elif title=='Subcatchment Summary':
        header='Name Area Width %Imperv %Slope Rain Gage Outlet';collection='swmm:subcatchments'
        definitions=(('area',landarea),('width',length),('impervious_percent','%'),('slope_percent','%'));digits=(2,2,2,4)
    elif title=='Cross Section Summary':
        header='Full Full Hyd. Max. No. of Full Conduit Shape Depth Area Rad. Width Barrels Flow';collection='swmm:links'
        definitions=(('full_depth',length),('full_area',area),('full_hydraulic_radius',length),('maximum_width',length),('barrels',None),('full_flow',flow));digits=(2,2,2,2,0,2)
    elif title=='Node Summary':
        header='Invert Max. Ponded External Name Type Elev. Depth Area Inflow';collection='swmm:nodes'
        definitions=(('elevation',length),('full_depth',length),('ponded_area',area));digits=(2,2,1)
    elif title=='Link Summary':
        header='Name From Node To Node Type Length %Slope Roughness';collection='swmm:links'
        definitions=(('length',length),('slope_percent','%'),('roughness',None));digits=(1,4,4)
    else:raise ReportLayoutError('Unknown input table')
    columns=tuple(col(key,unit,'engine-resolved input '+key+'; dimensional units require Flow Units context') for key,unit in definitions)
    if title=='Subcatchment Summary':
        columns+=(col('rain_gage',None,'printed rain gage ID',kind='text'),col('outlet',None,'printed outlet ID; node/subcatchment category not given by the table',kind='text'))
    elif title=='Cross Section Summary':columns=(col('shape',None,'native shape keyword or named geometry; category not printed',kind='text'),)+columns
    elif title=='Node Summary':columns=(col('type',None,'native node type',kind='text'),)+columns+(col('external_inflow',None,'native Yes flag for external, dry-weather or RDII inflows; blank is not flagged',kind='text'),)
    elif title=='Link Summary':columns=tuple(col(key,None,'native '+key+'; end nodes are in original input orientation',kind='text') for key in ('inlet','outlet','type'))+columns
    rows=[]
    for number,line in checkpointed(header_rows(block,header)):
        reader=RowReader(number,line,source);name=reader.word().value;position=reader.tokens[reader.i-1].end();prefix=()
        if title=='Cross Section Summary':
            prefix=(reader.word(),);position=reader.tokens[reader.i-1].end()
        elif title=='Node Summary':
            prefix=(reader.word(),);position=reader.tokens[reader.i-1].end()
            if prefix[0].value not in ('JUNCTION','OUTFALL','STORAGE','DIVIDER'):raise ReportLayoutError('Unknown input node type')
        elif title=='Link Summary':
            inlet=reader.word();outlet=reader.word();kind=reader.word();position=reader.tokens[reader.i-1].end()
            if kind.value.startswith('TYPE') or kind.value=='IDEAL':
                token=reader.take()
                if token[0]!='PUMP':raise ReportLayoutError('Unknown pump input type')
                kind=text_at(reader,kind.span.column-1,token.end());position=token.end()
            if kind.value not in ('CONDUIT','ORIFICE','WEIR','OUTLET') and not kind.value.endswith('PUMP'):raise ReportLayoutError('Unknown input link type')
            prefix=(inlet,outlet,kind)
            if kind.value!='CONDUIT':
                end(reader,position);cells=prefix+tuple(missing(c,'not_applicable_to_link_type') for c in columns[3:])
                rows.append(ResultRow(key=name,label=name,target=Ref(collection=collection,key=name),cells=cells));continue
        numeric_columns=columns[len(prefix):len(prefix)+len(digits)]
        numeric,position=numbers(reader,position,numeric_columns,digits);cells=prefix+numeric
        if title=='Subcatchment Summary':
            tokens=list(re.compile(r'\S+').finditer(line,position))
            if len(tokens)!=2:raise ReportLayoutError('Unknown subcatchment outlet fields')
            cells+=tuple(text_at(reader,t.start(),t.end()) for t in tokens);position=tokens[-1].end()
        elif title=='Node Summary':
            flag=re.fullmatch(r'\s*(Yes)?\s*',line[position:])
            if not flag:raise ReportLayoutError('Unknown external inflow flag')
            cells+=(text_at(reader,position+flag.start(1),position+flag.end(1)) if flag[1] else missing(columns[-1],'not_flagged'),)
            position=len(line)
        end(reader,position);rows.append(ResultRow(key=name,label=name,target=Ref(collection=collection,key=name),cells=cells))
    return columns,tuple(rows)


def pollutants(block,context,source):
    lines=header_rows(block,'Ppt. GW Kdecay Name Units Concen. Concen. 1/days CoPollutant')
    columns=(col('concentration_unit',None,'printed native concentration unit',kind='text'),
        col('rainfall_concentration',None,'rainfall concentration; cell unit is authoritative'),
        col('groundwater_concentration',None,'groundwater concentration; cell unit is authoritative'),
        col('decay_rate','1/d','native first-order decay rate'),col('co_pollutant',None,'co-pollutant identity if emitted',kind='text'),
        col('co_fraction',None,'printed co-pollutant fraction'))
    rows=[]
    for number,line in checkpointed(lines):
        m=re.match(r'\s*(\S+)\s+(MG/L|UG/L|#/L)',line)
        if not m:raise ReportLayoutError('Unknown pollutant input identity/unit')
        reader=RowReader(number,line,source);prefix=(text_at(reader,m.start(2),m.end(2)),)
        effective=(col('rainfall_concentration',m[2],'native'),col('groundwater_concentration',m[2],'native'),columns[3])
        values,position=numbers(reader,m.end(),effective,(2,2,2));tail=line[position:]
        if tail.strip():
            co=re.fullmatch(r'\s+(\S+)\s+\(([+-]?\d+\.\d{2})\)\s*',tail)
            if not co:raise ReportLayoutError('Unknown co-pollutant fields')
            extra=(text_at(reader,position+co.start(1),position+co.end(1)),reader.number_cell(co[2],None,position+co.start(2),position+co.end(2),'decimal:2'))
        else:extra=tuple(missing(c,'no_co_pollutant_printed') for c in columns[4:])
        rows.append(ResultRow(key=m[1],label=m[1],target=Ref(collection='swmm:pollutants',key=m[1]),cells=prefix+values+extra))
    return columns,tuple(rows)


def rain_gages(block,context,source):
    lines=header_rows(block,'Data Recording Name Data Source Type Interval')
    columns=(col('source_text',None,'entire printed source description',kind='text'),
        col('data_source',None,'source ID or file path; source kind asserted by captured INP',kind='text'),
        col('rain_type',None,'printed native rainfall type for a time series',kind='text'),
        col('recording_interval','min','printed recording interval for a time series'))
    known=None if context.rain_gage_sources is None else {canonical_key(name):kind for name,kind in context.rain_gage_sources}
    rows=[]
    for number,line in checkpointed(lines):
        reader=RowReader(number,line,source);name=reader.word().value
        position=reader.tokens[reader.i-1].end()
        start=position+len(line[position:])-len(line[position:].lstrip());stop=len(line.rstrip())
        if start==stop:raise ReportLayoutError('Missing printed rain gage source')
        raw=text_at(reader,start,stop)
        if known is None or canonical_key(name) not in known:
            cells=(raw,)+tuple(missing(column,'source_kind_not_proven') for column in columns[1:])
        elif known[canonical_key(name)]=='FILE':
            cells=(raw,raw,missing(columns[2],'not_printed_for_file'),missing(columns[3],'not_printed_for_file'))
        else:
            m=re.fullmatch(r'(\S+)\s+(INTENSITY|VOLUME|CUMULATIVE)\s+(\d+) min\.',line[start:stop])
            if not m:raise ReportLayoutError('Rain gage report differs from asserted time-series kind')
            cells=(raw,text_at(reader,start+m.start(1),start+m.end(1)),text_at(reader,start+m.start(2),start+m.end(2)),
                reader.number_cell(m[3],'min',start+m.start(3),start+m.end(3),'decimal:0'))
        rows.append(ResultRow(key=name,label=name,target=Ref(collection='swmm:raingages',key=name),cells=cells))
    return columns,tuple(rows)


def lid_controls(block,context,source):
    length,area,_,_=units(context)
    lines=header_rows(block,'No. of Unit Unit % Area % Imperv % Perv Subcatchment LID Control Units Area Width Covered Treated Treated')
    columns=(col('subcatchment_id',None,'subcatchment identity',kind='text'),col('lid_control_id',None,'LID definition identity',kind='text'))
    columns+=tuple(col(key,unit,'engine-resolved LID deployment '+key) for key,unit in (
        ('number',None),('unit_area',area),('unit_width',length),('covered_area_percent','%'),('impervious_treated_percent','%'),('pervious_treated_percent','%')))
    seen=defaultdict(int);rows=[]
    for number,line in checkpointed(lines):
        reader=RowReader(number,line,source);sub=reader.word();lid=reader.word();position=reader.tokens[reader.i-1].end()
        values,position=numbers(reader,position,columns[2:],(0,2,2,2,2,2));end(reader,position)
        identity=canonical_key(sub.value),canonical_key(lid.value);seen[identity]+=1
        rows.append(ResultRow(key=('swmm:lid_occurrence',sub.value,lid.value,str(seen[identity])),label=sub.value+'/'+lid.value,
            target=None,cells=(sub,lid)+values))
    return columns,tuple(rows)


def geometry_arrays(block,context,source):
    kind=block.title.partition(' ')[0];collection={'Shape':'swmm:curves','Transect':'swmm:transects','Street':'swmm:streets'}[kind]
    columns=tuple(col(key,None,'native normalized '+key+' at geometry table index; not original input geometry')
                  for key in ('area','hydraulic_radius','width'))
    current=None;metric=None;arrays={};rows=[];seen=set()
    def finish():
        if current is None:return
        if set(arrays)!=set(('Area','Hrad','Width')) or any(len(values)!=50 for values in arrays.values()):
            raise ReportLayoutError('Incomplete native 51-entry geometry table (zero index is not printed)')
        for i in range(50):
            rows.append(ResultRow(key=(current,str(i+1)),label=current+' #'+str(i+1),target=Ref(collection=collection,key=current),
                cells=tuple(arrays[key][i] for key in ('Area','Hrad','Width'))))
    for i,line in enumerate(checkpointed(_splitlines(block.text)[3:]),3):
        if not line.strip():continue
        heading=re.fullmatch(r'\s*'+kind+r' (\S+)\s*',line)
        if heading:
            finish();current=heading[1];identity=canonical_key(current)
            if identity in seen:raise ReportLayoutError('Repeated native geometry identity')
            seen.add(identity);arrays={};metric=None;continue
        if current is None:raise ReportLayoutError('Missing geometry identity')
        label=re.match(r'\s*(Area|Hrad|Width):\s*',line);position=0
        if label:
            metric=label[1];position=label.end()
            if metric in arrays:raise ReportLayoutError('Repeated geometry metric')
            arrays[metric]=[]
        if metric is None:raise ReportLayoutError('Missing geometry metric')
        reader=RowReader(block.start_line+i,line,source)
        while line[position:].strip():
            cells,position=numbers(reader,position,(columns[0],),(4,));arrays[metric].extend(cells)
    finish()
    return columns,tuple(rows)


def builtins():
    for key,title in (('input_landuses','Landuse Summary'),('input_subcatchments','Subcatchment Summary'),
        ('input_nodes','Node Summary'),('input_links','Link Summary'),('input_cross_sections','Cross Section Summary')):
        yield 'swmm:'+key,title,input_summaries
    yield 'swmm:input_pollutants','Pollutant Summary',pollutants
    yield 'swmm:input_raingages','Raingage Summary',rain_gages
    yield 'swmm:input_lid_controls','LID Control Summary',lid_controls
    for key,title in (('input_shapes','Shape Summary'),('input_transects','Transect Summary'),('input_streets','Street Summary')):
        yield 'swmm:'+key,title,geometry_arrays
