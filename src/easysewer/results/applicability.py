"""Execution evidence is separate from the presence/decoding of saved values."""

from dataclasses import dataclass, replace
import json

from ..model.identity import canonical_key, namespace_key
from ..validation._cooperative import checkpointed


@dataclass(frozen=True, kw_only=True)
class ResultApplicability:
    status: str = 'unknown'
    reasons: tuple[str, ...] = ('easysewer:execution-context-unavailable',)
    evidence: tuple[str, ...] = ()

    def __post_init__(self):
        if self.status not in ('computed', 'replayed', 'partial', 'not_computed', 'not_applicable', 'unknown'):
            raise ValueError('Unknown result applicability status')
        for name in checkpointed(('reasons', 'evidence')):
            values = getattr(self, name)
            if type(values) is not tuple or any(type(v) is not str or not v for v in checkpointed(values)):
                raise TypeError('Applicability requires immutable nonempty string codes')
            if len(set(values)) != len(values):raise ValueError('Duplicate applicability code')
        for value in checkpointed(self.reasons):namespace_key(value)
        if self.status not in ('computed', 'replayed') and not self.reasons:
            raise ValueError('Limited/unknown applicability requires a reason')

    @property
    def unavailable(self):
        return self.status in ('not_computed', 'not_applicable')

    def to_data(self):
        return dict(status=self.status, reasons=list(self.reasons), evidence=list(self.evidence))

    @classmethod
    def from_data(cls, data):
        if type(data) is not dict or set(data) != {'status', 'reasons', 'evidence'}:
            raise ValueError('Invalid result applicability')
        if any(type(data[k]) is not list for k in checkpointed(('reasons', 'evidence'))):
            raise TypeError('Applicability JSON requires arrays')
        return cls(status=data['status'], reasons=tuple(data['reasons']), evidence=tuple(data['evidence']))


