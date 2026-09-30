"""Portable cache production conditions and explicitly registered comparisons.

Matching captured conditions is not a claim that sampled RUNOFF reproduces all
hydrologic state or that a fresh solve will be bitwise equal to interpolation.
No file is opened by capture: resource evidence comes from the run snapshot.
"""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
import hashlib
import json

from ..io.json import JsonDocument
from ..model.identity import canonical_key, namespace_key
from ..results.series import digest
from ..validation._cooperative import checkpointed
from ..io.json._work import equal_json


def _key(value):
    if type(value) is not str:raise TypeError('Expected a versioned policy key')
    namespace_key(value.rsplit(':',1)[0] if value.rsplit(':',1)[-1].isdigit() else value)


def _codes(values):
    if type(values) is not tuple or any(type(v) is not str for v in checkpointed(values)):
        raise TypeError('Expected immutable reason codes')
    if len(set(values))!=len(values):raise ValueError('Duplicate reason code')
    for value in checkpointed(values):namespace_key(value)


def _fields(data, expected):
    if type(data) is not dict or set(data)!=set(expected):raise ValueError('Invalid cache evidence fields')
    return data


@dataclass(frozen=True, kw_only=True)
class CacheContext:
    policy: str
    kind: str
    input_sha256: str
    engine_sha256: str | None
    facts: JsonDocument
    limitations: tuple[str, ...] = ()

    def __post_init__(self):
        _key(self.policy)
        if type(self.kind) is not str or not self.kind:raise ValueError('Expected a cache kind')
        if self.input_sha256 is None:raise ValueError('Input evidence requires a digest')
        digest(self.input_sha256);digest(self.engine_sha256);_codes(self.limitations)
        if not isinstance(self.facts,JsonDocument) or type(self.facts.data) is not dict:
            raise TypeError('Cache facts require immutable JSON object data')
        for key in checkpointed(self.facts.data):namespace_key(key)

    def to_data(self):
        return dict(policy=self.policy,kind=self.kind,input_sha256=self.input_sha256,
            engine_sha256=self.engine_sha256,facts=self.facts.data,limitations=list(self.limitations))

    @classmethod
    def from_data(cls, data):
        _fields(data,('policy','kind','input_sha256','engine_sha256','facts','limitations'))
        if type(data['limitations']) is not list:raise TypeError('Expected limitation array')
        return cls(**{k:data[k] for k in checkpointed(('policy','kind','input_sha256','engine_sha256'))},
            facts=JsonDocument.from_data(data['facts']),limitations=tuple(data['limitations']))

    @property
    def sha256(self):
        raw=json.dumps(self.to_data(),sort_keys=True,separators=(',',':'),ensure_ascii=False).encode('utf-8')
        return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True, kw_only=True)
class CacheEvidence:
    """A portable assertion binding condition evidence to exact cache bytes."""
    cache_sha256: str
    context: CacheContext

    def __post_init__(self):
        if self.cache_sha256 is None:raise ValueError('Cache evidence requires a digest')
        digest(self.cache_sha256)
        if not isinstance(self.context,CacheContext):raise TypeError('Expected cache context')

    def to_bytes(self):
        return JsonDocument.from_data(dict(schema='easysewer:cache-evidence:1',
            cache_sha256=self.cache_sha256,context=self.context.to_data())).to_bytes()

    @classmethod
    def from_bytes(cls, data):
        value=JsonDocument.from_bytes(data).data
        _fields(value,('schema','cache_sha256','context'))
        if value['schema']!='easysewer:cache-evidence:1':raise ValueError('Unknown cache evidence envelope')
        return cls(cache_sha256=value['cache_sha256'],context=CacheContext.from_data(value['context']))


