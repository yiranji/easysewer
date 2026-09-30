"""TREATMENT expression parsing, source ownership and native preflight."""

from collections import OrderedDict
import re

from ...model import treatment as t
from ...model.fields import validate_fields
from ...model.identity import Ref, canonical_key
from ...schema import DecodedFeature, FeatureDescriptor
from ...schema.structured import EncodedRow, FeatureData, RecordEntry, SourceBinding
from ...validation import Diagnostic, Severity, SourceSpan, ValidationReport
from .expressions import ExpressionCodec
from .field_sources import FieldSources
from .treatment_sources import treatment_sources
from ...schema.treatment_fields import TREATMENT_FIELD_RULES
from ...validation._cooperative import checkpointed


class TreatmentExpressionCodec(ExpressionCodec):
    def variable(self, resolved):
        return resolved

    def format_variable(self, node):
        return t.variable_text(node)

    def parse(self, text, resolve=None, *, pollutants=()):
        names = {canonical_key(name) for name in pollutants}
        return super().parse(text, resolve or (lambda token:t.resolve_variable(token, names)))


def _parts(line):
    if len(line.values) < 3:
        raise ValueError('TREATMENT requires node, pollutant and C/R = expression')
    text = ' '.join(line.values[2:])
    head, separator, expression = text.partition('=')
    if not head or head[0].upper() not in ('C','R') or not separator:
        raise ValueError('TREATMENT result must begin with C/R and include an equals sign')
    return head[0].upper(), head, expression


class TreatmentCodec:
    descriptor = FeatureDescriptor(key='swmm:treatment', sections=frozenset({'TREATMENT'}),
        requires=('swmm:network','swmm:quality'))
    collections = (t.TREATMENT_COLLECTION,)
    field_rules = TREATMENT_FIELD_RULES
    unit_transforms = t.TREATMENT_TRANSFORMS
    pollutant_unit_transforms = t.TREATMENT_POLLUTANT_TRANSFORMS

    def __init__(self):
        self.expressions = TreatmentExpressionCodec()

    def decode(self, document, profile):
        groups, records, bindings, issues = OrderedDict(), [], [], []
        source_fields = FieldSources()
        lexical = {d.span.line for d in checkpointed(document.report.errors) if d.span}
        pollutants = {canonical_key(line.values[0]) for line in checkpointed(document.records('POLLUTANTS'))}
        def issue(line, message, severity=Severity.ERROR, code='treatment.invalid_input'):
            issues.append(Diagnostic(code=code, message=message, severity=severity, section='TREATMENT',
                object_id=line.values[0] if line.values else None,
                span=SourceSpan(source=document.source, line=line.number, column=1, end_column=len(line.content)+1)))
        for line in checkpointed(document.records('TREATMENT')):
            if len(line.values) < 2:
                issue(line, 'TREATMENT requires node and pollutant')
                continue
            key = canonical_key(tuple(line.values[:2]))
            groups.setdefault(key, []).append(line)
        for key, lines in checkpointed(groups.items()):
            if any(line.number in lexical for line in checkpointed(lines)):
                continue
            try:
                entries = []
                for line in checkpointed(lines):
                    kind, head, expression = _parts(line)
                    def variable(token):
                        value = t.resolve_variable(token, pollutants)
                        if type(value) is t.TreatmentProcessVariable and token.upper() != value.name:
                            issue(line, f'Native process prefix {token!r} resolves to {value.name}', Severity.INFO, 'treatment.native_variable')
                        return value
                    if type(self.expressions).parse is TreatmentExpressionCodec.parse:
                        parsed, spans = self.expressions._parse_with_spans(expression, variable)
                    else:
                        parsed, spans = self.expressions.parse(expression, variable), None
                    row = t.Treatment(node=Ref(collection='swmm:nodes', key=line.values[0]),
                        pollutant=Ref(collection='swmm:pollutants', key=line.values[1]), kind=kind,
                        expression=parsed)
                    ValidationReport(diagnostics=tuple(validate_fields(row))).raise_for_errors()
                    entries.append((line, row, head, spans))
                    if head.strip().upper() != kind:
                        issue(line, 'Native uses the first result character; normalization writes C/R', Severity.INFO, 'treatment.native_keyword')
                if len(lines) > 1:
                    issue(line, 'The final treatment assignment takes effect', Severity.INFO, 'treatment.repeated_assignment')
                records.append(RecordEntry(collection='swmm:treatment', value=row))
                bindings.extend(SourceBinding(line=line.number, key=key) for line in checkpointed(lines))
                treatment_sources(source_fields, row, entries)
            except (ValueError, TypeError, OverflowError) as error:
                issue(line, str(error))
        originals = {Ref(collection='swmm:treatment', key=(entry.value.node.key, entry.value.pollutant.key)).canonical: entry.value for entry in checkpointed(records)}
        return DecodedFeature(value=FeatureData(records=tuple(records), bindings=tuple(bindings), **source_fields.finish(originals)),
            claimed_lines=frozenset(b.line for b in checkpointed(bindings)), report=ValidationReport(diagnostics=tuple(issues)))

    def encode(self, store, profile):
        for key, row in checkpointed(store.collection('swmm:treatment').items()):
            yield EncodedRow(key=canonical_key(key), section='TREATMENT',
                values=(row.node.key, row.pollutant.key, row.kind, '=', self.expressions.format(row.expression)),
                owners=(Ref(collection='swmm:treatment', key=key),))

    def validate(self, store, profile):
        yield from t.validate_treatment(store)

    def validate_document(self, document, profile):
        pollutants = {canonical_key(line.values[0]) for line in document.records('POLLUTANTS')}
        nodes = {canonical_key(line.values[0]) for section in ('JUNCTIONS','OUTFALLS','STORAGE','DIVIDERS')
            for line in document.records(section)}
        for line in document.records('TREATMENT'):
            span = SourceSpan(source=document.source, line=line.number, column=1, end_column=len(line.content)+1)
            try:
                _, _, expression = _parts(line)
                if canonical_key(line.values[0]) not in nodes or canonical_key(line.values[1]) not in pollutants:
                    raise ValueError('An earlier source assignment uses a removed node/pollutant')
                self.expressions.parse(expression, pollutants=pollutants)
            except (ValueError, TypeError) as error:
                yield Diagnostic(code='treatment.source_reference', section='TREATMENT', span=span,
                    message=f'Native parses every source assignment: {error}; normalize obsolete assignments explicitly')
                continue
            if len(line.content.encode(document.encoding)) >= 1023:
                yield Diagnostic(code='treatment.native_line_capacity', section='TREATMENT', span=span,
                    message='Treatment expression exceeds the fixed native input line capacity')
            tokens = re.finditer(r'[A-Za-z_][A-Za-z_0-9]*|(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?', expression)
            if any(len(token[0]) >= 255 for token in tokens):
                yield Diagnostic(code='treatment.native_token_capacity', section='TREATMENT', span=span,
                    message='Arithmetic token exceeds the native 254-character payload capacity; explicitly normalize or edit it before running')
