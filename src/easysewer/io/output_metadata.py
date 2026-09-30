"""Bounded, pure-Python SWMM 5.2.4 OUT identity and completion metadata."""

from dataclasses import dataclass
import codecs
from datetime import datetime, timedelta
import math
from pathlib import Path
import struct

from ..model.identity import Ref, canonical_key
from ..results.applicability import ResultContext
from ..validation._cooperative import checkpointed
from ..validation._cooperative import checkpoint_scope

FLOW_UNITS = ('CFS', 'GPM', 'MGD', 'CMS', 'LPS', 'MLD')
MAGIC = 516114522


class NotRecordedError(KeyError):
    pass


@dataclass(frozen=True, kw_only=True)
class OutputMetadata:
    engine_version: int
    flow_units: str
    groups: tuple[tuple[str, tuple[str, ...]], ...]
    pollutant_units: tuple[str, ...]
    variable_codes: tuple[tuple[str, tuple[int, ...]], ...]
    periods: int
    report_step: timedelta
    saved_start: datetime
    first_time: datetime | None
    last_time: datetime | None
    output_offset: int
    period_bytes: int
    semantics: tuple[tuple[str, str], ...] = ()
    averages: bool | None = None
    producer: str | None = None
    input_properties: tuple = ()
    identifier_encoding: str = 'utf-8'
    result_context: ResultContext = ResultContext()

    def names(self, collection):
        return dict(self.groups)[collection]

    def index(self, target: Ref):
        key = canonical_key(target.key)
        for index, name in checkpointed(enumerate(self.names(target.collection))):
            if canonical_key(name) == key:
                return index
        raise NotRecordedError(f'{target.collection}/{target.key} is not recorded in this OUT')

    @classmethod
    def read(cls, path, *, encoding='utf-8', max_metadata_bytes=64*1024*1024, checkpoint=None):
        with checkpoint_scope(checkpoint):
            with Path(path).open('rb') as stream:
                return cls.from_stream(stream, encoding=encoding, max_metadata_bytes=max_metadata_bytes)

    @classmethod
    def from_stream(cls, stream, *, encoding='utf-8', max_metadata_bytes=64*1024*1024, checkpoint=None):
        """Read a caller-owned seekable stream; never reopen its path or close it."""
        with checkpoint_scope(checkpoint):
            if type(max_metadata_bytes) is not int or max_metadata_bytes<64:
                raise ValueError('Invalid output metadata byte limit')
            encoding=codecs.lookup(encoding).name
            stream.seek(0, 2);size=stream.tell()
            if size<52:
                raise ValueError('OUT is truncated or has no completion footer')
            def exact(count):
                value=stream.read(count)
                if len(value)!=count:
                    raise ValueError('Truncated OUT data')
                return value
            stream.seek(size-24)
            id_pos,input_pos,output_pos,periods,error,magic=struct.unpack('<6i',exact(24))
            if magic!=MAGIC or error or periods<0 or not 28==id_pos<=input_pos<output_pos<=size-24:
                raise ValueError('Invalid, failed or unfinished OUT footer')
            if output_pos>max_metadata_bytes:
                raise ValueError('OUT metadata exceeds the explicit byte budget')
            stream.seek(0)
            def read(count):
                if count<0 or stream.tell()+count>output_pos:
                    raise ValueError('OUT metadata crosses the result boundary')
                return exact(count)
            def integer():return struct.unpack('<i',read(4))[0]
            header_magic,version,units,ns,nn,nl,np=struct.unpack('<7i',read(28))
            if header_magic!=MAGIC or version!=52004 or units not in range(6) or min(ns,nn,nl,np)<0:
                raise ValueError('Unsupported OUT header/profile')
            counts=(ns,nn,nl,np)
            if sum(counts)*5>input_pos-id_pos:
                raise ValueError('OUT object counts exceed its name region')
            groups=[]
            for namespace,count in checkpointed(zip(('subcatchments','nodes','links','pollutants'),counts)):
                names=[]
                for _ in checkpointed(range(count)):
                    length=integer()
                    if not 0<length<=65536 or stream.tell()+length>input_pos:
                        raise ValueError('Invalid OUT identifier length')
                    name=read(length).decode(encoding,errors='strict')
                    canonical_key(name)
                    names.append(name)
                if len({canonical_key(name) for name in checkpointed(names)})!=len(names):
                    raise ValueError('Duplicate OUT identities')
                groups.append(('swmm:'+namespace,tuple(names)))
            quality=[]
            for _ in checkpointed(range(np)):
                code=integer()
                if code not in (0,1,2):raise ValueError('Invalid OUT concentration unit')
                quality.append(('MG/L','UG/L','#/L')[code])
            if stream.tell()!=input_pos:
                raise ValueError('OUT name/unit region does not match its footer')
            input_properties=[]
            for collection,count in checkpointed(zip(('swmm:subcatchments','swmm:nodes','swmm:links'),(ns,nn,nl))):
                properties=integer()
                if not 0<=properties<=64:raise ValueError('Invalid OUT input-property count')
                codes=struct.unpack('<'+'i'*properties,read(4*properties))
                rows=[]
                for _ in checkpointed(range(count)):
                    row=tuple(struct.unpack('<i' if code==0 else '<f',read(4))[0] for code in checkpointed(codes))
                    if not all(math.isfinite(value) for value in checkpointed(row)):
                        raise ValueError('Nonfinite OUT input property')
                    rows.append(row)
                input_properties.append((collection,codes,tuple(rows)))
            variables=[]
            for namespace in checkpointed(('subcatchments','nodes','links','system')):
                count=integer()
                if not 0<count<=np+64:raise ValueError('Invalid OUT variable count')
                codes=struct.unpack('<'+'i'*count,read(4*count))
                if len(set(codes))!=len(codes) or min(codes)<0:
                    raise ValueError('Invalid OUT variable codes')
                variables.append(('swmm:'+namespace,codes))
            saved_start=struct.unpack('<d',read(8))[0]
            interval=integer()
            if interval<=0 or not math.isfinite(saved_start) or stream.tell()!=output_pos:
                raise ValueError('Invalid OUT reporting metadata')
            width=8+4*(sum(count*len(codes) for count,(_,codes) in checkpointed(zip((ns,nn,nl),variables[:3])))+len(variables[3][1]))
            if output_pos+periods*width!=size-24:
                raise ValueError('OUT result size disagrees with its period count')
            def when(value):
                if not math.isfinite(value):raise ValueError('Nonfinite OUT time')
                try:return datetime(1899,12,30)+timedelta(days=value)
                except OverflowError:raise ValueError('OUT time exceeds the calendar range') from None
            first=last=None
            if periods:
                stream.seek(output_pos);first=when(struct.unpack('<d',exact(8))[0])
                stream.seek(output_pos+(periods-1)*width);last=when(struct.unpack('<d',exact(8))[0])
                if last<first:raise ValueError('OUT times run backwards')
            return cls(engine_version=version,flow_units=FLOW_UNITS[units],groups=tuple(groups),pollutant_units=tuple(quality),
                variable_codes=tuple(variables),periods=periods,report_step=timedelta(seconds=interval),saved_start=when(saved_start),
                first_time=first,last_time=last,output_offset=output_pos,period_bytes=width,input_properties=tuple(input_properties),
                identifier_encoding=encoding)
