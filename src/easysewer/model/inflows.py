"""Node/constituent relations for external and dry-weather inflows."""

from dataclasses import dataclass, replace
from enum import Enum
from typing import Literal

from ..validation import Diagnostic, DiagnosticSubject, Severity, ValidationReport
from .fields import number, reference, validate_fields
from .identity import Ref
from .store import CollectionSpec
from .usage import ResourceUse
from .units import UnitTransform
from .pollutant_units import PollutantResourceConversion, PollutantUnitConversion, PollutantUnitTransform


class FlowConstituent(Enum):
    FLOW = "FLOW"


FLOW = FlowConstituent.FLOW


def constituent_key(value):
    """The reserved flow marker and pollutant references have distinct keys."""
    if value is FLOW:
        return "FLOW"
    if isinstance(value, Ref) and value.collection == "swmm:pollutants" and isinstance(value.key, str):
        return "POLLUTANT:" + value.key
    raise ValueError("Constituent must be FLOW or a reference to swmm:pollutants")


def inflow_key(value):
    if not isinstance(value.node, Ref) or not isinstance(value.node.key, str):
        raise ValueError("An inflow requires a node reference with a scalar ID")
    return value.node.key, constituent_key(value.constituent)


@dataclass(frozen=True, kw_only=True)
class ExternalInflow:
    """Extension base: each constituent supplies its own value/unit variant."""
    node: Ref = reference("swmm:nodes")
    constituent: FlowConstituent | Ref


@dataclass(frozen=True, kw_only=True)
class FlowInflow(ExternalInflow):
    constituent: Literal[FLOW] = FLOW
    series: Ref | None = reference("swmm:timeseries", None)
    scale_factor: float | None = number("ratio", None)
    baseline: float | None = number("flow", None)
    pattern: Ref | None = reference("swmm:patterns", None)


@dataclass(frozen=True, kw_only=True)
class ConcentrationInflow(ExternalInflow):
    constituent: Ref = reference('swmm:pollutants')
    series: Ref | None = reference('swmm:timeseries', None)
    scale_factor: float | None = number('ratio', None)
    baseline: float | None = number('concentration', None)
    pattern: Ref | None = reference('swmm:patterns', None)


@dataclass(frozen=True, kw_only=True)
class MassInflow(ConcentrationInflow):
    baseline: float | None = number('external_mass_rate', None)
    mass_factor: float | None = number('ratio', None, positive=True)


@dataclass(frozen=True, kw_only=True)
class DryWeatherInflow:
    """Extension base for independently keyed flow/concentration relations."""
    node: Ref = reference("swmm:nodes")
    constituent: FlowConstituent | Ref


@dataclass(frozen=True, kw_only=True)
class DryWeatherFlow(DryWeatherInflow):
    constituent: Literal[FLOW] = FLOW
    baseline: float = number("flow")
    # Preserve optional empty slots and user order. Native sorts by pattern
    # kind, with the last occurrence of each kind taking effect.
    patterns: tuple[Ref | None, ...] = ()

    def validate_local(self):
        if len(self.patterns) > 4:
            yield Diagnostic(code="inflow.pattern_capacity", message="DWF accepts at most four pattern slots", field="patterns")
        for index, pattern in enumerate(self.patterns):
            if pattern is not None and pattern.collection != "swmm:patterns":
                yield Diagnostic(code="inflow.pattern_namespace", message="DWF patterns must reference swmm:patterns", field=f"patterns[{index}]")


@dataclass(frozen=True, kw_only=True)
class DryWeatherConcentration(DryWeatherInflow):
    constituent: Ref = reference('swmm:pollutants')
    baseline: float = number('concentration')
    patterns: tuple[Ref | None, ...] = ()

    def validate_local(self):
        yield from DryWeatherFlow.validate_local(self)


INFLOW_TRANSFORMS = tuple(UnitTransform(value_type=kind, convert=lambda row,context: row)
    for kind in (ConcentrationInflow, MassInflow, DryWeatherConcentration))


def _convert_concentration(row, context):
    from .resources import InlineTimeSeries
    if row.constituent.canonical != context.target.canonical:
        return PollutantUnitConversion(value=row)
    if type(row) is MassInflow:
        return PollutantUnitConversion(value=replace(row,
            mass_factor=(row.mass_factor if row.mass_factor is not None else 1.)*context.factor))
    updated = replace(row, baseline=row.baseline*context.factor if row.baseline is not None else None)
    resources = ()
    if type(row) is ConcentrationInflow and row.series is not None:
        series = context.record(row.series)
        if type(series) is not InlineTimeSeries:
            raise ValueError('External concentration data needs explicit document conversion/materialization before model conversion')
        converted = replace(series, points=tuple(replace(p, value=p.value*context.factor) for p in series.points))
        resources = (PollutantResourceConversion(target=row.series, path=('series',), value=converted),)
    return PollutantUnitConversion(value=updated, resources=resources)


