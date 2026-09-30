"""Fixed-column rainfall codecs matched to the SWMM 5.2.4 reader."""

from datetime import date
import re

from ..validation import Diagnostic, Severity, SourceSpan
from ..validation._cooperative import checkpointed
from ._data_text import decode_lines, error
from .historical_rainfall import ArchiveRainfallRecord as R, ArchiveRainfallObservation as O, CANADIAN, ONLINE
from .inp.geometry import finite_number, number_text

ELEMENTS=('HPCP','QPCP','QGAG')


def _int(text):
    if not re.fullmatch(r'[+-]?\d+',text.strip()):raise ValueError('Expected a fixed-column signed integer')
    return int(text)


def detect(rows):
    for index,(_,row) in enumerate(rows[:5]):
        if re.match(r'^\d{6} +\d{2} +(HPCP|QPCP|QGAG)',row):return 'NWS_SPACE_DELIMITED',index,False
        if len(row)>49 and re.match(r'^ *\d{2} +(HPCP|QPCP|QGAG) +\S{2} +\d{4}',row[37:]):return 'NWS_SPACE_DELIMITED',index,True
        if re.match(r'^\d{6},\d{2},(HPCP|QPCP|QGAG)',row):return 'NWS_COMMA_DELIMITED',index,False
        if re.match(r'^\S{3}\d{6}\d{2}(HPCP|QPCP|QGAG)',row):return 'NWS_TAPE',index,False
        if re.match(r'^COOP:\d{6}',row):
            header=rows[0][1]
            element='HPCP' if 'HPCP' in header else 'QPCP' if 'QPCP' in header else None
            if element is None:raise ValueError('NWS online rainfall requires HPCP or QPCP in the first physical header')
            return ('NWS_ONLINE_60' if element=='HPCP' else 'NWS_ONLINE_15'),index,False
        if re.match(r'^\d{7}\d{3}\d{2}\d{2}123',row) and len(row)>=185:return 'AES_HLY',index,False
        if re.match(r'^\d{7}\d{4}\d{2}\d{2}123',row) and len(row)>=186:return 'CMC_HLY',index,False
        if re.match(r'^\d{7}\d{4}\d{2}\d{2}159',row) and len(row)>=691:return 'CMC_FIF',index,False
    return None,None,None