@dataclass(frozen=True, kw_only=True)
class CacheReuse:
    policy: str = 'easysewer:unknown-cache-conditions:1'
    status: str = 'unknown'
    intent: str = 'inspect'
    origin: str = 'unavailable'
    differences: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ('cache:producer-evidence-unavailable',)
    producer_context_sha256: str | None = None
    consumer_context_sha256: str | None = None

    def __post_init__(self):
        _key(self.policy);_codes(self.differences);_codes(self.reasons)
        if self.status not in ('matched','changed','unknown'):raise ValueError('Unknown cache comparison status')
        if self.intent not in ('inspect','require_match','frozen'):raise ValueError('Unknown cache reuse intent')
        if self.origin not in ('unavailable','runner-observed','caller-asserted'):raise ValueError('Unknown cache evidence origin')
        if self.status=='matched' and (self.differences or self.reasons):raise ValueError('Matched conditions cannot have differences or unknown reasons')
        if self.status!='matched' and not self.reasons:raise ValueError('Limited comparison requires reasons')
        digest(self.producer_context_sha256);digest(self.consumer_context_sha256)
        if self.status=='matched' and (self.origin=='unavailable' or self.producer_context_sha256 is None or self.consumer_context_sha256 is None):
            raise ValueError('Matched conditions require identified producer and consumer evidence')

    @property
    def allowed(self):
        return self.intent!='require_match' or self.status=='matched'

    def to_data(self):
        return dict(policy=self.policy,status=self.status,intent=self.intent,origin=self.origin,
            differences=list(self.differences),reasons=list(self.reasons),
            producer_context_sha256=self.producer_context_sha256,consumer_context_sha256=self.consumer_context_sha256)

    @classmethod
    def from_data(cls,data):
        fields=('policy','status','intent','origin','differences','reasons','producer_context_sha256','consumer_context_sha256')
        _fields(data,fields)
        if any(type(data[k]) is not list for k in checkpointed(('differences','reasons'))):raise TypeError('Expected comparison arrays')
        return cls(**{k:tuple(v) if k in ('differences','reasons') else v for k,v in checkpointed(data.items())})


@dataclass(frozen=True, kw_only=True)
class CachePolicy:
    key: str
    kind: str
    capture: object
    compare: object

    def __post_init__(self):
        _key(self.key)
        if type(self.kind) is not str or not self.kind:raise ValueError('Expected a cache kind')
        if not callable(self.capture) or not callable(self.compare):raise TypeError('Cache policy requires trusted callbacks')


@dataclass(frozen=True, kw_only=True)
class CachePolicies:
    policies: tuple[CachePolicy, ...] = ()

    def __post_init__(self):
        if type(self.policies) is not tuple or any(type(p) is not CachePolicy for p in checkpointed(self.policies)):
            raise TypeError('Expected immutable cache policies')
        if len({p.key for p in checkpointed(self.policies)})!=len(self.policies) or len({p.kind for p in checkpointed(self.policies)})!=len(self.policies):
            raise ValueError('Duplicate cache policy or kind')

    def capture(self, kind, snapshot):
        if hashlib.sha256(snapshot.input_bytes).hexdigest()!=snapshot.input_sha256:
            raise ValueError('Snapshot input differs from its digest')
        for policy in checkpointed(self.policies):
            if policy.kind==kind:
                value=policy.capture(snapshot)
                if not isinstance(value,CacheContext) or (value.policy,value.kind,value.input_sha256,value.engine_sha256)!=(policy.key,kind,snapshot.input_sha256,snapshot.backend.sha256):
                    raise ValueError('Cache adapter returned mismatched context')
                return value
        return CacheContext(policy='easysewer:unknown-cache-conditions:1',kind=kind,
            input_sha256=snapshot.input_sha256,engine_sha256=snapshot.backend.sha256,
            facts=JsonDocument.from_data({}),limitations=('cache:unsupported-kind',))

    def assess(self, evidence, consumer, *, cache_sha256, intent='inspect', origin='caller-asserted'):
        base=CacheReuse(policy=consumer.policy,intent=intent,origin=origin if evidence else 'unavailable',
            consumer_context_sha256=consumer.sha256)
        if evidence is None:return base
        if not isinstance(evidence,CacheEvidence):raise TypeError('Expected cache evidence')
        if evidence.cache_sha256!=cache_sha256 or evidence.context.kind!=consumer.kind:
            raise ValueError('Condition evidence does not identify the consumed cache')
        producer=evidence.context;base=replace(base,producer_context_sha256=producer.sha256)
        for policy in checkpointed(self.policies):
            if policy.key==producer.policy==consumer.policy and policy.kind==consumer.kind:
                status,differences,reasons=policy.compare(producer,consumer)
                return replace(base,status=status,differences=differences,reasons=reasons)
        return replace(base,reasons=('cache:comparison-policy-unavailable',))


