"""Versioned FlexiblePonding policy, snapshot binding and pure calculations."""

from dataclasses import dataclass, replace
import math
from pathlib import Path
import platform

from .backend import BackendArtifact, BackendRunPlan
from .native import StandardBackend, _CORRECTION_PREFIXES, _IO_CAPABILITIES
from ..io.json import JsonDocument
from ..model import UnitContext
from ..model.network import Junction, Divider
from ..utils import probe_library_path
from ..validation import Diagnostic, Severity, ValidationError, ValidationReport

POLICY = 'easysewer:flexible-ponding:incremental-volume:2'
EXTENSION = 'easysewer:flexible-ponding'
_LEGACY_HASHES = {
    'Windows': '335836b62ad0595b1a15ed4fef5aa1693901042fbb932ec4c0589258e2cfac1d',
    'Linux': '3e168f1d2ea8ea3e140178d079e7ade1b06430a315f276146934a7281f4f9082',
}
_CHECKPOINT_HASHES = {
    'Windows': '6de04f3e5271a8684d19192019f8441b9a1c8ac8ae5473e712c327338b1eaa73',
    'Linux': '0f09f366e1bd46e84ab92181468f0b5012bbba55d02a9a109ca5711354486ed0',
}
_PATH_HASHES = {
    'Windows': 'b51dee8ac805021a6b035f8c0658563cdac92b22d7eb79fe7fece6f04281987f',
    'Linux': '936b830cf5609b92b853dae08df2aeb46b700efbc74a55a3b2ef74811c58f4c3',
}
_HORTON_HASHES = {'Windows': '2e8ec7aa58f6f8f76e690bf8eb6bc5659bb729a5b6f4af15f772d6bbcb8a8df6', 'Linux': '9f5546e1839d9c3ba35bffec998fc1d0b33ac9d12206dbfc882c316760864638'}
_HORTON_OUTFALL_HASHES = {'Windows': '5eb49c9b096a947740f0156932fe8343583a1d0725ad8f943ccdb65a77437ca3', 'Linux': '120897cecdea48535bde26125506d836ce9294e21f9815c321cdd0a213bf2cc1'}
_HASHES = _HORTON_OUTFALL_HASHES
BUILD_POLICY = POLICY + ':native:14'


def finite(value, name, *, minimum=0):
    if type(value) not in (int,float) or not math.isfinite(value) or value<minimum:
        raise ValueError(f'{name} must be a finite number >= {minimum}')
    return float(value)


@dataclass(frozen=True, kw_only=True)
class FlexiblePondingPolicy:
    """Fixed physical threshold units, independent of model FLOW_UNITS.

    Version 2 removes a fraction of this step's positive above-ground storage
    increment, bounded by available ponded volume and positive overflow volume.
    It never removes preexisting below-ground storage or a stale multi-step sum.
    """
    external_flooding_ratio: float = .5
    depth_threshold_m: float = .3
    flow_threshold_cms: float = .1
    record_steps: bool = True
    policy: str = POLICY

    def __post_init__(self):
        if finite(self.external_flooding_ratio,'external_flooding_ratio')>1:
            raise ValueError('external_flooding_ratio must be between 0 and 1')
        finite(self.depth_threshold_m,'depth_threshold_m')
        finite(self.flow_threshold_cms,'flow_threshold_cms')
        if type(self.record_steps) is not bool:raise TypeError('record_steps must be bool')
        if self.policy!=POLICY:raise ValueError('Unsupported FlexiblePonding policy version')

    def to_json_document(self):
        from dataclasses import asdict
        return JsonDocument.from_data(asdict(self))

    @classmethod
    def from_json_document(cls, document):
        if type(document.data) is not dict:raise TypeError('Policy must be a JSON object')
        return cls(**document.data)

    @classmethod
    def json_schema(cls):
        return {'$schema':'https://json-schema.org/draft/2020-12/schema','type':'object','additionalProperties':False,
            'properties':{'policy':{'const':POLICY},'external_flooding_ratio':{'type':'number','minimum':0,'maximum':1},
                'depth_threshold_m':{'type':'number','minimum':0},'flow_threshold_cms':{'type':'number','minimum':0},
                'record_steps':{'type':'boolean'}}}