@dataclass(frozen=True, kw_only=True)
class ResultContext:
    """Versioned facts; unknown policies/facts survive storage without claims.

    Facts describe execution scope, not proof of physical cache compatibility.
    A standalone OUT supplies none. RPT can prove replay through its interface
    marker, but cannot prove every process option or the loaded binary identity.
    """
    policy: str = 'easysewer:unspecified'
    facts: tuple[tuple[str, str], ...] = ()
    evidence: tuple[str, ...] = ()

    def __post_init__(self):
        if type(self.policy) is not str:raise TypeError('Expected an execution policy')
        namespace_key(self.policy.rsplit(':', 1)[0] if self.policy.rsplit(':', 1)[-1].isdigit() else self.policy)
        if type(self.facts) is not tuple or any(type(row) is not tuple or len(row) != 2 for row in checkpointed(self.facts)):
            raise TypeError('Execution facts require immutable key/value pairs')
        for key, value in checkpointed(self.facts):
            namespace_key(key)
            if type(value) is not str or not value:raise TypeError('Execution fact values must be strings')
        if len(dict(self.facts)) != len(self.facts):raise ValueError('Duplicate execution fact')
        ResultApplicability(evidence=self.evidence)
        bound = self.fact('swmm:groundwater-targets')
        if bound is not None:
            values=json.loads(bound)
            if type(values) is not list or any(type(v) is not str for v in checkpointed(values)):
                raise ValueError('Groundwater scope requires an array of object identities')
            keys=[canonical_key(v) for v in checkpointed(values)]
            if len(set(keys)) != len(keys) or keys != values:raise ValueError('Groundwater scope requires unique canonical identities')

    def fact(self, key):
        return dict(self.facts).get(key) if self.policy == 'swmm:result-context:1' else None

    def with_fact(self, key, value):
        return replace(self, facts=tuple((k,v) for k,v in checkpointed(self.facts) if k != key)+((key,value),))

    def assessment(self, status, *reasons):
        return ResultApplicability(status=status, reasons=tuple(reasons), evidence=self.evidence)

    def _process(self, key):
        value = self.fact(key)
        if value == 'inactive':return self.assessment('not_applicable', 'swmm:process-inactive')
        if value in ('computed', 'replayed'):
            if self.fact('swmm:completed') == 'no':
                return self.assessment('partial', 'swmm:simulation-ended-early')
            return self.assessment(value)
        return self.assessment('unknown', 'easysewer:execution-context-unavailable')

    def balance(self, key):
        process = {'runoff':'swmm:runoff', 'flow':'swmm:routing', 'quality':'swmm:routing-quality'}[key]
        if self.fact('swmm:report-disabled') == 'yes':
            finalizes = self.fact('swmm:balance-without-report')
            if finalizes == 'no':
                return self.assessment('not_computed', 'swmm:balance-finalization-disabled')
            if finalizes != 'yes':
                return self.assessment('unknown', 'swmm:balance-finalization-unverified')
        if key == 'runoff' and self.fact('swmm:runoff') == 'replayed':
            return self.assessment('not_computed', 'swmm:runoff-hydrology-not-recomputed')
        return self._process(process)

    def report(self, key):
        hydro = {'swmm:runoff_quantity_continuity', 'swmm:runoff_quality_continuity',
                 'swmm:groundwater_continuity', 'swmm:subcatchment_runoff', 'swmm:groundwater',
                 'swmm:subcatchment_washoff', 'swmm:lid_performance', 'swmm:lid_detail'}
        if key in hydro:
            if self.fact('swmm:runoff') == 'replayed':
                return self.assessment('not_computed', 'swmm:runoff-hydrology-not-recomputed')
            return self._process('swmm:runoff')
        if key == 'swmm:flow_routing_continuity':return self._process('swmm:routing')
        if key == 'swmm:quality_routing_continuity':return self._process('swmm:routing-quality')
        return self.assessment('unknown', 'easysewer:result-scope-unspecified')

    def output(self, collection, variable, *, target=None):
        if collection in ('swmm:nodes', 'swmm:links'):
            known = {'swmm:concentration', 'swmm:depth', 'swmm:volume'} | (
                {'swmm:head', 'swmm:lateral_inflow', 'swmm:inflow', 'swmm:overflow', 'swmm:flooding'}
                if collection == 'swmm:nodes' else {'swmm:flow', 'swmm:velocity', 'swmm:capacity'})
            if variable not in known:return self.assessment('unknown', 'easysewer:result-scope-unspecified')
            return self._process('swmm:routing-quality' if variable == 'swmm:concentration' else 'swmm:routing')
        replay = self.fact('swmm:runoff') == 'replayed'
        if collection == 'swmm:subcatchments':
            if variable not in ('swmm:rainfall','swmm:snow_depth','swmm:evaporation','swmm:infiltration',
                                'swmm:runoff','swmm:groundwater_flow','swmm:groundwater_elevation',
                                'swmm:soil_moisture','swmm:concentration','swmm:losses'):
                return self.assessment('unknown', 'easysewer:result-scope-unspecified')
            if variable in ('swmm:groundwater_flow','swmm:groundwater_elevation','swmm:soil_moisture') and target:
                bound = self.fact('swmm:groundwater-targets')
                if bound is not None and canonical_key(target.key) not in json.loads(bound):
                    return self.assessment('not_applicable', 'swmm:groundwater-binding-absent')
                if not replay:return self._process('swmm:groundwater')
            if variable == 'swmm:rainfall':
                return self._process('swmm:rainfall')
            if replay:
                if self.fact('swmm:runoff-physics') != '1':
                    return self.assessment('unknown', 'swmm:replay-physics-unverified')
                return self.assessment('replayed', 'swmm:sampled-runoff-interface')
            return self._process('swmm:runoff')
        if collection == 'swmm:system':
            if variable in ('swmm:snow_depth', 'swmm:infiltration', 'swmm:runoff'):
                return self.output('swmm:subcatchments', variable)
            if variable == 'swmm:rainfall':return self._process('swmm:rainfall')
            if variable == 'swmm:evaporation' and replay:
                if self.fact('swmm:runoff-physics') != '1':
                    return self.assessment('unknown', 'swmm:replay-physics-unverified')
                if self.fact('swmm:groundwater-targets') == '[]':
                    return self.assessment('replayed', 'swmm:sampled-runoff-interface')
                return self.assessment('partial', 'swmm:groundwater-evaporation-not-stored')
            if variable in ('swmm:dry_weather_inflow', 'swmm:groundwater_inflow', 'swmm:rdii_inflow',
                            'swmm:external_inflow', 'swmm:inflow', 'swmm:flooding', 'swmm:outflow', 'swmm:storage'):
                return self._process('swmm:routing')
        return self.assessment('unknown', 'easysewer:result-scope-unspecified')

    def artifact(self, kind):
        if kind == 'HOTSTART' and self.fact('swmm:runoff') == 'replayed':
            return self.assessment('partial', 'swmm:hydrologic-continuation-state-not-stored')
        if kind == 'HOTSTART' and self.fact('swmm:hotstart') == 'partial-state':
            return self.assessment('partial', 'swmm:hotstart-not-complete-checkpoint')
        return self.assessment('unknown', 'easysewer:result-scope-unspecified')

    def to_data(self):
        return dict(policy=self.policy, facts=[list(row) for row in checkpointed(self.facts)], evidence=list(self.evidence))

    @classmethod
    def from_data(cls, data):
        if type(data) is not dict or set(data) != {'policy','facts','evidence'}:
            raise ValueError('Invalid execution result context')
        if type(data['facts']) is not list or any(type(row) is not list for row in checkpointed(data['facts'])):
            raise TypeError('Expected execution fact arrays')
        if type(data['evidence']) is not list:raise TypeError('Expected execution evidence array')
        return cls(policy=data['policy'], facts=tuple(tuple(row) for row in checkpointed(data['facts'])), evidence=tuple(data['evidence']))