RUNOFF_POLICY='swmm:runoff-production-conditions:1'
RDII_POLICY='swmm:rdii-production-conditions:1'
HOTSTART_POLICY='swmm:hotstart-continuation-conditions:1'
_NONPHYSICAL={'swmm:title','easysewer:metadata','swmm:map','swmm:backdrop','swmm:report','swmm:tags','swmm:labels','swmm:profiles'}
_COLLECTIONS={'swmm:'+name for name in ('curves','timeseries','patterns','transects','streets','inlets',
    'inlet_usage','raingages','subcatchments','snowpacks','subcatchment_adjustments','climate','inflows',
    'dwf','nodes','links','controls','hydrographs','rdii','pollutants','landuses','coverages','loadings',
    'buildup','washoff','aquifers','groundwater','gwf','treatment','lid_controls','lid_usage','events')}
_REQUIRED=_COLLECTIONS | {'swmm:effective-options','swmm:calendar','swmm:initial-interfaces',
    'swmm:groundwater-bindings','easysewer:backend-policy'}
_RDII_HISTORY={'initialization':'empty-rainfall-memory-at-start',
    'coverage':'producer-calendar-including-dry-gaps',
    'format':'SWMM5-RDII','units':'CFS','identity':'native-node-indices'}
_HOTSTART_SCOPE={'format':'SWMM5-HOTSTART4','checkpoint':'partial',
    'hydraulic-clock':'producer-end',
    'runoff-clock':'last-runoff-step-may-differ-from-hydraulic-end',
    'omitted':['solver-integration-history','climate-and-gage-history',
        'lid-state','rdii-convolution-and-abstraction-memory','control-memory']}


def _runoff_context(snapshot):
    return _swmm_context(snapshot,kind='RUNOFF',policy=RUNOFF_POLICY)


