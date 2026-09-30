"""Physical codecs for the four SWMM climate file formats."""

from datetime import date
import re

from ..validation import Diagnostic, Severity, SourceSpan
from ..validation._cooperative import checkpointed
from ._data_text import decode_lines, error, tokens
from .climate_data import ClimateObservation as O, ClimateRecord as R, PARAMETERS
from .inp.geometry import finite_number, number_text


def _number(text):
    return finite_number(text)


def _integer(text):
    if not text.strip().isascii() or not text.strip().isdecimal():
        raise ValueError('Expected an unsigned integer in a fixed-width climate field')
    return int(text)


def _value(text):
    return None if text.startswith('*') else _number(text)


def parse(data, *, encoding, source):
    rows=list(decode_lines(data, encoding=encoding, source=source))
    if not rows or not rows[0][1]:
        raise ValueError('Climate format must be identified on the first physical line')
    first=rows[0][1]
    parts=tokens(first)
    if first.startswith('DLY') and first[23:27] == '9999': format='TD3200'
    elif len(first) >= 233 and first[13:16].strip() in ('001','002','151','1','2'): format='DLY0204'
    elif len(parts) >= 5 and all(p.isascii() and p.isdecimal() for p in parts[1:4]): format='USER_PREPARED'
    elif 'DATE' in first and any(p in first for p in PARAMETERS): format='GHCND'
    else: raise ValueError('Unrecognized climate file format')
    records, issues=[],[]
    def warn(code, message, number, row):
        issues.append(Diagnostic(code=code, message=message, severity=Severity.WARNING,
            span=SourceSpan(source=source,line=number,column=1,end_column=len(row)+1)))
    columns=[]
    if format == 'GHCND':
        matches=list(re.finditer(r'\S+', first))
        columns=[(m.group(),m.start(),matches[i+1].start() if i+1<len(matches) else None) for i,m in enumerate(matches)]
        names=[name for name,_,_ in columns]
        if len(set(names)) != len(names) or 'DATE' not in names:
            raise ValueError('GHCND requires distinct column labels including DATE')
        for parameter in PARAMETERS:
            if parameter in first and not any(name == parameter and pos == first.index(parameter) for name,pos,_ in columns):
                raise ValueError('GHCND climate label has an ambiguous native substring position')
    for number, row in checkpointed(rows):
        if not row: continue
        try:
            if format != 'USER_PREPARED' and not row.isascii():
                raise ValueError('Fixed-width climate records require ASCII byte columns')
            if format == 'USER_PREPARED':
                parts=tokens(row)
                if len(parts)<4:
                    raise ValueError('User climate row requires station/year/month/day')
                when=date(*(_integer(p) for p in parts[1:4]))
                values=tuple(_value(v) for v in parts[4:8])+(None,)*max(0,8-len(parts))
                observations=tuple(O(parameter=p,value=v) for p,v in zip(('TMAX','TMIN','EVAP','AWND'),values))
                records.append(R(station=parts[0],date=when,observations=observations))
                if len(parts)>8:
                    warn('climate.ignored_columns','Native USER climate ignores columns after wind; normalization omits them',number,row)
            elif format == 'TD3200':
                if len(row)<30 or row[:3]!='DLY' or row[23:27]!='9999':
                    raise ValueError('Invalid TD3200 monthly header')
                when=date(_integer(row[17:21]),_integer(row[21:23]),1)
                count=_integer(row[27:30]); parameter=row[11:15]
                if not 0<=count<=31 or len(row)<30+count*12:
                    raise ValueError('TD3200 monthly count/length mismatch')
                observations=[]
                for index in range(count):
                    cell=row[30+index*12:42+index*12]
                    observations.append(O(parameter=parameter,day=_integer(cell[:2]),time_code=cell[2:4],sign=cell[4],
                        value=None if cell[5:10]=='99999' else _integer(cell[5:10]),measurement_flag=cell[10],quality_flag=cell[11]))
                records.append(R(station=row[3:11],date=when,parameter=parameter,units_code=row[15:17],observations=tuple(observations)))
                if row[30+count*12:].strip():
                    warn('climate.ignored_columns','Native TD3200 ignores trailing text; normalization omits it',number,row)
            elif format == 'DLY0204':
                if len(row)<233:
                    raise ValueError('DLY0204 requires a complete 233-character monthly row')
                when=date(_integer(row[7:11]),_integer(row[11:13]),1)
                code=_integer(row[13:16])
                parameter={1:'TMAX',2:'TMIN',151:'EVAP'}.get(code,f'{code:03d}')
                observations=[]
                for day in range(1,32):
                    cell=row[16+(day-1)*7:23+(day-1)*7]
                    observations.append(O(parameter=parameter,day=day,sign=cell[0],
                        value=None if cell[1:6] in ('99999','     ') else _integer(cell[1:6]),quality_flag=cell[6]))
                records.append(R(station=row[:7],date=when,parameter=parameter,observations=tuple(observations)))
                if row[233:].strip():
                    warn('climate.ignored_columns','Native DLY0204 ignores trailing text; normalization omits it',number,row)
            else:
                if number==1: continue
                if not row.strip(' -\t'): continue
                cells={name:row[pos:end].rstrip() for name,pos,end in columns}
                when_text=cells['DATE'].strip()
                if len(when_text)!=8 or not when_text.isdecimal():
                    raise ValueError('GHCND date requires YYYYMMDD')
                when=date(int(when_text[:4]),int(when_text[4:6]),int(when_text[6:]))
                observations=[]; extras=[]
                for name,pos,end in columns:
                    text=cells[name].lstrip(' \t')
                    if name in PARAMETERS:
                        # sscanf skips leading whitespace before applying its
                        # eight-character width. Do not allow an empty column
                        # to consume the following column as its value.
                        match=re.match(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?',text[:8])
                        if not match: raise ValueError('GHCND numeric column must contain a value or -9999 sentinel')
                        raw=match.group(); value=_number(raw)
                        suffix=text[len(raw):].rstrip()
                        if suffix and not suffix[0].isspace() and suffix[0] in '.0123456789eE+-':
                            raise ValueError('GHCND numeric value exceeds the native eight-character scan width')
                        observations.append(O(parameter=name,value=None if abs(value)>=9999 else value,quality_flag=suffix.strip()))
                    elif name not in ('DATE','STATION'): extras.append((name,cells[name]))
                observations.sort(key=lambda v:PARAMETERS.index(v.parameter))
                records.append(R(station=cells.get('STATION',''),date=when,observations=tuple(observations),extra_fields=tuple(extras)))
        except (ValueError,TypeError,OverflowError) as exc:
            error(str(exc),source=source,line=number,text=row,code='climate.invalid_data')
    if not records: raise ValueError('Climate file has no records')
    return format,tuple(records),issues


def _ghcnd_number(value):
    if value is None: return '-9999'
    variants=[number_text(value),*(format(value,f'.{precision}g') for precision in range(1,18))]
    for candidate in variants:
        if len(candidate)<=8 and float(candidate)==value: return candidate
    raise ValueError('Value cannot be written exactly within the GHCND eight-character numeric width')


def render(document):
    kind=document.format
    if kind=='GHCND':
        parameters=tuple(p for p in PARAMETERS if any(v.parameter==p for r in document.records for v in r.observations))
        extras=tuple(dict.fromkeys(name for r in document.records for name,_ in r.extra_fields))
        names=('STATION','DATE',*parameters,*extras)
        cells=[]
        for r in document.records:
            values={v.parameter:_ghcnd_number(v.value).ljust(8)+v.quality_flag for v in r.observations}
            cells.append(dict(STATION=r.station,DATE=f'{r.date.year:04d}{r.date.month:02d}{r.date.day:02d}',**values,**dict(r.extra_fields)))
        widths={name:max(len(name),8 if name in PARAMETERS else 0,*(len(c.get(name,'-9999' if name in PARAMETERS else '')) for c in cells))+2 for name in names}
        yield ''.join(name.ljust(widths[name]) for name in names).rstrip()
        yield ''.join(('-'*len(name)).ljust(widths[name]) for name in names).rstrip()
        for cells_row in cells:
            yield ''.join(cells_row.get(name,'-9999' if name in PARAMETERS else '').ljust(widths[name]) for name in names).rstrip()
        return
    for record in document.records:
        when=record.date
        if kind=='USER_PREPARED':
            values={v.parameter:v.value for v in record.observations}
            yield ' '.join((record.station,str(when.year),str(when.month),str(when.day),*(
                '*' if values.get(p) is None else number_text(values[p]) for p in ('TMAX','TMIN','EVAP','AWND'))))
        elif kind=='TD3200':
            parameter=record.parameter or record.observations[0].parameter
            header=f'DLY{record.station}{parameter}{record.units_code}{when.year:04d}{when.month:02d}9999{len(record.observations):03d}'
            yield header+''.join(f'{v.day:02d}{v.time_code}{v.sign}{99999 if v.value is None else int(v.value):05d}{v.measurement_flag}{v.quality_flag}' for v in record.observations)
        else:
            name=record.parameter or record.observations[0].parameter
            parameter={'TMAX':1,'TMIN':2,'EVAP':151}.get(name)
            if parameter is None: parameter=int(name)
            header=f'{record.station}{when.year:04d}{when.month:02d}{parameter:03d}'
            yield header+''.join(f'{v.sign}{99999 if v.value is None else int(v.value):05d}{v.quality_flag}' for v in record.observations)
