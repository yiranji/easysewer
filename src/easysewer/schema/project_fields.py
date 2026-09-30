"""Informational project declarations, without rendering a GUI or evaluating JSON."""
from dataclasses import fields

from ..model.fields import validate_fields
from ..model.identity import Ref
from ..model.inspection import FieldFact, FieldSemantics
from ..model.project import ProjectTitle, JsonAnnotation, ObjectTag, ProfilePlot
from ..validation import Diagnostic
from .field_contracts import FieldRule


def project_field(context):
    root = context.record
    issues = tuple(validate_fields(root))
    if type(root) is ProjectTitle and not issues:
        from ..io.inp.project import _MARKER
        issues += tuple(Diagnostic(code='project.reserved_annotation', field=f'lines[{i}]',
            message='Version 1 annotation markers belong to metadata, not title prose')
            for i, line in enumerate(root.lines) if _MARKER.fullmatch(line.strip()))
    refs = (root.target,) if type(root) is ObjectTag else root.links if type(root) is ProfilePlot else ()
    if not issues:
        issues += tuple(Diagnostic(code='project.field_reference', message='Missing referenced project object')
                        for ref in refs if not context.store.contains(ref))
    reason = {
        ProjectTitle: 'Stored physical title lines, including comments and blanks; the engine reports only its first three non-comment title lines',
        JsonAnnotation: 'Informational JSON declaration; embedded names and numbers are not model references or physical quantities',
        ObjectTag: 'Configured GUI tag; no hydraulic or rendered GUI value is computed',
        ProfilePlot: 'Configured ordered profile links; no rendered profile or path continuity is inferred',
    }[type(root)]
    default = FieldFact(status='required')
    if type(root) is ProjectTitle:
        default = FieldFact(status='known', value=(), reason='An omitted title has no prose')
        if type(context.path[-1]) is int:
            default = FieldFact(status='not_applicable', reason='A title line has no independent omission default')
    elif type(context.container) is Ref and context.field == 'collection':
        default = FieldFact(status='not_applicable', reason='Reference class is declared by the project grammar')
    return FieldSemantics(unit=FieldFact(status='not_applicable', reason='Informational declaration has no physical unit'),
        default=default, effective=(FieldFact(status='invalid', reason='Invalid project declaration or missing reference')
            if issues else FieldFact(status='known', value=context.value, reason=reason)), diagnostics=issues)


def rules(root, *nested):
    return tuple(FieldRule(value_type=value_type, field=f.name, root_type=root,
                           resolve=project_field, resolve_item=project_field if f.name in ('lines', 'links') else None)
                 for value_type in (root, *nested) for f in fields(value_type))


PROJECT_FIELD_RULES = (*rules(ProjectTitle), *rules(JsonAnnotation))
TAG_FIELD_RULES = rules(ObjectTag, Ref)
PROFILE_FIELD_RULES = rules(ProfilePlot, Ref)