def _swmm_context(snapshot, *, kind, policy):
    from ..io.inp import InpDocument
    from ..model import Model
    from ..model.file_resources import file_references, replace_path
    from ..model.identity import Ref
    from ..schema import EPA_SWMM_5_2_4
    from ..validation import ValidationError
    issues=[];facts={}
    def unknown(code):
        return CacheContext(policy=policy,kind=kind,input_sha256=snapshot.input_sha256,
            engine_sha256=snapshot.backend.sha256,facts=JsonDocument.from_data(facts),limitations=(code,))
    if snapshot.profile!=EPA_SWMM_5_2_4:return unknown('cache:unsupported-profile')
    if snapshot.backend.sha256 is None or 'easysewer:runoff-physics:1' not in snapshot.backend.capabilities:
        return unknown('cache:unverified-engine')
    try:model=Model.from_document(InpDocument.from_bytes(snapshot.input_bytes),strict=True)
    except (ValueError,TypeError,ValidationError):return unknown('cache:unsupported-input')
    if model._store.opaque_constraints:return unknown('cache:unsupported-input')
    types=model._schema.json_types
    resources={(row.owner.canonical,row.field):row for row in checkpointed(snapshot.resources)}
    if len(resources)!=len(snapshot.resources):raise ValueError('Duplicate snapshot resource identity')
    for specification in checkpointed(model._store.specifications):
        namespace=specification.key
        if namespace in _NONPHYSICAL or namespace in ('swmm:options','swmm:files'):continue
        if namespace not in _COLLECTIONS:issues.append('cache:unsupported-collection')
        rows=[]
        for key,row in checkpointed(model.collection(namespace).items()):
            if namespace=='swmm:groundwater':
                aquifer=model.aquifers[row.aquifer.key]
                row=replace(row,**{name:getattr(row,name) if getattr(row,name) is not None else getattr(aquifer,name)
                    for name in checkpointed(('bottom_elevation','water_table_elevation','upper_moisture'))},
                    threshold_elevation=row.threshold_elevation if row.threshold_elevation is not None else model.nodes[row.node.key].elevation)
            # Positions and report destinations do not enter runoff production.
            owner=Ref(collection=namespace,key=key)
            for path,reference in checkpointed(file_references(row)):
                if reference.direction=='output':
                    row=replace_path(row,path,None)
                    continue
                record=resources.get((owner.canonical,path))
                sha=record.sha256 if record is not None else None
                if sha is None:issues.append('cache:resource-evidence-unavailable')
                row=replace_path(row,path,replace(reference,path=sha or 'unavailable',base_directory=None,flavor='native'))
            encoded=types.encode(row)
            for field in checkpointed({'swmm:nodes':('position','polygon'),'swmm:links':('vertices',),
                          'swmm:raingages':('position',),'swmm:subcatchments':('polygon',)}.get(namespace,())):
                encoded.pop(field,None)
            rows.append(encoded)
        facts[namespace]=rows
    values=types.encode(model.effective_options.values)
    for key in checkpointed(('temp_directory','report_start_date','report_start_time')):values.pop(key,None)
    facts['swmm:effective-options']=values
    facts['swmm:calendar']={'start':model.effective_options.start.isoformat(),'end':model.effective_options.end.isoformat()}
    facts['swmm:initial-interfaces']=[]
    for binding in checkpointed(model.files.values()):
        if binding.mode!='USE' or binding.kind==kind:continue
        record=resources.get((Ref(collection='swmm:files',key=binding.key).canonical,('file',)))
        if record is None or record.sha256 is None:issues.append('cache:resource-evidence-unavailable')
        facts['swmm:initial-interfaces'].append(dict(kind=binding.kind,sha256=record.sha256 if record else None))
    facts['swmm:groundwater-bindings']=[]
    for row in checkpointed(model.groundwater.values()):
        aquifer=model.aquifers[row.aquifer.key]
        facts['swmm:groundwater-bindings'].append(dict(subcatchment=canonical_key(row.subcatchment.key),
            receiver=canonical_key(row.node.key),bottom=row.bottom_elevation if row.bottom_elevation is not None else aquifer.bottom_elevation,
            area=model.subcatchments[row.subcatchment.key].area))
    facts['easysewer:backend-policy']=dict(key=snapshot.backend.key,numerical_policy=snapshot.backend.numerical_policy,
        capabilities=list(snapshot.backend.capabilities))
    if snapshot.backend_settings is not None:
        if snapshot.backend.key!='easysewer:flexible-ponding':issues.append('cache:unsupported-backend-settings')
        else:
            settings=JsonDocument.from_bytes(snapshot.backend_settings).data
            for key in checkpointed(('input_sha256','trace')):settings.pop(key,None)
            if type(settings.get('policy')) is dict:settings['policy'].pop('record_steps',None)
            facts['easysewer:backend-settings']=settings
    if kind in ('RDII','HOTSTART'):
        # Calendars have different relations for replay and continuation; they
        # must not also be compared for equality inside the option dictionary.
        for field in checkpointed(('start_date','start_time','end_date','end_time')):values.pop(field,None)
    if kind=='RDII':
        facts['swmm:rdii-history']=_RDII_HISTORY
    if kind=='HOTSTART':
        from ..model.resources import InlineTimeSeries, FileTimeSeries
        # Elapsed series restart at each simulation's own start. External
        # series and control/climate clocks are conservatively start-bound:
        # their contents are not opened or reinterpreted by this adapter.
        anchored=[]
        for row in checkpointed(model.timeseries.values()):
            if isinstance(row,FileTimeSeries) or (isinstance(row,InlineTimeSeries) and
                    any(isinstance(point.time,timedelta) for point in checkpointed(row.points))):
                anchored.append('timeseries:'+canonical_key(row.id))
        if model.controls:anchored.append('controls')
        if model.climate.file is not None:anchored.append('climate-file')
        facts['swmm:time-origins']={key:model.effective_options.start.isoformat() for key in checkpointed(anchored)}
        facts['swmm:hotstart-state-scope']=_HOTSTART_SCOPE
    return CacheContext(policy=policy,kind=kind,input_sha256=snapshot.input_sha256,
        engine_sha256=snapshot.backend.sha256,facts=JsonDocument.from_data(facts),limitations=tuple(dict.fromkeys(issues)))


