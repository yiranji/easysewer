"""Ordered shared-resource blocks, including native per-series date continuation."""

from collections import OrderedDict
from dataclasses import fields
from datetime import datetime, time, timedelta
from pathlib import Path, PureWindowsPath

from ...model.fields import validate_fields
from ...model.identity import Ref, canonical_key
from ...model.options import get_options
from ...model.resources import (
    CURVE_KINDS, PATTERN_LENGTHS, Curve, CurvePoint, FileTimeSeries, InlineTimeSeries,
    Pattern, SeriesPoint, resource_collections,
    RESOURCE_UNIT_TRANSFORMS,
)
from ...model.values import FileReference
from ...schema.option_profile import clock_duration, decimal_clock_hours
from ...schema.resource_fields import RESOURCE_FIELD_RULES
from ...schema.registry import DecodedFeature, FeatureDescriptor
from ...schema.structured import EncodedRow, FeatureData, RecordEntry, SourceBinding
from ...validation import Diagnostic, Severity, SourceSpan, ValidationReport
from ...validation._cooperative import checkpointed
from .durations import DurationCodec
from .geometry import finite_number, number_text
from .options import _date, _native_time, _decimal_hours_duration
from .field_sources import FieldSources

_NAMES = {"CURVES": "swmm:curves", "TIMESERIES": "swmm:timeseries", "PATTERNS": "swmm:patterns"}


class _Unsupported(ValueError):
    pass


def _key(section, name, index="header"):
    return section, canonical_key(name), str(index)


def _series_time(token):
    if ":" in token:
        return _native_time(token, integer_seconds=False)
    return _decimal_hours_duration(token)


def _time_text(value):
    if value.microseconds or value < timedelta() or value.total_seconds() > 2_147_483_647:
        return decimal_clock_hours(value)
    return DurationCodec(numeric_unit="hours").format(value)


def _resource_sources(source, owner, record, lines):
    """Bind a successfully decoded whole group; never claim a partial resource."""
    source.cover(owner, *((f.name,) for f in fields(record)))
    for index, line in enumerate(lines):
        source.add(owner, ('id',), line, (0,), role='value' if index == 0 else 'retained', contributes=index == 0)
    if type(record) in (Curve, Pattern):
        source.add(owner, ('kind',), lines[0], (1,))
        count = 0
        for row, line in enumerate(lines):
            start = 2 if row == 0 else 1
            if type(record) is Curve:
                for col in range(start, len(line.values), 2):
                    prefix = ('points', count)
                    source.cover(owner, prefix, prefix + ('x',), prefix + ('y',))
                    source.add(owner, ('points',), line, (col, col + 1), overwrite=False)
                    source.add(owner, prefix, line, (col, col + 1))
                    for name, pos in (('x', col), ('y', col + 1)):
                        source.add(owner, prefix + (name,), line, (pos,))
                    count += 1
            else:
                for col in range(start, len(line.values)):
                    active = count < 24
                    source.add(owner, ('factors',), line, (col,), overwrite=False,
                               role='value' if active else 'retained', contributes=active)
                    if active:
                        source.cover(owner, ('factors', count))
                        source.add(owner, ('factors', count), line, (col,))
                    count += 1
    elif type(record) is FileTimeSeries:
        line = lines[0]
        source.add(owner, ('file',), line, (1, 2))
        source.cover(owner, *(('file', f.name) for f in fields(FileReference)))
        source.add(owner, ('file', 'path'), line, (2,))
        for name in ('base_directory', 'flavor'):
            source.add(owner, ('file', name), line, (2,), role='derived')
        source.add(owner, ('file', 'direction'), line, (1,), role='derived')
    else:
        count, date_source = 0, None
        for line in lines:
            col = 1
            while col < len(line.values):
                start = col
                try:
                    _date(line.values[col])
                    explicit_date = True
                except (ValueError, OverflowError):
                    explicit_date = False
                if explicit_date:
                    date_source = line, col
                    col += 1
                prefix = ('points', count)
                source.cover(owner, prefix, prefix + ('time',), prefix + ('value',))
                source.add(owner, ('points',), line, range(start, col + 2), overwrite=False)
                source.add(owner, prefix, line, range(start, col + 2))
                source.add(owner, prefix + ('value',), line, (col + 1,))
                if explicit_date or date_source is None:
                    source.add(owner, prefix + ('time',), line, range(start, col + 1))
                else:
                    date_line, date_col = date_source
                    source.add(owner, prefix + ('time',), date_line, (date_col,), role='derived', overwrite=False)
                    source.add(owner, prefix + ('time',), line, (col,), role='derived', overwrite=False)
                count += 1
                col += 2