def parse(data,*,encoding,source):
    rows=list(decode_lines(data,encoding=encoding,source=source))
    if any(not row.isascii() for _,row in rows):
        raise ValueError('Historical rainfall headers and data require ASCII byte columns')
    # C fgets retains a newline in strlen. Fixed daily records need 185/186/691
    # total bytes for native format detection, including their line terminator.
    detected_rows=[(n,row+'\n' if n<len(rows) else row) for n,row in rows]
    kind,index,named=detect(detected_rows)
    if kind is None:raise ValueError('No recognized historical rainfall record within the first five physical lines')
    records=[];issues=[];date_offset=value_offset=None
    if kind in ONLINE:
        element='HPCP' if kind=='NWS_ONLINE_60' else 'QPCP'
        value_offset=rows[0][1].index(element)
        date_offset=rows[index][1].rfind(':')-11
        if date_offset<11 or value_offset<date_offset+14:
            raise ValueError('NWS online date/time or value column offsets are invalid')
    def warning(code,message,n,row):issues.append(Diagnostic(code=code,message=message,severity=Severity.WARNING,
        span=SourceSpan(source=source,line=n,column=1,end_column=len(row)+1)))
    for number,row in checkpointed(rows[index:]):
        if not row:continue
        try:
            if not row.isascii():raise ValueError('Historical rainfall uses ASCII byte columns')
            if kind in CANADIAN:
                start=17 if kind=='AES_HLY' else 18
                if len(row)<start:raise ValueError('Truncated Canadian daily header')
                station=row[:7];year_width=3 if kind=='AES_HLY' else 4
                year=_int(row[7:7+year_width])
                if kind=='AES_HLY':year+=2000 if year<100 else 1000
                month=_int(row[7+year_width:9+year_width]);day=_int(row[9+year_width:11+year_width])
                element=row[11+year_width:14+year_width]
                count=96 if kind=='CMC_FIF' else 24;step=15 if count==96 else 60
                if len(row)<start+7*count:raise ValueError('Canadian daily record is missing one or more required slots')
                observations=[]
                for i in range(count):
                    cell=row[start+7*i:start+7*(i+1)]
                    if cell[0].isspace() and cell[6].isdigit():
                        raise ValueError('Native Canadian scanf would consume a numeric quality flag into a space-padded value; use an unambiguous fixed-width value')
                    hour,minute=divmod(i*step,60)
                    observations.append(O(hour=hour,minute=minute,value=_int(cell[:6]),quality=cell[6]))
                records.append(R(station=station,date=date(year,month,day),element=element,observations=tuple(observations)))
                if row[start+7*count:].strip():warning('rainfall.archive_trailing_text','Native ignores daily trailing text; normalization omits it',number,row)
            elif kind in ONLINE:
                if not re.match(r'^COOP:\d{6}',row):
                    if re.match(r'\d{8}',row[date_offset:date_offset+8]):
                        raise ValueError('Native online reader can consume this dated row, but its station metadata is not in COOP format')
                    warning('rainfall.archive_skipped_line','Non-data online line is omitted during canonical writing',number,row)
                    continue
                if len(row)<=date_offset+23:raise ValueError('Online record is too short for native date/value detection')
                stamp=row[date_offset:date_offset+8]
                if not re.fullmatch(r'\d{8}',stamp):raise ValueError('Online date requires YYYYMMDD at the header column')
                when=date(int(stamp[:4]),int(stamp[4:6]),int(stamp[6:8]))
                clock=re.match(r' +(\d{2}):(\d{2})',row[date_offset+8:])
                if not clock:raise ValueError('Online time requires HH:MM')
                suffix=row[value_offset:];parts=suffix.split()
                if not parts:raise ValueError('Online record has no rainfall value')
                decimal='.' in suffix
                value=finite_number(parts[0]) if decimal else _int(parts[0])
                flag=parts[1][0] if len(parts)>1 else ' '
                if len(parts)>2 or len(parts)>1 and len(parts[1])>1:
                    warning('rainfall.archive_trailing_text','Native online reader ignores trailing text beyond its first condition character',number,row)
                observations=(O(hour=int(clock[1]),minute=int(clock[2]),value=value,condition=flag,decimal_inches=decimal),)
                records.append(R(station=row[5:11],date=when,element=element,observations=observations))
            else:
                if kind=='NWS_TAPE':
                    if len(row)<30:raise ValueError('Truncated NWS tape header')
                    station,division,element,units,record_type=row[3:9],row[9:11],row[11:15],row[15:17],row[:3]
                    when=date(_int(row[17:21]),_int(row[21:23]),_int(row[23:27]))
                    start,width=30,12;station_name=None
                    advertised=_int(row[27:30])
                else:
                    shift=31 if named else 0
                    station=row[:6];station_name=row[7:37].rstrip() if named else None
                    division,element,units=row[7+shift:9+shift],row[10+shift:14+shift],row[15+shift:17+shift]
                    pos=18+shift
                    when=date(_int(row[pos:pos+4]),_int(row[pos+5:pos+7]),_int(row[pos+8:pos+10]))
                    start,width=28+shift,16;record_type='HPD';advertised=None
                tail=row[start:]
                if len(tail)<width or len(tail)%width:
                    # Native codes may be omitted on the final interval. Its
                    # required clock/value prefix must still be present.
                    remainder=len(tail)%width
                    minimum=10 if kind=='NWS_TAPE' else 12
                    if remainder>=minimum:
                        length=len(tail)
                        tail=tail.ljust(length+width-remainder)
                        if kind=='NWS_COMMA_DELIMITED':
                            chars=list(tail)
                            for separator_position in (12,14):
                                if separator_position>=remainder:chars[length-remainder+separator_position]=','
                            tail=''.join(chars)
                    else:raise ValueError('Incomplete NWS rainfall clock/value group')
                observations=[]
                for i in range(0,len(tail),width):
                    cell=tail[i:i+width]
                    if kind=='NWS_TAPE':
                        if cell[4].isspace() and cell[10].isdigit():
                            raise ValueError('Native tape scanf would consume a numeric condition into a space-padded value')
                        if not cell[:4].isdigit():raise ValueError('Rainfall clock fields require zero-padded digits')
                        hour,minute,value,flag,quality=_int(cell[:2]),_int(cell[2:4]),_int(cell[4:10]),cell[10],cell[11]
                    else:
                        separator=',' if kind=='NWS_COMMA_DELIMITED' else ' '
                        if any(cell[j]!=separator for j in (0,5,12,14)):
                            raise ValueError('NWS group separator is outside its fixed native column')
                        if not cell[1:5].isdigit():raise ValueError('Rainfall clock fields require zero-padded digits')
                        hour,minute,value,flag,quality=_int(cell[1:3]),_int(cell[3:5]),_int(cell[6:12]),cell[13],cell[15]
                    observations.append(O(hour=hour,minute=minute,value=value,condition=flag,quality=quality))
                if advertised is not None and advertised!=len(observations):
                    warning('rainfall.archive_ignored_count','Native tape reader ignores the header interval count; canonical writing corrects it',number,row)
                records.append(R(station=station,date=when,element=element,division=division,units_code=units,record_type=record_type,station_name=station_name,observations=tuple(observations)))
        except (ValueError,OverflowError,TypeError) as exc:
            error(str(exc),source=source,line=number,text=row,code='rainfall.invalid_archive')
    return kind,tuple(records),issues


