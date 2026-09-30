"""Treatment configuration and variable units, without evaluating water quality."""
from dataclasses import fields

from ..model import treatment as t, expressions as e, network as n, quality as q
from ..model.fields import validate_fields
from ..model.identity import Ref, canonical_key
from ..model.inspection import FieldFact, FieldSemantics
from ..model.options import get_options
from ..model.units import UnitContext
from ..validation import Diagnostic, ValidationReport
from .field_contracts import FieldRule
from .hydrology_fields import known, invalid, na, reference

LEAVES = (t.TreatmentProcessVariable, t.TreatmentConcentration, t.TreatmentRemoval)
ARITHMETIC = (e.ExpressionNumber, e.UnaryExpression, e.BinaryExpression, e.FunctionExpression)
PROCESS_DIMENSIONS = {'HRT': 'hours', 'DT': 'seconds', 'FLOW': 'flow', 'DEPTH': 'length', 'AREA': 'area'}


def treatment_context(context):
    rows = context.store.collection('swmm:treatment')
    pending, seen = [context.record], set()
    while pending:
        row = pending.pop()
        if type(row) is not t.Treatment:
            return FieldFact(reason='Extension treatment requires its own field contract')
        nodes = tuple(e.walk_expression(row.expression))
        if any(not isinstance(v, e.ExpressionNode) for v in nodes):
            return invalid('Invalid treatment arithmetic')
        if any(type(v) not in (*ARITHMETIC, *LEAVES) for v in nodes):
            return FieldFact(reason='Extension treatment arithmetic requires its own field contract')
        if any(type(v) is e.FunctionExpression and v.function not in e.FUNCTIONS for v in nodes):
            return FieldFact(reason='Extension arithmetic function requires its own field contract')
        if not ValidationReport(diagnostics=tuple(validate_fields(row))).is_valid:
            return invalid('Invalid treatment declaration or dependent equation')
        key = canonical_key((row.node.key, row.pollutant.key))
        if key in seen: continue
        seen.add(key)
        refs = [(row.node, (n.Junction, n.Storage, n.Divider, n.Outfall)), (row.pollutant, (q.Pollutant,))]
        refs += [(v.pollutant, (q.Pollutant,)) for v in nodes if type(v) in (t.TreatmentConcentration, t.TreatmentRemoval)]
        for ref, types in refs:
            fact = reference(context, ref)
            if fact.status != 'known': return fact
            if type(context.store.collection(ref.collection)[ref.key]) not in types:
                return FieldFact(reason='Extension treatment target requires its own field contract')
        # A concentration leaf reads the referenced pollutant's treatment kind;
        # a removal leaf also evaluates its equation. Keep both dependencies.
        for v in nodes:
            if type(v) in (t.TreatmentConcentration, t.TreatmentRemoval):
                other = rows.get((row.node.key, v.pollutant.key))
                if other is not None: pending.append(other)
    if any(d.severity.value == 'error' and d.object_id in {str(key) for key in seen}
           for d in t.validate_treatment(context.store)):
        return invalid('Treatment names or removal dependencies are not native-compatible')
    return known(context.record)


def treatment_field(context):
    root, container, name, value = context.record, context.container, context.field, context.value
    options = get_options(context.store)
    issues = tuple(validate_fields(options))
    if not ValidationReport(diagnostics=issues).is_valid:
        return FieldSemantics(effective=invalid('Invalid treatment options'), diagnostics=issues)
    fact = treatment_context(context)
    if fact.status != 'known':
        return FieldSemantics(effective=fact, diagnostics=(Diagnostic(code='treatment.field_context', message=fact.reason),)
                              if fact.status in ('invalid', 'ambiguous') else ())
    units = UnitContext(flow_units=options.flow_units or context.profile.option_default('flow_units'))
    unit, default = na('Non-numeric treatment syntax'), FieldFact(status='required')
    effective = known(value, 'Configured treatment expression; no concentration, removal, clipping or runtime state is evaluated')
    def concentration(ref):
        return units.unit(q.concentration_dimension(context.store.collection(ref.collection)[ref.key]))
    if container is root and name == 'expression':
        unit = known('1' if root.kind == 'R' else concentration(root.pollutant),
                     'Declared equation output; absence of an equation is not equivalent to zero removal')
    elif type(container) is Ref:
        if name == 'collection': default = na('Reference namespace fixed by treatment grammar')
    else:
        leaf = container if type(container) in LEAVES else value if type(value) in LEAVES else None
        if type(leaf) is t.TreatmentProcessVariable:
            reason = {'HRT': 'Residence time in hours; zero at non-storage nodes',
                      'DT': 'Routing time step in seconds', 'FLOW': 'Current node inflow in user flow units',
                      'DEPTH': 'Mean of old and new node depths', 'AREA': 'Mean surface area at old and new depths'}[leaf.name]
            unit = known('s' if leaf.name == 'DT' else units.unit(PROCESS_DIMENSIONS[leaf.name]), reason)
            effective = known(value, 'Configured process variable; '+reason)
        elif type(leaf) is t.TreatmentConcentration:
            unit = known(concentration(leaf.pollutant), 'Referenced pollutant units; its treatment kind selects influent or mixed-node concentration')
        elif type(leaf) is t.TreatmentRemoval:
            unit = known('1', 'Fractional removal at this node; a missing equation yields zero, not an implicit equation')
        elif type(container) in ARITHMETIC and name not in ('operator', 'function'):
            unit = FieldFact(reason='Empirical constant or subexpression has no independently declared unit; no dimensional inference is imposed')
    return FieldSemantics(unit=unit, default=default, effective=effective)


TREATMENT_FIELD_RULES = tuple(FieldRule(value_type=value_type, field=f.name, root_type=t.Treatment, resolve=treatment_field)
    for value_type in (t.Treatment, *ARITHMETIC, *LEAVES, Ref) for f in fields(value_type))