@dataclass(frozen=True, kw_only=True)
class PondingAdjustment:
    depth: float
    volume: float
    overflow: float
    external_flow: float
    removed_volume: float


def adjust_ponding(*, previous_depth, previous_volume, depth, volume, overflow,
                   area, dt, ratio, depth_threshold, flow_threshold, flow_per_volume_rate):
    """flow_per_volume_rate maps native V/s to the selected native flow unit."""
    for name,value in (('previous_depth',previous_depth),('previous_volume',previous_volume),
                       ('depth',depth),('volume',volume),('area',area),('dt',dt),
                       ('ratio',ratio),('depth_threshold',depth_threshold),
                       ('flow_threshold',flow_threshold),('flow_per_volume_rate',flow_per_volume_rate)):
        finite(value,name)
    if area<=0 or dt<=0 or flow_per_volume_rate<=0 or ratio>1:
        raise ValueError('Ponding requires positive area/time/unit factor and ratio <= 1')
    if type(overflow) not in (int,float) or not math.isfinite(overflow):
        raise ValueError('overflow must be finite')
    removed=0.
    if depth>depth_threshold and overflow>flow_threshold and depth>previous_depth and volume>previous_volume:
        increment=min(volume-previous_volume,area*(depth-previous_depth),area*depth,volume)
        removed=min(ratio*increment,overflow/flow_per_volume_rate*dt)
    external=removed/dt*flow_per_volume_rate
    if not math.isfinite(external):
        raise ValueError('Ponding adjustment overflowed the supported numeric range')
    return PondingAdjustment(depth=max(0.,depth-removed/area),volume=max(0.,volume-removed),
        overflow=overflow-external,external_flow=external,removed_volume=removed)