def _compare_runoff(producer,consumer):
    return _compare_conditions(producer,consumer,required=_REQUIRED)


def _compare_conditions(producer,consumer, *, required, calendar=None, invariants=None):
    left,right=producer.facts.data,consumer.facts.data
    excluded={'swmm:calendar'} if calendar is not None else set()
    differences=tuple(sorted(k for k in checkpointed((left.keys() | right.keys())-excluded) if not equal_json(left.get(k),right.get(k))))
    if producer.engine_sha256!=consumer.engine_sha256:differences+=('cache:engine',)
    reasons=tuple(dict.fromkeys(producer.limitations+consumer.limitations))
    if producer.engine_sha256 is None or consumer.engine_sha256 is None:
        reasons+=('cache:engine-evidence-unavailable',)
    if any(not required<=data.keys() for data in checkpointed((left,right))):
        reasons+=('cache:required-facts-unavailable',)
    if any(data.keys()-(required | {'easysewer:backend-settings'}) for data in checkpointed((left,right))):
        reasons+=('cache:unsupported-facts',)
    if any(data.get(key)!=value for data in checkpointed((left,right)) for key,value in checkpointed((invariants or {}).items())):
        reasons+=('cache:unsupported-condition-semantics',)
    if calendar is not None:
        try:
            windows=[]
            for data in checkpointed((left,right)):
                window=data['swmm:calendar']
                if type(window) is not dict or set(window)!={'start','end'}:raise ValueError('Invalid calendar')
                start,end=(datetime.fromisoformat(window[key]) for key in checkpointed(('start','end')))
                if start.tzinfo is not None or end.tzinfo is not None or end<=start:raise ValueError('Invalid calendar')
                windows.append((start,end))
            differences+=calendar(*windows)
        except (KeyError,TypeError,ValueError):reasons+=('cache:calendar-evidence-unavailable',)
    reasons=tuple(dict.fromkeys(reasons))
    if differences:return 'changed',differences,('cache:production-conditions-changed',)+reasons
    if reasons or not left:return 'unknown',(),reasons or ('cache:conditions-unavailable',)
    return 'matched',(),()


def _rdii_context(snapshot):
    return _swmm_context(snapshot,kind='RDII',policy=RDII_POLICY)


def _hotstart_context(snapshot):
    return _swmm_context(snapshot,kind='HOTSTART',policy=HOTSTART_POLICY)


def _rdii_calendar(producer,consumer):
    differences=[]
    # A later fresh start clears convolution/initial-abstraction history, even
    # inside a fully covered file. Only a shared antecedent start can match.
    if producer[0]!=consumer[0]:differences.append('cache:rdii-antecedent-start')
    if consumer[0]<producer[0] or consumer[1]>producer[1]:differences.append('cache:rdii-time-coverage')
    return tuple(differences)


def _compare_rdii(producer,consumer):
    return _compare_conditions(producer,consumer,required=_REQUIRED | {'swmm:rdii-history'},
        calendar=_rdii_calendar,invariants={'swmm:rdii-history':_RDII_HISTORY})


def _compare_hotstart(producer,consumer):
    return _compare_conditions(producer,consumer,
        required=_REQUIRED | {'swmm:time-origins','swmm:hotstart-state-scope'},
        calendar=lambda p,c:() if p[1]==c[0] else ('cache:hotstart-continuation-time',),
        invariants={'swmm:hotstart-state-scope':_HOTSTART_SCOPE})


def swmm_cache_policies():
    return CachePolicies(policies=(
        CachePolicy(key=RUNOFF_POLICY,kind='RUNOFF',capture=_runoff_context,compare=_compare_runoff),
        CachePolicy(key=RDII_POLICY,kind='RDII',capture=_rdii_context,compare=_compare_rdii),
        CachePolicy(key=HOTSTART_POLICY,kind='HOTSTART',capture=_hotstart_context,compare=_compare_hotstart)))
