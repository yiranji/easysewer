"""Complete 5.2.4 analysis-option syntax and declared native coercions."""

from datetime import date, time, timedelta
from decimal import Decimal, DecimalException, ROUND_HALF_EVEN, localcontext
from dataclasses import replace
import math
from pathlib import Path, PureWindowsPath
import re

from ...model.fields import validate_fields
from ...model.identity import Ref
from ...model.options import DayTime, MonthDay, Options, OPTIONS_COLLECTION, get_options, option_diagnostics
from ...model.values import FileReference
from ...schema.option_profile import OPTION_DEFINITIONS, OPTIONS_BY_KEYWORD, clock_duration, decimal_clock_hours, resolve_options, inspect_option
from ...schema.field_contracts import FieldRule
from ...schema.context_fields import DAYTIME_FIELD_RULES, OPTION_COMPONENT_FIELD_RULES
from ...schema.registry import DecodedFeature, FeatureDescriptor
from ...schema.structured import EncodedRow, FeatureData, FeatureEncoding, OmittedRecord, RecordEntry, SourceBinding
from ...validation import Diagnostic, Severity, SourceSpan, ValidationError, ValidationReport
from .durations import DurationCodec
from .geometry import finite_number, integer, number_text
from .field_sources import FieldSources
from ...validation._cooperative import checkpointed

_CLOCK = re.compile(r"([+]?[0-9]+):([+]?[0-9]+)(?::([+]?[0-9]+)(\.[0-9]+)?)?\Z")
_MONTHS = {month: index + 1 for index, month in enumerate((
    "JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"))}
_ROUTING_ALIASES = {"NF": "STEADY", "KW": "KINWAVE", "EKW": "KINWAVE", "XKINWAVE": "KINWAVE", "DW": "DYNWAVE"}
_OWNER = Ref(collection="swmm:options", key="settings")


def _native_time(token, *, integer_seconds):
    """Native integer clock components; decimal hours round when decoded."""
    match = _CLOCK.fullmatch(token)
    if match:
        hours, minutes, seconds = int(match[1]), int(match[2]), int(match[3] or 0)
        if minutes >= 60 or seconds >= 60:
            raise ValueError("Minute and second components must be less than 60")
        if hours * 3600 + minutes * 60 + seconds > 2_147_483_647:
            raise ValueError("Clock exceeds native signed integer range; use decimal hours for calendar clocks")
        result = timedelta(hours=hours, minutes=minutes, seconds=seconds)
        changed = bool(match[4] and Decimal(match[4]) != 0)
        return result, changed
    hours = finite_number(token)
    if hours < 0:
        raise ValueError("Time cannot be negative")
    if integer_seconds:
        day_value = hours / 24.0
        days = math.floor(day_value)
        seconds = min(86399, math.floor((day_value - days) * 86400 + .5)) + 86400 * days
        if seconds > 2_147_483_647:
            raise ValueError("Time exceeds native signed integer range")
        result = timedelta(seconds=seconds)
        return result, abs(hours * 3600 - seconds) > 1e-9
    # Keep the public microsecond clock stable across INP round trips even when
    # the total hours exceed a float's microsecond precision.
    return _decimal_hours_duration(token)


def _decimal_hours_duration(token):
    hours = finite_number(token)
    try:
        with localcontext() as context:
            context.prec = 40
            context.rounding = ROUND_HALF_EVEN
            microseconds = (Decimal(token) * Decimal(3_600_000_000)).to_integral_value()
        result = timedelta(microseconds=int(microseconds))
    except (DecimalException, OverflowError):
        raise ValueError("Time exceeds the supported duration range") from None
    return result, abs(result.total_seconds() - hours * 3600) > 1e-12


def _calendar_integer(token):
    # int() also accepts underscores and Unicode decimal digits. The native
    # sscanf parser can truncate those tokens or reject them, so accepting them
    # here would change calendar meaning during normalized export.
    if not re.fullmatch(r"[+]?[0-9]+", token):
        raise ValueError("Calendar components require ASCII decimal integers")
    return int(token)


def _date(token):
    parts = re.split(r"[-/]", token)
    if len(parts) != 3:
        raise ValueError("Expected month/day/year")
    month = _MONTHS.get(parts[0].upper())
    if month is None:
        month = _calendar_integer(parts[0])
    return date(_calendar_integer(parts[2]), month, _calendar_integer(parts[1]))


