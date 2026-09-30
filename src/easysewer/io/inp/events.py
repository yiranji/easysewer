"""SWMM 5.2.4 input.c/readEvent and routing.c/sortEvents semantics."""

from datetime import datetime, time

from ...model.events import RoutingEvent, EventSchedule, EVENTS_COLLECTION
from ...model.fields import validate_fields
from ...model.identity import Ref
from ...schema import DecodedFeature, FeatureDescriptor
from ...schema.context_fields import EVENT_FIELD_RULES
from ...schema.structured import (
    EncodedRow, FeatureData, FeatureEncoding, OmittedRecord, RecordEntry, SourceBinding,
)
from ...validation import Diagnostic, Severity, SourceSpan, ValidationReport
from .options import _date
from .resources import _series_time, _time_text
from .field_sources import FieldSources
from ...validation._cooperative import checkpointed


_OWNER = Ref(collection='swmm:events', key='schedule')


class EventsCodec:
    descriptor = FeatureDescriptor(key='swmm:events', sections=frozenset({'EVENTS'}),
        atomic_write=True, ordered_sections=frozenset({'EVENTS'}))
    collections = (EVENTS_COLLECTION,)
    field_rules = EVENT_FIELD_RULES

    def decode(self, document, profile):
        periods, bindings, issues = [], [], []
        sources = FieldSources()
        sources.cover(_OWNER, ('periods',))
        lexical = {d.span.line for d in checkpointed(document.report.errors) if d.span}
        for line in checkpointed(document.records('EVENTS')):
            span = SourceSpan(source=document.source, line=line.number,
                column=1, end_column=len(line.content) + 1)
            if line.number in lexical:
                sources.block_value(_OWNER, ('periods',))
                continue
            try:
                if len(line.values) < 4:
                    raise ValueError('EVENTS requires start date/time and end date/time')
                values, coerced = [], False
                for index in checkpointed((0, 2)):
                    duration, changed = _series_time(line.values[index + 1])
                    values.append(datetime.combine(_date(line.values[index]), time()) + duration)
                    coerced |= changed
                event = RoutingEvent(start=values[0], end=values[1])
                ValidationReport(diagnostics=tuple(validate_fields(event))).raise_for_errors()
                index = len(periods)
                sources.cover(_OWNER, ('periods', index))
                sources.add(_OWNER, ('periods',), line, range(4), role='derived', overwrite=False)
                sources.add(_OWNER, ('periods', index), line, range(4), role='derived')
                for name, tokens in checkpointed((('start', (0, 1)), ('end', (2, 3)))):
                    path = ('periods', index, name)
                    sources.cover(_OWNER, path)
                    sources.add(_OWNER, path, line, tokens, role='derived')
                bindings.append(SourceBinding(line=line.number, key=('EVENTS', str(len(periods)))))
                periods.append(event)
                if coerced:
                    issues.append(Diagnostic(code='events.native_time', section='EVENTS', span=span,
                        severity=Severity.WARNING,
                        message='Native clock fractions are truncated; decimal hours use model microsecond resolution'))
                if len(line.values) > 4:
                    issues.append(Diagnostic(code='events.ignored_columns', section='EVENTS', span=span,
                        severity=Severity.WARNING, message='Native EVENTS ignores trailing fields; normalization removes them'))
            except (ValueError, TypeError, OverflowError) as error:
                sources.block_value(_OWNER, ('periods',))
                issues.append(Diagnostic(code='events.invalid_input', section='EVENTS', span=span,
                    message=str(error)))
        records = (RecordEntry(collection='swmm:events', value=EventSchedule(periods=tuple(periods))),) if periods else ()
        owners = {_OWNER.canonical: records[0].value} if records else {}
        return DecodedFeature(value=FeatureData(records=records, bindings=tuple(bindings), **sources.finish(owners)),
            claimed_lines=frozenset(b.line for b in checkpointed(bindings)), report=ValidationReport(diagnostics=tuple(issues)))

    def encode(self, store, profile):
        schedule = store.collection('swmm:events').get('schedule')
        if schedule is None:
            return FeatureEncoding()
        if not schedule.periods:
            return FeatureEncoding(omitted=(OmittedRecord(owner=_OWNER, reason='No routing event restrictions'),))
        rows = []
        for index, event in checkpointed(enumerate(schedule.periods)):
            values = []
            for value in checkpointed((event.start, event.end)):
                values.extend((f'{value.month:02d}/{value.day:02d}/{value.year:04d}',
                    _time_text(value - datetime.combine(value.date(), time()))))
            rows.append(EncodedRow(key=('EVENTS', str(index)), section='EVENTS',
                values=tuple(values), owners=(_OWNER,)))
        return FeatureEncoding(rows=tuple(rows))

    def validate(self, store, profile):
        schedule = store.collection('swmm:events').get('schedule')
        if schedule is None or not ValidationReport(diagnostics=tuple(validate_fields(schedule))).is_valid:
            return
        ordered = sorted(schedule.periods, key=lambda event: event.start)
        if tuple(ordered) != schedule.periods:
            yield Diagnostic(code='events.native_order', section='EVENTS', severity=Severity.INFO,
                message='Source event order is retained; the engine sorts by start time at startup')
        if any(a.end > b.start for a, b in zip(ordered, ordered[1:])):
            yield Diagnostic(code='events.overlap', section='EVENTS', severity=Severity.WARNING,
                message='The engine clips each overlapping event end to the next event start; it does not merge intervals')
