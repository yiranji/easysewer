"""Explicit display declarations, independent of hydraulic units and GUI state."""

from dataclasses import fields

from ..model.fields import validate_fields
from ..model.inspection import FieldFact, FieldSemantics
from ..model.identity import Ref
from ..model.project import Backdrop, MapExtent, MapSettings, MapLabel, MapLabels, resolve_map
from ..model.values import FileReference, Point
from ..validation import Diagnostic
from .field_contracts import FieldRule


def _known(value, reason=''):
    return FieldFact(status='known', value=value, reason=reason)


def _na(reason):
    return FieldFact(status='not_applicable', reason=reason)


def _map_context(context):
    def stored(collection, key, default):
        try:
            return context.store.collection(collection).get(key, default)
        except KeyError:
            return default
    settings = stored('swmm:map', 'settings', MapSettings())
    backdrop = stored('swmm:backdrop', 'image', Backdrop())
    diagnostics = (*validate_fields(settings), *validate_fields(backdrop))
    if settings.units_precedence == 'BACKDROP' and (settings.units is None or backdrop.units is None):
        diagnostics += (Diagnostic(code='map.unit_precedence', field='units_precedence',
            message='BACKDROP precedence requires explicit units in both MAP and BACKDROP'),)
    return settings, backdrop, diagnostics


def coordinate_unit(context):
    settings, backdrop, diagnostics = _map_context(context)
    if diagnostics:
        return FieldFact(status='invalid', reason='Invalid map context'), diagnostics
    units = resolve_map(settings, backdrop).units
    if units is None or units == 'NONE':
        return FieldFact(reason='No explicit coordinate unit; ambient GUI preferences are not model data'), ()
    return _known({'FEET': 'ft', 'METERS': 'm', 'DEGREES': 'degree'}[units],
                  'Explicit map coordinate unit; no CRS or hydraulic-unit conversion implied'), ()


def map_field(context):
    default = FieldFact(reason='Omitted display state may depend on GUI environment or image; no default is invented')
    unit = _na('Non-numeric display declaration')
    effective = _known(context.value, 'Stored display declaration; does not render the GUI')
    root = context.record
    issues = tuple(validate_fields(root))
    legacy = context.path[0] in ('legacy_offset', 'legacy_scaling')
    if legacy:
        unit = FieldFact(reason='Deprecated coordinates/scaling are preserved without assuming active unit semantics')
        effective = _na('GUI 5.2.4 reads these legacy values but does not use them')
    elif isinstance(context.container, (Point, MapExtent)) or context.field == 'extent':
        unit, context_issues = coordinate_unit(context)
        issues += context_issues
        if context.value is None:
            effective = FieldFact(reason='No explicit bounds; automatic GUI/image bounds are not computed')
        else:
            default = FieldFact(status='required') if context.field != 'extent' else default
    elif isinstance(context.container, FileReference):
        default = FieldFact(status='required') if context.field == 'path' else _na('Derived lexical file context')
    elif context.field == 'units':
        settings, backdrop, context_issues = _map_context(context)
        issues += context_issues
        resolved = resolve_map(settings, backdrop) if not context_issues else None
        effective = (_known(resolved.units, f'Shared coordinate units declared by {resolved.units_source}')
                     if resolved is not None and resolved.units is not None else FieldFact(reason='No explicit shared units'))
    elif context.field == 'units_precedence':
        default = _na('Derived serialization order has no standalone INP default')
        _, _, context_issues = _map_context(context)
        issues += context_issues
    elif context.field == 'file' and root.file is None:
        effective = _known(None, 'Explicit FILE empty-string clearing command') if root.clear_file else FieldFact(reason='No image command')
    elif context.field == 'clear_file':
        default = _known(False, 'No clearing command when FILE is absent')
    if issues:
        effective = FieldFact(status='invalid', reason='Invalid display field/context')
    return FieldSemantics(unit=unit, default=default, effective=effective, diagnostics=tuple(dict.fromkeys(issues)))


def label_field(context):
    default = FieldFact(status='required')
    unit = _na('Non-numeric label declaration')
    effective = _known(context.value, 'Stored label declaration, not rendered pixel geometry')
    issues = tuple(validate_fields(context.record))
    if not issues:
        for index, row in enumerate(context.record.entries):
            if isinstance(getattr(row, 'anchor', None), Ref) and not context.store.contains(row.anchor):
                issues += (Diagnostic(code='label.missing_anchor', field=f'entries[{index}].anchor',
                    message='Label anchor does not refer to an existing node'),)
    if context.field == 'entries':
        default = _known((), 'No labels in an omitted layer')
    elif context.field in ('font_name', 'font_size', 'bold', 'italic', 'anchor'):
        defaults = dict(font_name='Arial', font_size=10, bold=False, italic=False, anchor=None)
        default = _known(defaults[context.field], 'GUI 5.2.4 label input default')
        if context.field == 'font_size':
            unit = _known('pt', 'Font point size is independent of map/flow units')
    elif isinstance(context.container, Ref) and context.field == 'collection':
        default = _na('The label grammar fixes the reference namespace')
    if isinstance(context.container, Point) or context.field == 'position':
        unit, context_issues = coordinate_unit(context)
        issues += context_issues
    if issues:
        effective = FieldFact(status='invalid', reason='Invalid label field/context')
    return FieldSemantics(unit=unit, default=default, effective=effective, diagnostics=issues)


MAP_FIELD_RULES = tuple(FieldRule(value_type=value_type, field=f.name, root_type=root_type, resolve=map_field)
    for root_type in (MapSettings, Backdrop)
    for value_type in (root_type, MapExtent, Point, *((FileReference,) if root_type is Backdrop else ()))
    for f in fields(value_type))

LABEL_FIELD_RULES = tuple(FieldRule(value_type=value_type, field=f.name, root_type=MapLabels, resolve=label_field)
    for value_type in (MapLabels, MapLabel, Point, Ref) for f in fields(value_type))
