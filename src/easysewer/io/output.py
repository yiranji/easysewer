"""Bounded SWMM OUT queries with explicit ownership and saved-value semantics."""

from dataclasses import replace
import codecs
from datetime import datetime, timedelta
import hashlib
import math
import os
from pathlib import Path
import struct
import threading

from .output_metadata import NotRecordedError, OutputMetadata
from ..model.identity import Ref, canonical_key
from ..results import ResultSeries, ResultSource, OutputVariables, swmm_output_variables
from ..results.applicability import ResultContext


class OutputChangedError(ValueError):
    pass


class OutputReader:
    """Own one binary file, without a native handle or array allocation.

    Bare OUT does not encode REPORT AVERAGES or the custom backend identity.
    A RunResult supplies that captured context and verifies the artifact digest.
    Read/decode failures close the file. Queries on one reader are serialized;
    separate iterators can interleave because every chunk uses absolute offsets.
    """

    def __init__(self, path, *, metadata=None, expected_sha256=None, expected_size=None,
                 run_id=None, input_sha256=None, backend_sha256=None, variables=None,
                 max_metadata_bytes=64*1024*1024, max_buffer_bytes=1024*1024,
                 max_series_values=1_000_000, invalid_values='raise', encoding=None):
        self._stream=None;self._lock=threading.RLock()
        for key,value,minimum in (('max_buffer_bytes',max_buffer_bytes,12),('max_series_values',max_series_values,1)):
            if type(value) is not int or value<minimum:raise ValueError('Invalid '+key)
        if invalid_values not in ('raise','preserve'):raise ValueError('Invalid observation policy')
        if expected_size is not None and (type(expected_size) is not int or expected_size<0):raise ValueError('Invalid expected size')
        if variables is not None and not isinstance(variables,OutputVariables):raise TypeError('Expected an OutputVariables registry')
        if metadata is not None and not isinstance(metadata,OutputMetadata):raise TypeError('Expected output metadata')
        encoding=codecs.lookup(encoding or (metadata.identifier_encoding if metadata else 'utf-8')).name
        self.path=Path(path).resolve();self.variables=variables or swmm_output_variables()
        self.max_buffer_bytes=max_buffer_bytes;self.max_series_values=max_series_values;self.invalid_values=invalid_values
        # Validate identity fields before opening a file.
        source=ResultSource(format='swmm:out',path=str(self.path),sha256=expected_sha256,
            run_id=run_id,input_sha256=input_sha256,backend_sha256=backend_sha256,encoding=encoding)
        try:
            self._stream=self.path.open('rb')
            self._identity=self._stat()
            if expected_size is not None and self._identity[2]!=expected_size:raise OutputChangedError('OUT artifact size changed')
            actual=OutputMetadata.from_stream(self._stream,max_metadata_bytes=max_metadata_bytes,encoding=encoding)
            if metadata is not None:
                if not isinstance(metadata.result_context,ResultContext):raise TypeError('Expected execution result context')
                if replace(metadata,semantics=(),averages=None,producer=None,result_context=ResultContext())!=actual:
                    raise OutputChangedError('OUT metadata does not match the captured run')
                if metadata.averages is not None and type(metadata.averages) is not bool:raise ValueError('Invalid sampling context')
            self.metadata=metadata or actual
            if expected_sha256 is not None:
                self._stream.seek(0);digest=hashlib.sha256()
                while block:=self._stream.read(max_buffer_bytes):digest.update(block)
                if digest.hexdigest()!=expected_sha256:raise OutputChangedError('OUT artifact digest changed')
            self._unchanged()
            self.source=replace(source,engine_version=actual.engine_version)
            self._indices={key:{canonical_key(name):i for i,name in enumerate(names)} for key,names in actual.groups}
            self._variables={key:self.variables.available(actual,key) for key,_ in actual.variable_codes}
            self._offsets={};offset=8
            for key,codes in actual.variable_codes:
                self._offsets[key]=offset
                count=1 if key=='swmm:system' else len(actual.names(key))
                offset+=4*count*len(codes)
        except BaseException:
            self.close();raise

    @property
    def closed(self):
        return self._stream is None or self._stream.closed

    def _stat(self):
        stat=os.fstat(self._stream.fileno())
        return stat.st_dev,stat.st_ino,stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns

    def _unchanged(self):
        if self.closed:raise ValueError('Output reader is closed')
        if self._stat()!=self._identity:raise OutputChangedError('OUT changed during reading')

    def close(self):
        with self._lock:
            if self._stream is not None:
                stream=self._stream;self._stream=None;stream.close()

    def __enter__(self):
        if self.closed:raise ValueError('Output reader is closed')
        return self

    def __exit__(self,*args):
        self.close()

    def available_variables(self, collection):
        return self._variables[collection]

    def _selection(self,target,variable,pollutant,start,stop):
        with self._lock:
            try:self._unchanged()
            except BaseException:self.close();raise
        if target is not None and (not isinstance(target,Ref) or type(target.key) is not str):
            raise TypeError('OUT queries require a string object ID reference')
        collection=target.collection if target else 'swmm:system'
        index=0
        if target is not None:
            try:index=self._indices[collection][canonical_key(target.key)]
            except KeyError:raise NotRecordedError(f'{collection}/{target.key} is not recorded in this OUT') from None
            target=Ref(collection=collection,key=self.metadata.names(collection)[index])
        definition=next((row for row in self._variables.get(collection,()) if row.key==variable),None)
        if definition is None:raise NotRecordedError(f'{variable} is not recorded for {collection}')
        code=definition.code;concentration_unit=None
        if definition.pollutant:
            if not isinstance(pollutant,Ref) or pollutant.collection!='swmm:pollutants' or type(pollutant.key) is not str:
                raise TypeError('Concentration queries require a separate pollutant reference')
            try:p=self._indices['swmm:pollutants'][canonical_key(pollutant.key)]
            except KeyError:raise NotRecordedError('Pollutant is not recorded: '+str(pollutant.key)) from None
            pollutant=Ref(collection='swmm:pollutants',key=self.metadata.names('swmm:pollutants')[p])
            code+=p;concentration_unit=self.metadata.pollutant_units[p]
        elif pollutant is not None:raise ValueError('This variable does not have a pollutant dimension')
        codes=dict(self.metadata.variable_codes)[collection]
        if code not in codes:raise NotRecordedError('Selected pollutant variable is absent from this OUT')
        stop=self.metadata.periods if stop is None else stop
        if type(start) is not int or type(stop) is not int or not 0<=start<=stop<=self.metadata.periods:
            raise ValueError('Period range must be within [0, periods), with an exclusive stop')
        offset=self._offsets[collection]+4*(index*len(codes)+codes.index(code))
        semantics=dict(self.metadata.semantics).get(collection+':'+variable.split(':')[-1])
        if semantics is None:
            semantics=definition.semantics or (variable if self.metadata.producer else 'producer-unspecified:'+variable)
        sampling='producer-unspecified' if definition.semantics=='unknown' else self._sampling(collection,definition.code,index)
        context=dict(source=self.source,target=target,variable=variable,pollutant=pollutant,
            unit=definition.unit(self.metadata.flow_units,concentration_unit),sampling=sampling,semantics=semantics,
            applicability=self.metadata.result_context.output(collection,variable,target=target))
        return start,stop,offset,context

    def _sampling(self,collection,code,index):
        averages=self.metadata.averages
        if collection!='swmm:system' and code>{'swmm:subcatchments':8,'swmm:nodes':6,'swmm:links':5}[collection]:
            return 'producer-unspecified'
        if collection=='swmm:subcatchments':return 'report-time-value'
        if collection=='swmm:system':
            if code>14:return 'producer-unspecified'
            if code==9:return 'reported-combination-of-runoff-and-routing-inflows'
            if code in (5,6,7,8,10,11):return 'routing-state-at-report'
            if code!=12:return 'report-time-system-value'
            return 'routing-state-at-report' if averages else ('report-time-value' if averages is False else 'producer-unspecified')
        if averages is None:return 'producer-unspecified'
        if not averages:return 'report-time-interpolation'
        if collection=='swmm:links' and code==4:
            for name,codes,values in self.metadata.input_properties:
                if name==collection and 0 in codes:
                    return 'routing-step-arithmetic-mean' if values[index][codes.index(0)]==0 else 'last-routing-step-setting'
            return 'producer-unspecified'
        return 'routing-step-arithmetic-mean'

    def _read(self,offset,count):
        self._stream.seek(offset);raw=self._stream.read(count)
        if len(raw)!=count:raise ValueError('Truncated OUT result record')
        return raw

    @staticmethod
    def _time(value):
        if not math.isfinite(value):raise ValueError('Nonfinite OUT result time')
        try:return datetime(1899,12,30)+timedelta(days=value)
        except OverflowError:raise ValueError('OUT time exceeds the calendar range') from None

    def _chunk(self,start,stop,offset,context):
        with self._lock:
            try:
                self._unchanged()
                base=self.metadata.output_offset;width=self.metadata.period_bytes
                previous=None
                if start:
                    previous=self._time(struct.unpack('<d',self._read(base+(start-1)*width,8))[0])
                contiguous=(stop-start)*width<=self.max_buffer_bytes
                block=self._read(base+start*width,(stop-start)*width) if contiguous else None
                times=[];values=[];missing=[]
                for period in range(start,stop):
                    if block is not None:
                        cursor=(period-start)*width
                        when=struct.unpack_from('<d',block,cursor)[0]
                        value=struct.unpack_from('<f',block,cursor+offset)[0]
                    else:
                        cursor=base+period*width
                        when=struct.unpack('<d',self._read(cursor,8))[0]
                        value=struct.unpack('<f',self._read(cursor+offset,4))[0]
                    when=self._time(when)
                    if previous is not None and when<=previous:raise ValueError('OUT result times do not increase')
                    previous=when;times.append(when)
                    if not math.isfinite(value):
                        if self.invalid_values=='raise':raise ValueError('Nonfinite OUT observation at period '+str(period))
                        values.append(None);missing.append('nonfinite_source_value')
                    elif context['applicability'].unavailable:
                        values.append(None);missing.append(context['applicability'].reasons[0])
                    else:values.append(value);missing.append(None)
                self._unchanged()
                return ResultSeries(**context,periods=tuple(range(start,stop)),times=tuple(times),values=tuple(values),missing=tuple(missing))
            except BaseException:
                self.close();raise

    def iter_chunks(self,target,variable,*,pollutant=None,start=0,stop=None,chunk_size=8192):
        """Yield immutable series chunks. The caller owns this reader's lifetime."""
        if type(chunk_size) is not int or not 1<=chunk_size<=self.max_series_values:
            raise ValueError('Chunk size exceeds the result-value budget')
        start,stop,offset,context=self._selection(target,variable,pollutant,start,stop)
        for first in range(start,stop,chunk_size):
            yield self._chunk(first,min(stop,first+chunk_size),offset,context)

    def series(self,target,variable,*,pollutant=None,start=0,stop=None):
        start,stop,offset,context=self._selection(target,variable,pollutant,start,stop)
        if stop-start>self.max_series_values:raise ValueError('Series exceeds the result-value budget; use iter_chunks')
        periods=[];times=[];values=[];missing=[]
        for first in range(start,stop,8192):
            chunk=self._chunk(first,min(stop,first+8192),offset,context)
            periods.extend(chunk.periods);times.extend(chunk.times);values.extend(chunk.values);missing.extend(chunk.missing)
        return ResultSeries(**context,periods=tuple(periods),times=tuple(times),values=tuple(values),missing=tuple(missing))