INFLOW_POLLUTANT_TRANSFORMS = tuple(PollutantUnitTransform(value_type=kind, convert=_convert_concentration)
    for kind in (ConcentrationInflow, MassInflow, DryWeatherConcentration))


INFLOW_COLLECTIONS = (
    CollectionSpec(key="swmm:inflows", record_type=ExternalInflow, key_of=inflow_key, validate=validate_fields),
    CollectionSpec(key="swmm:dwf", record_type=DryWeatherInflow, key_of=inflow_key, validate=validate_fields),
)


def inflow_resource_uses(store):
    for namespace, value_types in (("swmm:inflows", (FlowInflow,ConcentrationInflow,MassInflow)), ("swmm:dwf", (DryWeatherFlow,DryWeatherConcentration))):
        for key, row in store.collection(namespace).items():
            if type(row) not in value_types or not ValidationReport(diagnostics=tuple(validate_fields(row))).is_valid:
                continue
            owner = Ref(collection=namespace, key=key)
            if namespace == 'swmm:inflows':
                if row.series is not None:
                    dimension='flow' if type(row) is FlowInflow else 'external_mass_rate' if type(row) is MassInflow else None
                    if dimension is None and store.contains(row.constituent):
                        from .quality import concentration_dimension
                        pollutant=store.collection('swmm:pollutants')[row.constituent.key]
                        if ValidationReport(diagnostics=tuple(validate_fields(pollutant))).is_valid:
                            dimension=concentration_dimension(pollutant)
                    if dimension:
                        yield ResourceUse(owner=owner, target=row.series, path=("series",), role="external inflow", dimensions=(dimension,))
                patterns = ((row.pattern, ("pattern",)),)
            else:
                patterns = ((pattern, ("patterns", index)) for index, pattern in enumerate(row.patterns))
            for pattern, path in patterns:
                if pattern is not None:
                    yield ResourceUse(owner=owner, target=pattern, path=path, role="inflow baseline pattern",
                                      accepted_kinds=("MONTHLY", "DAILY", "HOURLY", "WEEKEND"))


def validate_inflows(store):
    for key, row in store.collection("swmm:dwf").items():
        if type(row) not in (DryWeatherFlow,DryWeatherConcentration) or not ValidationReport(diagnostics=tuple(validate_fields(row))).is_valid:
            continue
        seen = {}
        for index, ref in enumerate(row.patterns):
            if ref is None or not store.contains(ref):
                continue
            pattern = store.collection(ref.collection)[ref.key]
            if pattern.kind in seen:
                yield Diagnostic(code="inflow.repeated_pattern_kind", severity=Severity.WARNING,
                    message="The last DWF pattern of each kind takes effect; same-kind factors are not multiplied",
                    object_id=str(key), field=f"patterns[{index}]",
                    subject=DiagnosticSubject(collection='swmm:dwf', key=key, path=('patterns', index)),
                    related=(DiagnosticSubject(collection='swmm:dwf', key=key, path=('patterns',)),
                             DiagnosticSubject(collection=ref.collection, key=ref.key, path=('kind',)),
                             DiagnosticSubject(collection=seen[pattern.kind].collection,
                                               key=seen[pattern.kind].key, path=('kind',))))
            seen[pattern.kind] = ref


def validate_inflows_run(store):
    from .network import Outfall
    for row in store.collection('swmm:inflows').values():
        if type(row) is not ConcentrationInflow or not ValidationReport(diagnostics=tuple(validate_fields(row))).is_valid:
            continue
        if store.contains(row.node) and not isinstance(store.collection('swmm:nodes')[row.node.key],Outfall):
            if (row.node.key,'FLOW') not in store.collection('swmm:inflows'):
                yield Diagnostic(code='inflow.concentration_needs_flow', object_id=row.node.key,
                    subject=DiagnosticSubject(collection='swmm:inflows', key=inflow_key(row)),
                    related=(DiagnosticSubject(collection=row.node.collection, key=row.node.key),
                             DiagnosticSubject(collection='swmm:inflows', key=(row.node.key,'FLOW'))),
                    message='A concentration inflow at a non-outfall needs external FLOW at the same node; DWF does not supply that external flow')
