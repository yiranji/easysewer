"""Modern and legacy HYDROGRAPHS, and derived nodal RDII relations."""

from collections import OrderedDict

from ...model.rdii import UnitHydrograph, HydrographResponse, RdiiInflow, RDII_COLLECTIONS, MONTHS, RESPONSES, validate_rdii
from ...model.fields import validate_fields
from ...model.identity import Ref, canonical_key
from ...schema import DecodedFeature, FeatureDescriptor
from ...schema.structured import EncodedRow, FeatureData, RecordEntry, SourceBinding
from ...validation import Diagnostic, Severity, SourceSpan, ValidationReport
from .formatting import optional_tail
from .geometry import finite_number, number_text
from .field_sources import FieldSources
from .rdii_sources import rdii_sources
from ...schema.rdii_fields import RDII_FIELD_RULES
from ...validation._cooperative import checkpointed


def _key(id, suffix):
    return 'HYDROGRAPHS', canonical_key(id), str(suffix)


class RdiiCodec:
    descriptor = FeatureDescriptor(key='swmm:rdii', sections=frozenset({'HYDROGRAPHS', 'RDII'}),
        requires=('swmm:hydrology', 'swmm:network'), atomic_write=True, ordered_sections=frozenset({'HYDROGRAPHS'}))
    collections = RDII_COLLECTIONS
    field_rules = RDII_FIELD_RULES

    def decode(self, document, profile):
        records, bindings, issues = [], [], []
        source_fields = FieldSources()
        lexical = {d.span.line for d in checkpointed(document.report.errors) if d.span}
        def issue(line, code, message, severity=Severity.ERROR):
            issues.append(Diagnostic(code=code, message=message, severity=severity, section=line.section,
                object_id=line.values[0] if line.values else None,
                span=SourceSpan(source=document.source, line=line.number, column=1, end_column=len(line.content)+1)))
        for section in checkpointed(('HYDROGRAPHS', 'RDII')):
            groups = OrderedDict()
            for line in checkpointed(document.records(section)):
                if line.values:
                    groups.setdefault(canonical_key(line.values[0]), []).append(line)
            for lines in checkpointed(groups.values()):
                if any(line.number in lexical for line in checkpointed(lines)):
                    continue
                responses, gage, prior_gages, source_bindings, inflow = [], None, [], [], None
                try:
                    for line in checkpointed(lines):
                        values = line.values
                        if section == 'RDII':
                            if len(values) < 3:
                                raise ValueError('RDII needs node, hydrograph group and sewer area')
                            inflow = RdiiInflow(node=Ref(collection='swmm:nodes', key=values[0]),
                                hydrograph=Ref(collection='swmm:hydrographs', key=values[1]), sewer_area=finite_number(values[2]))
                            ValidationReport(diagnostics=tuple(validate_fields(inflow))).raise_for_errors()
                            source_bindings.append(SourceBinding(line=line.number, key=('RDII', canonical_key(values[0]))))
                            if len(values) > 3:
                                issue(line, 'rdii.ignored_columns', 'Native RDII ignores trailing fields; normalization removes them', Severity.WARNING)
                            continue
                        if len(values) == 2:
                            if gage is not None:
                                prior_gages.append(gage)
                                issue(line, 'rdii.replaced_gage', 'The last gage supplies RDII; prior assignments still activate those gages and are retained', Severity.INFO)
                            gage = Ref(collection='swmm:raingages', key=values[1])
                            source_bindings.append(SourceBinding(line=line.number, key=_key(values[0], f'GAGE-{len(prior_gages)}')))
                            continue
                        if len(values) < 6:
                            raise ValueError('A hydrograph response needs month, response type and R/T/K')
                        token = values[1].upper()
                        month = token[:3] if token[:3] in MONTHS else 'ALL' if token.startswith('ALL') else None
                        if month is None:
                            raise ValueError('Unknown hydrograph month')
                        kind = next((k for k in checkpointed(RESPONSES) if values[2].upper().startswith(k)), None)
                        start = len(responses)
                        if kind is None:
                            if len(values) < 11:
                                raise ValueError('Legacy hydrographs require three R/T/K triples')
                            parameters = tuple(finite_number(v) for v in checkpointed(values[2:11]))
                            ia = tuple(finite_number(v) for v in checkpointed(values[11:14]))
                            kinds = RESPONSES
                            issue(line, 'rdii.legacy_syntax', 'Legacy triples expand to three ordered modern responses on normalization', Severity.INFO)
                            if len(values) > 14:
                                issue(line, 'rdii.ignored_columns', 'Native ignores extra legacy fields', Severity.WARNING)
                        else:
                            parameters = tuple(finite_number(v) for v in checkpointed(values[3:6]))
                            ia = tuple(finite_number(v) for v in checkpointed(values[6:9]))
                            kinds = (kind,)
                            if len(values) > 9:
                                issue(line, 'rdii.ignored_columns', 'Native ignores extra hydrograph fields', Severity.WARNING)
                        for index, response in checkpointed(enumerate(kinds)):
                            params = parameters[3*index:3*index+3]
                            row = HydrographResponse(month=month, response=response, fraction=params[0], time_to_peak=params[1], recession_ratio=params[2],
                                **dict(zip(('maximum_abstraction', 'recovery_rate', 'initial_abstraction'), ia)))
                            ValidationReport(diagnostics=tuple(validate_fields(row))).raise_for_errors()
                            responses.append(row)
                        source_bindings.append(SourceBinding(line=line.number, key=_key(values[0], start)))
                        if token != month or kind is not None and values[2].upper() != kind:
                            issue(line, 'rdii.native_keyword', 'Native month/type prefix is canonicalized on normalization', Severity.INFO)
                    if section == 'RDII':
                        if len(lines) > 1:
                            issue(lines[-1], 'rdii.replaced_inflow', 'The last nodal RDII assignment takes effect', Severity.INFO)
                        records.append(RecordEntry(collection='swmm:rdii', value=inflow))
                    else:
                        row = UnitHydrograph(id=lines[0].values[0], rain_gage=gage, prior_rain_gages=tuple(prior_gages), responses=tuple(responses))
                        ValidationReport(diagnostics=tuple(validate_fields(row))).raise_for_errors()
                        records.append(RecordEntry(collection='swmm:hydrographs', value=row))
                    entry = records[-1]
                    owner = Ref(collection=entry.collection, key=entry.value.id if section == 'HYDROGRAPHS' else entry.value.node.key)
                    rdii_sources(source_fields, owner, entry.value, lines)
                    bindings.extend(source_bindings)
                except (ValueError, TypeError, OverflowError) as error:
                    issue(line, 'rdii.invalid_input', str(error))
        originals = {Ref(collection=entry.collection, key=entry.value.id if entry.collection == 'swmm:hydrographs'
                         else entry.value.node.key).canonical: entry.value for entry in checkpointed(records)}
        return DecodedFeature(value=FeatureData(records=tuple(records), bindings=tuple(bindings), **source_fields.finish(originals)),
            claimed_lines=frozenset(b.line for b in checkpointed(bindings)), report=ValidationReport(diagnostics=tuple(issues)))

    def encode(self, store, profile):
        for row in checkpointed(store.collection('swmm:hydrographs').values()):
            owner = Ref(collection='swmm:hydrographs', key=row.id)
            if row.rain_gage is not None:
                for index, gage in checkpointed(enumerate((*row.prior_rain_gages, row.rain_gage))):
                    yield EncodedRow(key=_key(row.id, f'GAGE-{index}'), section='HYDROGRAPHS', values=(row.id, gage.key), owners=(owner,))
            for index, response in checkpointed(enumerate(row.responses)):
                tail = optional_tail(tuple(number_text(v) if v is not None else None for v in checkpointed((
                    response.maximum_abstraction, response.recovery_rate, response.initial_abstraction))), ('0', '0', '0'))
                yield EncodedRow(key=_key(row.id, index), section='HYDROGRAPHS', values=(row.id, response.month, response.response,
                    number_text(response.fraction), number_text(response.time_to_peak), number_text(response.recession_ratio), *tail), owners=(owner,))
        for row in checkpointed(store.collection('swmm:rdii').values()):
            yield EncodedRow(key=('RDII', canonical_key(row.node.key)), section='RDII',
                values=(row.node.key, row.hydrograph.key, number_text(row.sewer_area)), owners=(Ref(collection='swmm:rdii', key=row.node.key),))

    def validate(self, store, profile):
        yield from validate_rdii(store, profile)

    def validate_run(self, store, profile):
        yield from validate_rdii(store, profile, for_run=True)

    def validate_document(self, document, profile):
        names = {section: {canonical_key(line.values[0]) for line in document.records(section) if line.values}
                 for section in ('RAINGAGES', 'HYDROGRAPHS')}
        nodes = {canonical_key(line.values[0]) for section in ('JUNCTIONS','OUTFALLS','STORAGE','DIVIDERS')
                 for line in document.records(section) if line.values}
        for line in document.lines:
            values = line.values
            targets = []
            if line.section == 'HYDROGRAPHS' and len(values) == 2:
                targets.append((values[1], names['RAINGAGES']))
            elif line.section == 'RDII' and len(values) >= 2:
                targets.extend(((values[0], nodes), (values[1], names['HYDROGRAPHS'])))
            for name, present in targets:
                if canonical_key(name) not in present:
                    yield Diagnostic(code='rdii.source_reference', message=f'Native parses an unresolved source reference: {name}', section=line.section,
                        span=SourceSpan(source=document.source, line=line.number, column=1, end_column=len(line.content)+1))