@dataclass(frozen=True, kw_only=True)
class FlexiblePondingBackend(StandardBackend):
    key = 'easysewer:flexible-ponding'
    worker_kind = 'flexible-ponding'
    supported_extensions = (EXTENSION,)

    def _selection(self):
        path=self.library or probe_library_path('flexible_ponding')
        expected=self.expected_sha256 or (None if self.library else _HASHES.get(platform.system()))
        return path,expected.lower() if expected else None

    def execution_info(self, info):
        capabilities = tuple(value for value in info.capabilities
            if value not in ('swmm:standard', EXTENSION)
            and not value.startswith(_CORRECTION_PREFIXES)) + (EXTENSION,)
        # ABI 201 identifies accounting entry points; it does not prove that a
        # user-supplied library contains these independently qualified I/O fixes.
        revision = (14 if info.sha256 in _HORTON_OUTFALL_HASHES.values() else
                    13 if info.sha256 in _HORTON_HASHES.values() else
                    12 if info.sha256 in _PATH_HASHES.values() else
                    11 if info.sha256 in _CHECKPOINT_HASHES.values() else
                    10 if info.sha256 in _LEGACY_HASHES.values() else None)
        if revision is not None:
            capabilities += (f'easysewer:native-io-fixes:{revision}',)+_IO_CAPABILITIES
            if revision>=11:capabilities += ('easysewer:checkpoint:2',)
            if revision>=12:capabilities += ('easysewer:path-io:1',)
            if revision>=13:capabilities += ('easysewer:horton-capacity:1',)
            if revision>=14:capabilities += ('easysewer:horton-state:1','easysewer:outfall-gate:1')
        runoff_semantics = (('swmm:runoff-replay', 'easysewer:runoff-physics:1'),
                            ('swmm:runoff-rain-clock', 'easysewer:runoff-rain-clock:1')) if revision is not None else ()
        return replace(info,numerical_policy=f'{POLICY}:native:{revision}' if revision is not None and revision>=13 else POLICY,
            capabilities=capabilities,
            output_semantics=(('swmm:nodes:overflow','easysewer:system-flooding'),
                              ('swmm:system:flooding','easysewer:system-flooding'),
                              ('swmm:statistics','native-after-ponding-adjustment'),
                              ('easysewer:ponding-accounting','discrete-volume:1')) + runoff_semantics +
                ((('swmm:modified-horton','easysewer:horton-state:1'),
                  ('swmm:outfall-gate','easysewer:outfall-gate:1')) if revision is not None and revision>=14 else
                 (('swmm:modified-horton','easysewer:horton-capacity:1'),) if revision is not None and revision>=13 else ()))

    def prepare_run(self, model, config, snapshot, *, artifact_directory):
        settings=config.extensions.data.get(EXTENSION,{}) if config.extensions else {}
        try:policy=FlexiblePondingPolicy.from_json_document(JsonDocument.from_data(settings))
        except (TypeError,ValueError) as error:
            raise ValidationError(ValidationReport(diagnostics=(Diagnostic(code='flexible.policy',message=str(error)),))) from error
        options=model.effective_options.values
        issues=[]
        if not model.nodes or not options.allow_ponding or options.flow_routing!='DYNWAVE' or options.ignore_routing:
            issues.append(Diagnostic(code='flexible.routing',message='FlexiblePonding requires active DYNWAVE routing and ALLOW_PONDING YES'))
        nodes=[]
        for node in model.nodes.values():
            if type(node) in (Junction,Divider) and node.ponded_area is not None and node.ponded_area>0:
                nodes.append(dict(id=node.id,area=node.ponded_area))
        if not nodes:
            issues.append(Diagnostic(code='flexible.no_nodes',severity=Severity.WARNING,message='No nodes have positive ponded area; no external water is removed'))
        issues.append(Diagnostic(code='flexible.discrete_removal',severity=Severity.INFO,
            message='Ponded water is removed after routing as a discrete volume; mixed pollutant concentration is unchanged. Native statistics use the adjusted state.'))
        ValidationReport(diagnostics=tuple(issues)).raise_for_errors()
        units=model.units;rules=model.profile.unit_rules;si=UnitContext(flow_units='CMS')
        volume_factor=UnitContext(flow_units='CFS').convert(1,dimension='volume',to=units,rules=rules)
        flow_factor=UnitContext(flow_units='CFS').convert(1,dimension='flow',to=units,rules=rules)
        area_factor=UnitContext(flow_units='CFS').convert(1,dimension='area',to=units,rules=rules)
        depth_factor=UnitContext(flow_units='CFS').convert(1,dimension='depth',to=units,rules=rules)
        for node in nodes:
            node['volume_per_depth']=node['area']*volume_factor/(area_factor*depth_factor)
        trace=(Path(artifact_directory)/'flexible-ponding.jsonl').as_posix() if policy.record_steps else None
        parameters=JsonDocument.from_data(dict(policy=policy.to_json_document().data,input_sha256=snapshot.input_sha256,
            flow_units=units.flow_units,nodes=nodes,trace=trace,
            depth_threshold=si.convert(policy.depth_threshold_m,dimension='depth',to=units,rules=rules),
            flow_threshold=si.convert(policy.flow_threshold_cms,dimension='flow',to=units,rules=rules),
            flow_per_volume_rate=flow_factor/volume_factor,
            volume_to_liters=28.317/volume_factor,quality_enabled=not options.ignore_quality,
            pollutants=[dict(id=row.id,units=row.units) for row in model.pollutants.values()],
            depth_unit=units.unit('depth'),volume_unit=units.unit('volume')))
        return BackendRunPlan(parameters=parameters,diagnostics=tuple(issues),
            artifacts=(BackendArtifact(role='easysewer:flexible-ponding-steps',relative_path=trace),) if trace else ())
