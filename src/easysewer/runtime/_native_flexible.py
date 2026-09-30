"""Custom ponding ABI in an isolated worker; no Model or mutable user state."""

import ctypes as c
import hashlib
import json
import math
import os
from pathlib import Path

from ._native_solver import NativeSolver
from .backend import NativeFailure
from .flexible import FlexiblePondingPolicy, adjust_ponding, finite
from ..io.json import JsonDocument
from ..model.identity import canonical_key


class NativeFlexibleSolver(NativeSolver):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.parameters=None;self.trace=None;self.records=[];self.steps=0
        self._checkpoint_ready=False
        self.pollutants=[];self.statistics=[]
        for name,args,result in (
            ('execRouting',[],c.c_int),('saveResults',[],c.c_int),
            ('getFlexiblePondingAbi',[],c.c_int),
            ('getPondingStep',[],c.c_double),
            ('getPondingNodeStat',[c.c_int,c.c_int,c.POINTER(c.c_double)],c.c_int),
            ('getNodePollutant',[c.c_int,c.c_int,c.POINTER(c.c_double)],c.c_int),
            ('getCurrentTime',[],c.c_double),('getRoutingDuration',[],c.c_double),
            ('setValue',[c.c_int,c.c_int,c.c_double],None)):
            function=getattr(self.lib,'swmm_'+name)
            function.argtypes=args;function.restype=result
        if self.lib.swmm_getFlexiblePondingAbi()!=201:
            raise ValueError('FlexiblePonding requires custom ABI 201 with discrete volume accounting')
        self.metadata['abi']+=':flexible:201'

    def open(self, paths):
        self.input_sha256=hashlib.sha256(Path(paths[0]).read_bytes()).hexdigest()
        value=super().open(paths)
        self.flow_units=value['flow_units']
        self.names=dict(value['groups'])['swmm:nodes']
        count=self.lib.swmm_getCount(4)
        if not 0<=count<=10_000_000:raise ValueError('Invalid native pollutant count')
        for index in range(count):
            name=c.create_string_buffer(256)
            self.lib.swmm_getName(4,index,name,len(name))
            units=self.lib.swmm_getValue(500,index)
            if not name.value or self.lib.swmm_getIndex(4,name.value)!=index or units not in (0,1,2):
                raise ValueError('Native pollutant identity/units verification failed')
            self.pollutants.append(dict(id=name.value.decode('utf-8'),index=index,
                concentration_unit=('MG/L','UG/L','#/L')[int(units)],
                quantity_unit=('mg','ug','count')[int(units)],removed_quantity=0.))
        if len({canonical_key(row['id']) for row in self.pollutants})!=count:
            raise ValueError('Duplicate native pollutants')
        value['groups'].append(('swmm:pollutants',[row['id'] for row in self.pollutants]))
        return value

    def configure(self, parameters):
        if self.parameters is not None:raise ValueError('Execution binding is already fixed')
        expected={'policy','input_sha256','flow_units','nodes','trace','depth_threshold','flow_threshold',
                  'flow_per_volume_rate','depth_unit','volume_unit','volume_to_liters','quality_enabled','pollutants'}
        if type(parameters) is not dict or set(parameters)!=expected:
            raise ValueError('Invalid FlexiblePonding binding fields')
        self.policy=FlexiblePondingPolicy.from_json_document(JsonDocument.from_data(parameters['policy']))
        if parameters['input_sha256']!=self.input_sha256:
            raise ValueError('Execution binding does not match the actual opened INP')
        if parameters['flow_units']!=('CFS','GPM','MGD','CMS','LPS','MLD')[self.flow_units]:
            raise ValueError('Execution binding differs from actual native flow units')
        for key in ('depth_threshold','flow_threshold','flow_per_volume_rate'):finite(parameters[key],key)
        if parameters['flow_per_volume_rate']<=0:raise ValueError('Invalid flow/volume conversion')
        if finite(parameters['volume_to_liters'],'volume_to_liters')<=0 or type(parameters['quality_enabled']) is not bool:
            raise ValueError('Invalid quality binding')
        actual={canonical_key(row['id']):row['concentration_unit'] for row in self.pollutants}
        declared={canonical_key(row['id']):row['units'] for row in parameters['pollutants']}
        if len(declared)!=len(parameters['pollutants']) or actual!=declared:
            raise ValueError('Actual native pollutants differ from the captured binding')
        identities={canonical_key(name):index for index,name in enumerate(self.names)}
        seen=set()
        for node in parameters['nodes']:
            if set(node)!={'id','area','volume_per_depth'}:raise ValueError('Invalid ponding node binding')
            key=canonical_key(node['id'])
            if key in seen or key not in identities:raise ValueError('Ponding node identity is absent or repeated')
            seen.add(key);index=identities[key]
            if self.lib.swmm_getIndex(2,node['id'].encode())!=index or self.lib.swmm_getValue(300,index) not in (0,3):
                raise ValueError('Actual native node type/index differs from the ponding binding')
            if finite(node['area'],'ponded area')<=0 or finite(node['volume_per_depth'],'volume per depth')<=0:
                raise ValueError('Ponded area must be positive')
            self.records.append(dict(id=self.names[index],index=index,area=node['area'],volume_per_depth=node['volume_per_depth'],
                removed_volume=0.,active_steps=0))
        if parameters['trace'] is not None:
            target=Path(parameters['trace'])
            if target.is_absolute() or '..' in target.parts or not target.resolve().is_relative_to(Path.cwd()):
                raise ValueError('Trace must be inside the private working directory')
            if target.exists() or not target.parent.is_dir():raise ValueError('Trace target must be an unused reserved file')
        self.parameters=parameters

    def _write(self, value):
        if self.trace:
            self.trace.write(json.dumps(value,ensure_ascii=False,allow_nan=False,separators=(',',':'))+'\n')

    def _state(self, index):
        values=dict(depth=self.lib.swmm_getValue(310,index),volume=self.lib.swmm_getValue(305,index),
                    overflow=self.lib.swmm_getValue(308,index))
        finite(values['depth'],'native ponding depth');finite(values['volume'],'native volume')
        if not math.isfinite(values['overflow']):raise ValueError('Nonfinite native overflow')
        return values

    def start(self, save_results):
        self._checkpoint_ready=False
        if self.parameters is None:raise ValueError('FlexiblePonding requires a captured execution binding before start')
        if self.parameters['trace']:
            self.trace=Path(self.parameters['trace']).open('x',encoding='utf-8',newline='\n')
            self._write(dict(kind='easysewer:flexible-ponding-trace',schema_version='1.0',parameters=self.parameters))
        super().start(save_results)
        self.duration=finite(self.lib.swmm_getRoutingDuration(),'native duration')/1000
        self.time=finite(self.lib.swmm_getCurrentTime(),'native time')*86400
        if self.duration<=0:raise ValueError('Native duration must be positive')
        self.previous={node['index']:self._state(node['index']) for node in self.records}
        self._checkpoint_ready=True

    def step(self, max_steps=1):
        self._checkpoint_ready=False
        count=0
        while count<max_steps and self.time<self.duration-1e-6:
            self.check('exec_routing',self.lib.swmm_execRouting())
            now=finite(self.lib.swmm_getCurrentTime(),'native time')*86400
            dt=self.lib.swmm_getPondingStep()
            if not math.isfinite(dt) or dt<=0 or now<=self.time or now>self.duration+1e-6:
                raise ValueError('Custom routing clock did not advance within the simulation window')
            rows=[]
            for node in self.records:
                index=node['index'];before=self._state(index);previous=self.previous[index]
                adjusted=adjust_ponding(previous_depth=previous['depth'],previous_volume=previous['volume'],**before,
                    area=node['volume_per_depth'],dt=dt,ratio=self.policy.external_flooding_ratio,
                    depth_threshold=self.parameters['depth_threshold'],flow_threshold=self.parameters['flow_threshold'],
                    flow_per_volume_rate=self.parameters['flow_per_volume_rate'])
                self.lib.swmm_setValue(311,index,adjusted.external_flow)
                if adjusted.removed_volume:
                    for code,value in ((310,adjusted.depth),(308,adjusted.overflow),(305,adjusted.volume)):
                        self.lib.swmm_setValue(code,index,value)
                after=self._state(index)
                for key in ('depth','volume','overflow'):
                    if not math.isclose(after[key],getattr(adjusted,key),rel_tol=1e-10,abs_tol=1e-10):
                        raise ValueError('Custom native setter failed its read-back check: '+key)
                if not math.isclose(self.lib.swmm_getValue(311,index),adjusted.external_flow,rel_tol=1e-10,abs_tol=1e-10):
                    raise ValueError('Custom external flooding setter failed its read-back check')
                self.previous[index]=after
                node['removed_volume']+=adjusted.removed_volume
                node['active_steps']+=bool(adjusted.removed_volume)
                quality=[]
                if self.parameters['quality_enabled']:
                    for pollutant in self.pollutants:
                        concentration=c.c_double()
                        self.check('ponding_quality',self.lib.swmm_getNodePollutant(index,pollutant['index'],c.byref(concentration)))
                        finite(concentration.value,'native concentration')
                        amount=adjusted.removed_volume*self.parameters['volume_to_liters']*concentration.value
                        finite(amount,'removed pollutant quantity')
                        pollutant['removed_quantity']+=amount
                        quality.append(dict(id=pollutant['id'],concentration=concentration.value,removed_quantity=amount))
                rows.append(dict(step=self.steps+1,time_seconds=now,dt_seconds=dt,id=node['id'],index=index,
                    previous=previous,before=before,after=after,external_flow=adjusted.external_flow,
                    removed_volume=adjusted.removed_volume,quality=quality if self.parameters['quality_enabled'] else None))
            self.check('save_results',self.lib.swmm_saveResults())
            for row in rows:
                if not math.isclose(self.lib.swmm_getValue(311,row['index']),row['external_flow'],rel_tol=1e-10,abs_tol=1e-10):
                    raise ValueError('Native finalization changed the applied external flow')
                self._write(row)
            self.time=now;self.steps+=1;count+=1
        finished=self.time>=self.duration-1e-6
        self._checkpoint_ready=True
        return dict(elapsed_days=0. if finished else self.time/86400,finished=finished,steps=count)

    def execution_results(self):
        return dict(policy=self.policy.policy,input_sha256=self.input_sha256,steps=self.steps,
            time_seconds=self.time,volume_unit=self.parameters['volume_unit'],flow_unit=self.parameters['flow_units'],
            removed_volume=math.fsum(node['removed_volume'] for node in self.records),nodes=self.records,
            quality_enabled=self.parameters['quality_enabled'],pollutants=self.pollutants,
            native_statistics=self.statistics,accounting='discrete-volume:1',
            statistics_metadata=dict(
                depth_unit=self.parameters['depth_unit'],volume_unit=self.parameters['volume_unit'],
                flow_unit=self.parameters['flow_units'],duration_unit='s',
                date_unit='days since 1899-12-30 in the model calendar; no timezone',
                removed_volume_scope='entire simulation',
                statistics_scope='active routing steps ending at or after native ReportStart',
                interval_scope='entire routing interval, including an interval crossing ReportStart',
                mean_depth_aggregation='arithmetic mean of routing-step end depths; not time weighted',
                unobserved_peak_date='simulation start when no strictly positive peak was observed'))

    def _close_trace(self):
        if self.trace:
            trace=self.trace;self.trace=None
            try:trace.flush();os.fsync(trace.fileno())
            finally:trace.close()

    def end(self):
        self._checkpoint_ready=False
        fields=('removed_volume','maximum_depth','maximum_flooding_flow','flooding_volume',
                'maximum_ponded_volume','flooded_seconds','maximum_depth_date','maximum_flooding_date','routing_step_mean_depth')
        expected={node['index']:node['removed_volume'] for node in self.records}
        for index,name in enumerate(self.names):
            stats=dict(id=name,index=index)
            for field,key in enumerate(fields):
                value=c.c_double()
                self.check('ponding_statistics',self.lib.swmm_getPondingNodeStat(index,field,c.byref(value)))
                stats[key]=finite(value.value,'native '+key)
            if not math.isclose(stats['removed_volume'],expected.get(index,0.),rel_tol=1e-9,abs_tol=1e-9):
                raise ValueError('Native removed volume does not match the policy ledger')
            self.statistics.append(stats)
        for pollutant in self.pollutants:
            if not self.parameters['quality_enabled']:
                pollutant['removed_quantity']=None
                continue
            actual=finite(self.lib.swmm_getValue(501,pollutant['index']),'native removed pollutant quantity')
            if not math.isclose(actual,pollutant['removed_quantity'],rel_tol=1e-9,abs_tol=1e-9):
                raise ValueError('Native removed pollutant quantity does not match the policy ledger')
            pollutant['native_removed_quantity']=actual
        result=super().end()
        self._write(dict(kind='completed',summary=self.execution_results()))
        self._close_trace()
        return result

    def cleanup(self, on_error=None):
        self._checkpoint_ready=False
        issues=super().cleanup(on_error=on_error)
        try:self._close_trace()
        except Exception as error:
            issue=NativeFailure(stage='trace_close',code=None,message=str(error));issues.append(issue)
            if on_error:on_error(issue)
        return issues