def context_from_model(model, backend, *, input_sha256):
    """Called for a captured, supported input and known native implementation."""
    if 'easysewer:runoff-physics:1' not in backend.capabilities or (model.support is not None and model.support.opaque_records):
        return ResultContext()
    options = model.effective_options.values
    replay = ('RUNOFF','USE') in model.files and bool(model.subcatchments)
    routing = bool(model.nodes) and not options.ignore_routing
    facts = (
        ('swmm:runoff', 'replayed' if replay else 'computed' if model.subcatchments else 'inactive'),
        ('swmm:routing', 'computed' if routing else 'inactive'),
        ('swmm:routing-quality', 'computed' if routing and model.pollutants and not options.ignore_quality else 'inactive'),
        ('swmm:rainfall', 'computed' if model.subcatchments and not options.ignore_rainfall else 'inactive'),
        ('swmm:report-disabled', 'yes' if model.effective_report.settings.disabled else 'no'),
        ('swmm:balance-without-report', 'yes' if backend.abi.endswith(':flexible:201') else 'no'),
        ('swmm:runoff-physics', '1'),
        ('swmm:groundwater', 'computed' if model.groundwater and not options.ignore_groundwater else 'inactive'),
        # Applicability is a set of bound objects, independent of relation row
        # order (which can change when edited source is read in a new worker).
        ('swmm:groundwater-targets', json.dumps(sorted(canonical_key(v.subcatchment.key) for v in checkpointed(model.groundwater.values())))),
        ('swmm:hotstart', 'partial-state'),
    )
    return ResultContext(policy='swmm:result-context:1', facts=facts,
        evidence=('input-sha256:'+input_sha256, 'backend-sha256:'+backend.sha256))


def context_from_input(raw, backend):
    """Conservative session fallback; opaque/unsupported input makes no claims."""
    import hashlib
    from ..io.inp import InpDocument
    from ..model import Model
    from ..validation import ValidationError
    try:
        model = Model.from_document(InpDocument.from_bytes(raw), strict=True)
        return context_from_model(model, backend, input_sha256=hashlib.sha256(raw).hexdigest())
    except (ValueError, TypeError, ValidationError, UnicodeError):
        return ResultContext()


def qualify_table(table, context):
    """Keep source text and cell tokens; hide numerical non-results from APIs."""
    from ..validation._cooperative import checkpointed
    assessment = context.report(table.key)
    if assessment.status == 'unknown':return table
    columns = tuple(replace(c, applicability=assessment) for c in checkpointed(table.columns))
    rows = table.rows
    if assessment.unavailable:
        reason = assessment.reasons[0]
        rows = tuple(replace(row, cells=tuple(
            replace(cell, value=None, missing_reason=reason, qualifier='equal')
            if column.kind == 'number' else cell for column,cell in checkpointed(zip(columns,row.cells)))) for row in checkpointed(rows))
    return replace(table, applicability=assessment, columns=columns, rows=rows)
