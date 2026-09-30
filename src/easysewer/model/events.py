"""Ordered hydraulic event periods; these change the routing calculation."""

from dataclasses import dataclass
from datetime import datetime

from .fields import validate_fields
from .store import CollectionSpec
from ..validation import Diagnostic


@dataclass(frozen=True, kw_only=True)
class RoutingEvent:
    start: datetime
    end: datetime

    def validate_local(self):
        for name in ('start', 'end'):
            if getattr(self, name).tzinfo is not None:
                yield Diagnostic(code='events.timezone', field=name,
                    message='SWMM event times are local model times without a timezone')
        if self.start.tzinfo is None and self.end.tzinfo is None and self.start >= self.end:
            yield Diagnostic(code='events.interval', message='Event start must precede its end')


@dataclass(frozen=True, kw_only=True)
class EventSchedule:
    # INP events have no IDs. One stable schedule owns an ordered sequence;
    # positions are positions, not invented persistent object identities.
    periods: tuple[RoutingEvent, ...] = ()


EVENTS_COLLECTION = CollectionSpec(key='swmm:events', record_type=EventSchedule,
    key_of=lambda _: 'schedule', validate=validate_fields)
