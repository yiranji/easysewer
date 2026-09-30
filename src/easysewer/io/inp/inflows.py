"""FLOW, pollutant concentration and mass inflows with distinct typed values."""

from collections import OrderedDict

from ...model.inflows import (FlowInflow, DryWeatherFlow, ConcentrationInflow, MassInflow, DryWeatherConcentration,
    INFLOW_COLLECTIONS, INFLOW_TRANSFORMS, INFLOW_POLLUTANT_TRANSFORMS,
    inflow_key, inflow_resource_uses, validate_inflows, validate_inflows_run)
from ...model.fields import validate_fields
from ...model.identity import Ref, canonical_key
from ...schema import DecodedFeature, FeatureDescriptor
from ...schema.structured import EncodedRow, FeatureData, RecordEntry, SourceBinding
from ...validation import Diagnostic, Severity, SourceSpan, ValidationReport
from .formatting import optional_tail
from .geometry import finite_number, number_text
from .field_sources import FieldSources
from .inflow_sources import inflow_sources
from ...schema.inflow_fields import INFLOW_FIELD_RULES
from ...validation._cooperative import checkpointed


def _key(section, row):
    return section, *canonical_key(inflow_key(row))


class InflowsCodec:
    descriptor = FeatureDescriptor(key="swmm:inflows", sections=frozenset({"INFLOWS", "DWF"}))
    collections = INFLOW_COLLECTIONS
    unit_transforms = INFLOW_TRANSFORMS
    pollutant_unit_transforms = INFLOW_POLLUTANT_TRANSFORMS
    field_rules = INFLOW_FIELD_RULES

    def decode(self, document, profile):
        groups, records, bindings, issues = OrderedDict(), [], [], []
        source_fields = FieldSources()
        lexical = {item.span.line for item in checkpointed(document.report.errors) if item.span}
        pollutants={canonical_key(line.values[0]) for line in checkpointed(document.records('POLLUTANTS')) if line.values}

        def issue(line, code, message, severity=Severity.ERROR):
            issues.append(Diagnostic(code=code, message=message, severity=severity, section=line.section,
                object_id=line.values[0], span=SourceSpan(source=document.source, line=line.number, column=1, end_column=len(line.content)+1)))

        for line in checkpointed(document.lines):
            if line.kind != "data" or line.section not in self.descriptor.sections:
                continue
            if len(line.values) < 2:
                issue(line, "inflow.invalid_input", "Inflow requires a node and constituent")
                continue
            node,constituent=canonical_key(line.values[:2])
            if constituent not in pollutants and constituent.startswith('FLOW'):
                if constituent!='FLOW':
                    issue(line,'inflow.native_keyword','Native recognizes this undefined constituent name as a FLOW prefix',Severity.INFO)
                constituent='FLOW'
            groups.setdefault((line.section,node,constituent), []).append(line)
        for (section, _, constituent), lines in checkpointed(groups.items()):
            if any(line.number in lexical for line in checkpointed(lines)):
                continue
            row = None
            try:
                for line in checkpointed(lines):
                    values = line.values
                    if section == "INFLOWS":
                        if len(values)<3:
                            raise ValueError("External inflow requires at least three fields")
                        if len(values)>8:
                            issue(line,'inflow.ignored_columns','Native ignores inflow fields after the eighth column',Severity.WARNING)
                        args = dict(node=Ref(collection="swmm:nodes", key=values[0]),
                            series=Ref(collection="swmm:timeseries", key=values[2]) if values[2] else None,
                            scale_factor=finite_number(values[5]) if len(values) > 5 else None,
                            baseline=finite_number(values[6]) if len(values) > 6 else None,
                            pattern=Ref(collection="swmm:patterns", key=values[7]) if len(values) > 7 else None)
                        if constituent == 'FLOW': row=FlowInflow(**args)
                        else:
                            args['constituent']=Ref(collection='swmm:pollutants',key=values[1])
                            token=values[3].upper() if len(values)>3 else 'CONCEN'
                            if token.startswith('MASS'):
                                row=MassInflow(**args,mass_factor=finite_number(values[4]) if len(values)>4 else None)
                                if token!='MASS': issue(line,'inflow.native_keyword','Native MASS prefix is normalized',Severity.INFO)
                            elif token.startswith('CONCEN'):
                                row=ConcentrationInflow(**args)
                                if token!='CONCEN': issue(line,'inflow.native_keyword','Native CONCEN prefix is normalized',Severity.INFO)
                                if len(values)>4 and values[4] not in ('1','1.0'):
                                    issue(line,'inflow.ignored_mass_factor','Native ignores the mass-factor slot of concentration inflows',Severity.WARNING)
                            else: raise ValueError('Pollutant inflow type must be CONCEN or MASS')
                        if constituent == 'FLOW' and ((len(values) > 3 and values[3].upper() != "FLOW") or
                                (len(values) > 4 and values[4] not in ("1", "1.0"))):
                            issue(line, "inflow.ignored_flow_slots", "Native ignores FLOW type and mass-conversion slots; normalization writes FLOW and 1", Severity.WARNING)
                    else:
                        if len(values)<3:
                            raise ValueError("DWF requires at least three fields")
                        if len(values)>7:
                            issue(line,'inflow.ignored_columns','Native ignores DWF fields after the fourth pattern',Severity.WARNING)
                        args=dict(node=Ref(collection="swmm:nodes", key=values[0]), baseline=finite_number(values[2]),
                            patterns=tuple(Ref(collection="swmm:patterns", key=value) if value else None for value in checkpointed(values[3:7])))
                        row=DryWeatherFlow(**args) if constituent=='FLOW' else DryWeatherConcentration(**args,constituent=Ref(collection='swmm:pollutants',key=values[1]))
                    ValidationReport(diagnostics=tuple(validate_fields(row))).raise_for_errors()
                if len(lines) > 1:
                    issue(lines[-1], "inflow.repeated_assignment", "The final inflow assignment for this node and constituent takes effect", Severity.INFO)
                records.append(RecordEntry(collection="swmm:inflows" if section == "INFLOWS" else "swmm:dwf", value=row))
                inflow_sources(source_fields, Ref(collection=records[-1].collection, key=inflow_key(row)), row, lines)
                bindings.extend(SourceBinding(line=line.number, key=_key(section, row)) for line in checkpointed(lines))
            except (ValueError, TypeError, OverflowError) as error:
                issue(line, "inflow.invalid_input", str(error))
        originals = {Ref(collection=r.collection, key=inflow_key(r.value)).canonical: r.value for r in checkpointed(records)}
        return DecodedFeature(value=FeatureData(records=tuple(records), bindings=tuple(bindings), **source_fields.finish(originals)),
            claimed_lines=frozenset(binding.line for binding in checkpointed(bindings)), report=ValidationReport(diagnostics=tuple(issues)))

    def encode(self, store, profile):
        for section, namespace, value_types in checkpointed((("INFLOWS", "swmm:inflows", (FlowInflow,ConcentrationInflow,MassInflow)), ("DWF", "swmm:dwf", (DryWeatherFlow,DryWeatherConcentration)))):
            for key, row in checkpointed(store.collection(namespace).items()):
                if type(row) not in value_types:
                    raise TypeError(f"No {section} writer for {type(row).__name__}")
                flow=type(row) in (FlowInflow,DryWeatherFlow)
                constituent='FLOW' if flow else row.constituent.key
                if section == "INFLOWS":
                    kind='FLOW' if flow else 'MASS' if type(row) is MassInflow else 'CONCEN'
                    tail = optional_tail((kind if kind=='MASS' else None, number_text(row.mass_factor) if type(row) is MassInflow and row.mass_factor is not None else None,
                        number_text(row.scale_factor) if row.scale_factor is not None else None,
                        number_text(row.baseline) if row.baseline is not None else None,
                        row.pattern.key if row.pattern else None), (kind, "1", "1", "0", ""))
                    values = row.node.key, constituent, row.series.key if row.series else "", *tail
                else:
                    values = row.node.key, constituent, number_text(row.baseline), *(pattern.key if pattern else "" for pattern in checkpointed(row.patterns))
                yield EncodedRow(key=_key(section, row), section=section, values=values, owners=(Ref(collection=namespace, key=key),))

    def validate(self, store, profile):
        yield from validate_inflows(store)

    def resource_uses(self, store, profile):
        yield from inflow_resource_uses(store)

    def validate_run(self, store, profile):
        yield from validate_inflows_run(store)

    def validate_document(self, document, profile):
        # Earlier assignments still pass through native parsing even when a
        # later row replaces their values. Validate their references as well.
        names = {section: {canonical_key(line.values[0]) for line in document.records(section)}
                 for section in ("TIMESERIES", "PATTERNS", "POLLUTANTS")}
        used_gages = {canonical_key(line.values[1]) for line in document.records("SUBCATCHMENTS") if len(line.values) >= 2}
        used_gages.update(canonical_key(line.values[1]) for line in document.records("HYDROGRAPHS") if len(line.values) == 2)
        rainfall_series = {canonical_key(line.values[5]) for line in document.records("RAINGAGES")
            if len(line.values) >= 6 and line.values[4].upper() == "TIMESERIES" and canonical_key(line.values[0]) in used_gages}
        for line in document.records("INFLOWS"):
            if len(line.values) >= 3 and line.values[2] and canonical_key(line.values[2]) in rainfall_series:
                # Even an overridden or pollutant assignment leaves a native
                # refersTo flag on its time series. Effective graph refs alone
                # cannot detect this source-level conflict.
                yield Diagnostic(code="inflow.native_rainfall_series", section="INFLOWS", object_id=line.values[0],
                    message="An inflow assignment marks an active rain series as non-rainfall, even when overridden; normalize obsolete assignments or use separate data",
                    span=SourceSpan(source=document.source, line=line.number, column=1, end_column=len(line.content)+1))
        for section in ("INFLOWS", "DWF"):
            for line in document.records(section):
                values = line.values
                if len(values) < 3:
                    continue
                span = SourceSpan(source=document.source, line=line.number, column=1, end_column=len(line.content)+1)
                if "FLOW" in names["POLLUTANTS"]:
                    yield Diagnostic(code="inflow.reserved_constituent", message="FLOW is reserved and cannot also name a pollutant", section=section, span=span)
                if section == "DWF":
                    refs = tuple(("PATTERNS", value) for value in values[3:7])
                else:
                    refs = (("TIMESERIES", values[2]),)
                    if len(values) > 7:
                        refs += (("PATTERNS", values[7]),)
                for target, identity in refs:
                    if identity and canonical_key(identity) not in names[target]:
                        yield Diagnostic(code="inflow.native_source_reference", message=f"Native still parses missing {target} reference {identity!r}; normalize overridden assignments explicitly",
                                         section=section, object_id=values[0], span=span)