def render(document):
    kind=document.format
    if kind in ONLINE:
        yield 'STATION'.ljust(17)+'DATE'.ljust(19)+('HPCP' if kind=='NWS_ONLINE_60' else 'QPCP')
    for r in document.records:
        when=r.date
        if kind in CANADIAN:
            year=f'{when.year%1000:03d}' if kind=='AES_HLY' else f'{when.year:04d}'
            yield f'{r.station}{year}{when.month:02d}{when.day:02d}{r.element}'+''.join(f'{int(v.value):06d}{v.quality}' for v in r.observations)
        elif kind in ONLINE:
            v=r.observations[0]
            value=number_text(v.value) if v.decimal_inches else str(int(v.value))
            if v.decimal_inches and '.' not in value:
                if 'e' in value.lower():raise ValueError('Online decimal inches must contain a decimal point')
                value+='.0'
            stamp=f'{when.year:04d}{when.month:02d}{when.day:02d} {v.hour:02d}:{v.minute:02d}'
            yield ('COOP:'+r.station).ljust(17)+stamp.ljust(19)+f'{value} {v.condition}'.ljust(12)
        elif kind=='NWS_TAPE':
            header=f'{r.record_type}{r.station}{r.division}{r.element}{r.units_code}{when.year:04d}{when.month:02d}{when.day:04d}{len(r.observations):03d}'
            yield header+''.join(f'{v.hour:02d}{v.minute:02d}{int(v.value):06d}{v.condition}{v.quality}' for v in r.observations)
        else:
            delimiter=',' if kind=='NWS_COMMA_DELIMITED' else ' '
            name='' if r.station_name is None else r.station_name.ljust(30)+' '
            header=r.station+delimiter+name+delimiter.join((r.division,r.element,r.units_code,f'{when.year:04d}',f'{when.month:02d}',f'{when.day:02d}'))
            yield header+''.join(delimiter+f'{v.hour:02d}{v.minute:02d}'+delimiter+f'{int(v.value):6d}'+delimiter+v.condition+delimiter+v.quality for v in r.observations)