class ResourcesCodec:
    def file_uses(self, store, profile):
        from ...model.file_resources import FileUse
        for record in checkpointed(store.collection("swmm:timeseries").values()):
            if isinstance(record, FileTimeSeries):
                yield FileUse(owner=Ref(collection="swmm:timeseries", key=record.id), path=("file",), file=record.file,
                              role="swmm:time_series", format="swmm:timeseries.data")

    descriptor = FeatureDescriptor(key="swmm:resources", sections=frozenset(_NAMES), atomic_write=True)
    collections = resource_collections()
    unit_transforms = RESOURCE_UNIT_TRANSFORMS
    field_rules = RESOURCE_FIELD_RULES

    def decode(self, document, profile):
        records, bindings, issues = [], [], []
        source_fields = FieldSources()
        lexical_errors = {item.span.line for item in checkpointed(document.report.errors) if item.span}

        def diagnostic(line, code, message, severity=Severity.ERROR):
            from ...validation import DiagnosticSubject
            issues.append(Diagnostic(code=code, message=message, severity=severity, section=line.section,
                                     object_id=line.values[0] if line.values else None,
                                     subject=DiagnosticSubject(collection=_NAMES[line.section], key=line.values[0])
                                         if line.values else None,
                                     span=SourceSpan(source=document.source, line=line.number, column=1,
                                                     end_column=len(line.content) + 1)))

        for section, namespace in checkpointed(_NAMES.items()):
            groups = OrderedDict()
            for line in checkpointed(document.records(section)):
                if line.values:
                    groups.setdefault(canonical_key(line.values[0]), []).append(line)
            for lines in checkpointed(groups.values()):
                if any(line.number in lexical_errors for line in checkpointed(lines)):
                    continue
                line = lines[0]
                name = line.values[0]
                try:
                    if section == "CURVES":
                        if len(line.values) < 2:
                            raise ValueError("A curve requires a type on its first line")
                        kind = line.values[1].upper()
                        if kind not in CURVE_KINDS:
                            raise _Unsupported(f"Unknown curve type {kind}")
                        points = []
                        for index, line in checkpointed(enumerate(lines)):
                            tokens = line.values[2 if index == 0 else 1:]
                            if len(tokens) % 2:
                                raise ValueError("Curve data requires complete x-y pairs")
                            points.extend(CurvePoint(x=finite_number(tokens[i]), y=finite_number(tokens[i + 1]))
                                          for i in checkpointed(range(0, len(tokens), 2)))
                        record = Curve(id=name, kind=kind, points=tuple(points))
                    elif section == "PATTERNS":
                        if len(line.values) < 2:
                            raise ValueError("A pattern requires a type on its first line")
                        kind = line.values[1].upper()
                        if kind not in PATTERN_LENGTHS:
                            raise _Unsupported(f"Unknown pattern type {kind}")
                        factors = []
                        for index, line in checkpointed(enumerate(lines)):
                            tokens = line.values[2 if index == 0 else 1:]
                            remaining = max(0, 24 - len(factors))
                            factors.extend(finite_number(token) for token in checkpointed(tokens[:remaining]))
                            if len(tokens) > remaining:
                                diagnostic(line, "resource.native_coercion", "Native pattern ignores factors beyond position 24", Severity.WARNING)
                        record = Pattern(id=name, kind=kind, factors=tuple(factors))
                    else:
                        files = [item for item in checkpointed(lines) if len(item.values) > 1 and item.values[1].upper() == "FILE"]
                        if files:
                            # Mixed inline/file assignments and repeated FILE declarations
                            # are retained as a whole until an explicit conflict policy exists.
                            if len(lines) != 1 or len(line.values) != 3:
                                raise ValueError("A file series must contain exactly one FILE declaration and no inline points")
                            path = line.values[2]
                            windows = bool(PureWindowsPath(path).drive or (document.source and PureWindowsPath(document.source).drive))
                            base = str((PureWindowsPath if windows else Path)(document.source).parent) if document.source else None
                            record = FileTimeSeries(id=name, file=FileReference(path=path, base_directory=base,
                                                    flavor="windows" if windows else "native", direction="input"))
                        else:
                            last_date, points = None, []
                            for line in checkpointed(lines):
                                tokens, index = line.values, 1
                                if len(tokens) < 3:
                                    raise ValueError("A series row requires at least one time-value pair")
                                while index < len(tokens):
                                    try:
                                        day = _date(tokens[index])
                                    except (ValueError, OverflowError):
                                        day = None
                                    if day is not None:
                                        last_date = day
                                        index += 1
                                    if index + 1 >= len(tokens):
                                        raise ValueError("Incomplete time-value pair")
                                    elapsed, coerced = _series_time(tokens[index])
                                    when = elapsed if last_date is None else datetime.combine(last_date, time()) + elapsed
                                    points.append(SeriesPoint(time=when, value=finite_number(tokens[index + 1])))
                                    if coerced:
                                        diagnostic(line, "resource.time_precision", "Time was normalized to supported native clock/microsecond precision", Severity.WARNING)
                                    index += 2
                            record = InlineTimeSeries(id=name, points=tuple(points))
                    report = ValidationReport(diagnostics=tuple(validate_fields(record)))
                    report.raise_for_errors()
                    issues.extend(report.diagnostics)
                    _resource_sources(source_fields, Ref(collection=namespace, key=name), record, lines)
                    records.append(RecordEntry(collection=namespace, value=record))
                    bindings.extend(SourceBinding(line=item.number, key=_key(section, name)) for item in checkpointed(lines))
                except (ValueError, TypeError, OverflowError) as error:
                    diagnostic(line, "resource.unsupported_variant" if isinstance(error, _Unsupported) else "resource.invalid_input",
                               str(error), Severity.WARNING if isinstance(error, _Unsupported) else Severity.ERROR)
        originals = {Ref(collection=r.collection, key=r.value.id).canonical: r.value for r in checkpointed(records)}
        return DecodedFeature(value=FeatureData(records=tuple(records), bindings=tuple(bindings),
                              **source_fields.finish(originals)),
                              claimed_lines=frozenset(item.line for item in checkpointed(bindings)),
                              report=ValidationReport(diagnostics=tuple(issues)))

    def encode(self, store, profile):
        def row(section, record, values, index="header"):
            return EncodedRow(key=_key(section, record.id, index), section=section, values=(record.id, *values),
                              owners=(Ref(collection=_NAMES[section], key=record.id),))
        for curve in checkpointed(store.collection("swmm:curves").values()):
            yield row("CURVES", curve, (curve.kind,))
            for index, point in checkpointed(enumerate(curve.points)):
                yield row("CURVES", curve, (number_text(point.x), number_text(point.y)), index)
        for series in checkpointed(store.collection("swmm:timeseries").values()):
            if isinstance(series, FileTimeSeries):
                yield row("TIMESERIES", series, ("FILE", series.file.path))
            elif isinstance(series, InlineTimeSeries):
                for index, point in checkpointed(enumerate(series.points)):
                    if isinstance(point.time, datetime):
                        stamp = point.time
                        values = (f"{stamp.month:02d}/{stamp.day:02d}/{stamp.year:04d}",
                                  _time_text(stamp - datetime.combine(stamp.date(), time())))
                    else:
                        values = (_time_text(point.time),)
                    yield row("TIMESERIES", series, (*values, number_text(point.value)), "header" if index == 0 else index)
            else:
                raise ValueError(f"No time-series writer for {type(series).__name__}")
        for pattern in checkpointed(store.collection("swmm:patterns").values()):
            yield row("PATTERNS", pattern, (pattern.kind,))
            for offset in checkpointed(range(0, len(pattern.factors), 6)):
                yield row("PATTERNS", pattern, tuple(number_text(value) for value in checkpointed(pattern.factors[offset:offset + 6])), offset)

    def validate(self, store, profile):
        options = get_options(store)
        if not ValidationReport(diagnostics=tuple(validate_fields(options))).is_valid:
            return
        try:
            start = datetime.combine(options.start_date or profile.option_default("start_date"), time()) + clock_duration(
                options.start_time if options.start_time is not None else profile.option_default("start_time"))
        except OverflowError:
            yield Diagnostic(code="resource.calendar_overflow", message="Simulation start exceeds the supported calendar range")
            return
        for series in store.collection("swmm:timeseries").values():
            if not isinstance(series, InlineTimeSeries):
                continue
            if not ValidationReport(diagnostics=tuple(validate_fields(series))).is_valid:
                continue
            previous = None
            for index, point in checkpointed(enumerate(series.points)):
                try:
                    stamp = start + point.time if isinstance(point.time, timedelta) else point.time
                except OverflowError:
                    yield Diagnostic(code="resource.calendar_overflow", message="Series time exceeds the supported calendar range",
                                     object_id=series.id, field=f"points[{index}].time")
                    continue
                if previous is not None and stamp <= previous:
                    yield Diagnostic(code="resource.series_order", message="Resolved series times must increase strictly",
                                     object_id=series.id, field=f"points[{index}].time")
                previous = stamp
