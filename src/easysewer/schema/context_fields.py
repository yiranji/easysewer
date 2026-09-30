"""Configured reporting, interface, event and calendar component contracts."""
from dataclasses import fields, replace

from ..model.events import EventSchedule, RoutingEvent
from ..model.fields import validate_fields
from ..model.files import InterfaceFile
from ..model.identity import Ref
from ..model.inspection import FieldFact, FieldSemantics
from ..model.options import DayTime, MonthDay, Options
from ..model.report import REPORT_DEFAULTS, ReportOptions, ReportSelection
from ..model.values import FileReference
from ..validation import Diagnostic, ValidationReport
from .field_contracts import FieldRule
from .option_profile import inspect_option


def known(value, reason=''):
    return FieldFact(status='known', value=value, reason=reason)


def na(reason):
    return FieldFact(status='not_applicable', reason=reason)


def report_field(context):
    root = context.record
    defaults = dict(context.profile.report_defaults)
    issues = tuple(validate_fields(root))
    valid_defaults = set(defaults) == {name for name, _ in REPORT_DEFAULTS}
    if valid_defaults:
        default_issues = tuple(validate_fields(ReportOptions(**defaults)))
        valid_defaults = not ValidationReport(diagnostics=default_issues).errors and all(
            value is not None for value in defaults.values())
    if not valid_defaults:
        issues += (Diagnostic(code='report.field_defaults', message='Profile must declare complete valid reporting defaults'),)
    name = context.path[0]
    selection = getattr(root, name)
    if selection is None and valid_defaults:
        selection = defaults[name]
    if isinstance(selection, ReportSelection):
        issues += tuple(Diagnostic(code='report.field_reference', field=name,
            message='Missing referenced reporting object') for ref in selection.members
            if not context.store.contains(ref))
    default = (known(defaults[name], 'Input default declared by the profile') if valid_defaults
               else FieldFact(status='invalid', reason='Invalid reporting defaults')) if len(context.path) == 1 else na(
                   'Selection components have no independent reporting option default')
    value = selection if len(context.path) == 1 else context.value
    effective = known(value, 'Configured reporting value with profile defaults; cumulative members and final mode are distinct. Use effective_report for actual output objects')
    if ValidationReport(diagnostics=issues).errors:
        effective = FieldFact(status='invalid', reason='Invalid reporting configuration, defaults or reference')
    return FieldSemantics(unit=na('Reporting switch, selector or object identity'),
                          default=default, effective=effective, diagnostics=issues)


def file_field(context):
    root = context.record
    issues = tuple(validate_fields(root))
    if root.kind != 'HOTSTART' and sum(v.kind == root.kind for v in context.store.collection('swmm:files').values()) > 1:
        issues += (Diagnostic(code='files.conflicting_modes', message='Interface kind has more than one active native slot'),)
    default = FieldFact(status='required')
    if type(context.container) is FileReference and context.field != 'path':
        default = na('Derived file direction or lexical path context; not an independent native default')
    effective = known(context.value, 'Configured interface declaration; inspection does not open the file, check its format or establish runtime activity')
    if ValidationReport(diagnostics=issues).errors:
        effective = FieldFact(status='invalid', reason='Invalid interface configuration')
    return FieldSemantics(unit=na('Interface kind, disposition or lexical file reference'),
                          default=default, effective=effective, diagnostics=issues)


def event_field(context):
    issues = tuple(validate_fields(context.record))
    default = FieldFact(status='required')
    unit = na('Ordered event declarations')
    if context.path == ('periods',):
        default = known((), 'No routing event restrictions when EVENTS is omitted')
    elif type(context.path[-1]) is int:
        default = na('An event item has no independent omission default')
    else:
        unit = known('local model datetime')
    effective = known(context.value, 'Configured event order and bounds; engine startup sorts and clips overlapping intervals without merging them')
    if ValidationReport(diagnostics=issues).errors:
        effective = FieldFact(status='invalid', reason='Invalid event interval')
    return FieldSemantics(unit=unit, default=default, effective=effective, diagnostics=issues)


def daytime_field(context):
    name = context.path[0]
    parent = inspect_option(replace(context, path=(name,), container=context.record,
        field=name, value=getattr(context.record, name)))
    effective = parent.effective
    if effective.status == 'known':
        value = effective.value
        component = (value.clock if isinstance(value, DayTime) else value) if context.field == 'clock' else (
            value.day_offset if isinstance(value, DayTime) else 0)
        effective = known(component, 'Component of the effective parent clock; reporting rollover and clamping are carried by effective report_start_date, leaving day_offset zero')
    return FieldSemantics(unit=known('local model clock' if context.field == 'clock' else 'day'),
        default=na('Clock components have no independent INP default; resolve the parent option'),
        effective=effective, diagnostics=parent.diagnostics)


def option_component_field(context):
    name = context.path[0]
    parent = inspect_option(replace(context, path=(name,), container=context.record,
        field=name, value=getattr(context.record, name)))
    default = na('This component has no independent INP default; resolve the parent option')
    if type(context.container) is MonthDay:
        unit = known('month' if context.field == 'month' else 'day',
            'Calendar component in the native non-leap reference year, not an elapsed duration')
        reason = ('Configured sweeping calendar component; native bounds use non-leap day ordinals with inclusive '
                  'comparisons and wrapping seasons. Runtime uses actual day-of-year, retaining the native leap-year offset')
    else:
        unit = na('Lexical temporary-directory reference')
        reason = ('Configured directory context only; no file IO or scratch-directory availability is inferred. '
                  'Runner workspace placement and native platform scratch behavior are separate')
        if context.field == 'path':
            default = FieldFact(status='required', reason='An explicit TEMPDIR declaration requires a path')
    effective = parent.effective
    if effective.status == 'known':
        effective = known(getattr(effective.value, context.field), reason)
    return FieldSemantics(unit=unit, default=default, effective=effective, diagnostics=parent.diagnostics)


def rules(root, resolver, *nested):
    return tuple(FieldRule(value_type=cls, root_type=root, field=f.name, resolve=resolver,
        resolve_item=resolver if f.name in ('members', 'periods') else None)
        for cls in (root, *nested) for f in fields(cls))


REPORT_FIELD_RULES = rules(ReportOptions, report_field, ReportSelection, Ref)
FILES_FIELD_RULES = rules(InterfaceFile, file_field, FileReference)
EVENT_FIELD_RULES = rules(EventSchedule, event_field, RoutingEvent)
DAYTIME_FIELD_RULES = tuple(FieldRule(value_type=DayTime, root_type=Options,
    field=f.name, resolve=daytime_field) for f in fields(DayTime))
OPTION_COMPONENT_FIELD_RULES = tuple(FieldRule(value_type=cls, root_type=Options,
    field=f.name, resolve=option_component_field) for cls in (MonthDay, FileReference) for f in fields(cls))