def parse_option(definition, token, *, source=None):
    kind = definition.kind
    changed = False
    if kind == "choice":
        value = token.upper()
        if definition.field == "flow_routing" and value in _ROUTING_ALIASES:
            value = _ROUTING_ALIASES[value]
        if value not in definition.choices:
            raise ValueError(f"{definition.keyword} expects one of {definition.choices}")
    elif kind == "boolean":
        if token.upper() not in ("YES", "NO"):
            raise ValueError("Expected YES or NO")
        value = token.upper() == "YES"
    elif kind == "number":
        value = finite_number(token)
    elif kind == "integer":
        # atoi truncates numeric suffixes. Retain well-formed numbers and report
        # any coercion; arbitrary trailing junk remains an invalid source row.
        finite_number(token)
        match = re.match(r"[+-]?[0-9]+", token)
        value = int(match[0]) if match else 0
        changed = value != finite_number(token)
        if not 0 <= value <= 2_147_483_647:
            raise ValueError("Integer option exceeds the native nonnegative range")
    elif kind == "compatibility":
        if token not in ("3", "4", "5"):
            raise ValueError("COMPATIBILITY expects 3, 4 or 5")
        value = int(token)
    elif kind == "date":
        value = _date(token)
    elif kind == "month_day":
        parts = token.split("/")
        if len(parts) != 2:
            raise ValueError("Expected month/day")
        value = MonthDay(month=_calendar_integer(parts[0]), day=_calendar_integer(parts[1]))
    elif kind in ("scheduled_step", "seconds_or_clock", "seconds", "clock"):
        if kind in ("scheduled_step", "clock") or (kind == "seconds_or_clock" and ":" in token):
            value, changed = _native_time(token, integer_seconds=(kind != "clock"))
        else:
            seconds = finite_number(token)
            if definition.field == "lengthening_step" and seconds < 0:
                value, changed = timedelta(), True
            else:
                value = DurationCodec(numeric_unit="seconds").parse(token)
        if kind == "clock":
            clock = time(value.seconds // 3600, value.seconds % 3600 // 60, value.seconds % 60, value.microseconds)
            value = DayTime(clock=clock, day_offset=value.days) if value.days else clock
    elif kind == "directory":
        windows = bool(PureWindowsPath(token).drive or (source and PureWindowsPath(source).drive))
        if source:
            base = str(PureWindowsPath(source).parent) if windows else str(Path(source).parent)
        else:
            base = None
        value = FileReference(path=token, base_directory=base, flavor="windows" if windows else "native", direction="output")
    else:
        raise ValueError(f"No codec for option kind {kind}")
    return value, changed


def format_option(definition, value):
    kind = definition.kind
    if kind == "boolean":
        return "YES" if value else "NO"
    if kind in ("number", "integer", "compatibility"):
        return number_text(value)
    if kind in ("scheduled_step", "seconds_or_clock", "seconds"):
        return DurationCodec(numeric_unit="seconds").format(value, style="clock" if kind == "scheduled_step" else "numeric")
    if kind == "date":
        return f"{value.month:02d}/{value.day:02d}/{value.year:04d}"
    if kind == "month_day":
        return f"{value.month:02d}/{value.day:02d}"
    if kind == "clock":
        duration = clock_duration(value)
        if duration.microseconds or duration.total_seconds() > 2_147_483_647:
            # Native clock syntax discards fractional seconds. Decimal hours
            # also avoid signed integer overflow for clocks beyond 2**31-1 s.
            return decimal_clock_hours(duration)
        return DurationCodec(numeric_unit="seconds").format(duration)
    if kind == "directory":
        return value.path
    return value


class OptionsCodec:
    descriptor = FeatureDescriptor(key="swmm:options", sections=frozenset({"OPTIONS"}), atomic_write=True)
    collections = (OPTIONS_COLLECTION,)
    field_rules = tuple(FieldRule(value_type=Options, field=item.field, resolve=inspect_option)
                        for item in OPTION_DEFINITIONS) + DAYTIME_FIELD_RULES + OPTION_COMPONENT_FIELD_RULES

    def decode(self, document, profile):
        values, bindings, issues = {}, [], []
        sources = FieldSources()
        sources.cover(_OWNER, *((item.field,) for item in checkpointed(OPTION_DEFINITIONS)))
        def uncertain_row(line):
            definition = OPTIONS_BY_KEYWORD.get(line.values[0].upper()) if line.values else None
            if definition is not None:
                sources.block(_OWNER, (definition.field,))
                if definition.field == 'flow_routing':
                    sources.block(_OWNER, ('ignore_routing',))

        def declare(line, name, *, role='value', contributes=True):
            sources.add(_OWNER, (name,), line, (1,), role=role, contributes=contributes)
        lexical_errors = {issue.span.line for issue in checkpointed(document.report.errors) if issue.span}
        for line in checkpointed(document.records("OPTIONS")):
            if line.number in lexical_errors:
                uncertain_row(line)
                continue
            span = SourceSpan(source=document.source, line=line.number, column=1, end_column=len(line.content) + 1)
            definition = OPTIONS_BY_KEYWORD.get(line.values[0].upper()) if line.values else None
            if definition is None:
                issues.append(Diagnostic(code="options.unknown_option", severity=Severity.WARNING,
                                         message="Unknown option retained as source", section="OPTIONS", span=span))
                continue
            try:
                if len(line.values) != 2:
                    raise ValueError("An option requires exactly a keyword and value")
                value, coerced = parse_option(definition, line.values[1], source=document.source)
                candidate = dict(values)
                if definition.field == "flow_routing" and value == "NONE":
                    candidate["flow_routing"] = candidate.get("flow_routing", profile.option_default("flow_routing"))
                    candidate["ignore_routing"] = True
                else:
                    candidate[definition.field] = value
                ValidationReport(diagnostics=tuple(validate_fields(Options(**candidate)))).raise_for_errors()
                if definition.field in values:
                    issues.append(Diagnostic(code="options.repeated_assignment", severity=Severity.INFO,
                                             message="Later assignment determines the effective option", section="OPTIONS",
                                             field=definition.field, span=span))
                if coerced:
                    issues.append(Diagnostic(code="options.native_coercion", severity=Severity.WARNING,
                                             message=f"SWMM 5.2.4 interprets {line.values[1]!r} as {value!r}",
                                             field=definition.field, section="OPTIONS", span=span))
                values = candidate
                bindings.append(SourceBinding(line=line.number, key=(definition.field,)))
                if definition.field == 'flow_routing' and value == 'NONE':
                    declare(line, 'flow_routing', role='retained', contributes=False)
                    declare(line, 'ignore_routing', role='derived')
                else:
                    declare(line, definition.field)
                    if definition.kind == 'clock':
                        for name in checkpointed(('clock', 'day_offset')):
                            path = (definition.field, name)
                            sources.cover(_OWNER, path)
                            sources.add(_OWNER, path, line, (1,), role='derived')
                    elif definition.kind == 'month_day':
                        for name in checkpointed(('month', 'day')):
                            path = (definition.field, name)
                            sources.cover(_OWNER, path)
                            sources.add(_OWNER, path, line, (1,), role='derived')
                    elif definition.kind == 'directory':
                        for name in checkpointed(('path', 'base_directory', 'flavor', 'direction')):
                            path = (definition.field, name)
                            sources.cover(_OWNER, path)
                            sources.add(_OWNER, path, line, (0,) if name == 'direction' else (1,),
                                        role='value' if name == 'path' else 'derived')
            except (ValueError, OverflowError, TypeError) as error:
                uncertain_row(line)
                issues.append(Diagnostic(code="options.invalid_input", message=str(error), section="OPTIONS",
                                         field=definition.field, span=span))
        options = Options(**values)
        return DecodedFeature(value=FeatureData(records=(RecordEntry(collection="swmm:options", value=options),),
                                               bindings=tuple(bindings), **sources.finish({_OWNER.canonical: options})),
                              claimed_lines=frozenset(item.line for item in checkpointed(bindings)),
                              report=ValidationReport(diagnostics=tuple(issues)))

    def encode(self, store, profile):
        if not store.collection("swmm:options"):
            return FeatureEncoding()
        options = get_options(store)
        rows = tuple(EncodedRow(key=(item.field,), section="OPTIONS", values=(item.keyword, format_option(item, getattr(options, item.field))),
                                owners=(_OWNER,))
                     for item in checkpointed(OPTION_DEFINITIONS) if getattr(options, item.field) is not None)
        omitted = () if rows else (OmittedRecord(owner=_OWNER, reason="No analysis options are explicitly set"),)
        return FeatureEncoding(rows=rows, omitted=omitted)

    def validate(self, store, profile):
        return ()  # Local field validation belongs to the collection.

    def file_uses(self, store, profile):
        from ...model.file_resources import FileUse
        file = get_options(store).temp_directory
        if file is not None:
            yield FileUse(owner=_OWNER, path=("temp_directory",), file=file, role="swmm:temporary_directory",
                          format="core:directory", kind="directory", access="write")

    def validate_run(self, store, profile):
        options = get_options(store)
        if tuple(validate_fields(options)):
            return ()
        try:
            issues = resolve_options(options, profile).report.diagnostics
        except ValidationError as error:
            issues = error.report.diagnostics
        return tuple(option_diagnostics(issues))
