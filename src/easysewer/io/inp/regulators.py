"""Pump and regulator syntax; graph ownership remains with NetworkCodec."""

from datetime import timedelta

from ...model import network as n
from ...model.identity import Ref
from ...model.values import Offset
from .formatting import optional_tail
from .geometry import UnsupportedGeometry, finite_number, number_text


def _bool(token, words=("NO", "YES")):
    if token.upper() not in words:
        raise ValueError(f"Expected {'/'.join(words)}: {token}")
    return token.upper() == words[1]


def _offset(token):
    return Offset.NODE_INVERT if token == "*" else finite_number(token)


def _offset_text(value):
    return "*" if value is Offset.NODE_INVERT else number_text(value if value is not None else 0)


def _optional(value, formatter=number_text):
    return None if value is None else formatter(value)


def _yes(value):
    return "YES" if value else "NO"


def parse_regulator_section(values, codec):
    """Only native-active geometry belongs to a regulator's semantic section."""
    known = {"CIRCULAR", "RECT_CLOSED", "RECT_OPEN", "TRIANGULAR", "TRAPEZOIDAL"}
    if not values or values[0].upper() not in known or not 5 <= len(values) <= 7:
        return codec.parse(values), False
    kind = values[0].upper()
    parameters = tuple(finite_number(token) for token in values[1:5])
    active_count = 1 if kind == "CIRCULAR" else 4 if kind == "TRAPEZOIDAL" else 2
    ignored = len(values) > 5 or any(parameters[active_count:])
    normalized = (values[0], *values[1:1 + active_count], *("0",) * (4 - active_count))
    return codec.parse(normalized), ignored


def parse_regulator(section, values):
    issues = []
    if len(values) < 3:
        raise ValueError("Link requires ID, inlet and outlet")
    common = dict(id=values[0], inlet=Ref(collection="swmm:nodes", key=values[1]),
                  outlet=Ref(collection="swmm:nodes", key=values[2]))
    if section == "PUMPS":
        if len(values) > 7:
            raise ValueError("Pump accepts at most seven fields")
        record = n.Pump(**common,
            curve=Ref(collection="swmm:curves", key=values[3]) if len(values) > 3 and values[3] != "*" else None,
            initially_on=_bool(values[4], ("OFF", "ON")) if len(values) > 4 else None,
            startup_depth=finite_number(values[5]) if len(values) > 5 else None,
            shutoff_depth=finite_number(values[6]) if len(values) > 6 else None)
    elif section == "ORIFICES":
        if not 6 <= len(values) <= 8:
            raise ValueError("Orifice requires six fields and up to two optional parameters")
        opening = None
        if len(values) > 7:
            hours = finite_number(values[7])  # Native accepts decimal hours, never a clock token.
            if hours < 0:
                raise ValueError("Opening time must be nonnegative")
            try:
                opening = timedelta(hours=hours)
            except OverflowError:
                raise ValueError("Opening time exceeds timedelta range") from None
            if opening.total_seconds() / 3600 != hours:
                issues.append(("orifice.opening_time_precision", "Opening time was rounded to microseconds; unchanged source retains its original token"))
        record = n.Orifice(**common, orientation=values[3].upper(), offset=_offset(values[4]),
            coefficient=finite_number(values[5]), gated=_bool(values[6]) if len(values) > 6 else None,
            opening_time=opening)
    elif section == "WEIRS":
        if not 6 <= len(values) <= 13:
            raise ValueError("Weir requires six fields and up to seven optional parameters")
        kind = values[3].upper()
        changes = {}
        for index, field, parser in ((6, "gated", _bool), (7, "end_contractions", finite_number),
                                     (8, "end_coefficient", finite_number), (9, "can_surcharge", _bool)):
            changes[field] = parser(values[index]) if len(values) > index and values[index] != "*" else None
        if kind == "ROADWAY":
            changes["road_width"] = finite_number(values[10]) if len(values) > 10 else None
            if len(values) > 11 and values[11] != "*":
                if values[11].upper() not in ("PAVED", "GRAVEL"):
                    raise UnsupportedGeometry(f"Unknown roadway surface: {values[11]}")
                changes["road_surface"] = values[11].upper()
        elif any(token not in ("0", "*") for token in values[10:12]):
            issues.append(("weir.ignored_road_fields", "Road width/surface slots are ignored for this weir type"))
        record = n.Weir(**common, weir_type=kind, crest_height=_offset(values[4]),
            coefficient=finite_number(values[5]), coefficient_curve=Ref(collection="swmm:curves", key=values[12])
            if len(values) > 12 and values[12] != "*" else None, **changes)
    elif section == "OUTLETS":
        if len(values) < 6:
            raise ValueError("Outlet requires at least six fields")
        parts = values[4].upper().split("/")
        if len(parts) > 2 or (len(parts) == 2 and parts[1] not in ("HEAD", "DEPTH")):
            raise UnsupportedGeometry(f"Unsupported outlet relation: {values[4]}")
        basis = parts[1] if len(parts) > 1 else "DEPTH"
        if parts[0] == "FUNCTIONAL":
            if not 7 <= len(values) <= 8:
                raise ValueError("Functional outlet requires coefficient, exponent and optional gate")
            rating = n.FunctionalRating(basis=basis, coefficient=finite_number(values[5]), exponent=finite_number(values[6]))
            end = 7
        elif parts[0] == "TABULAR":
            if not 6 <= len(values) <= 7:
                raise ValueError("Tabular outlet requires curve and optional gate")
            rating = n.TabularRating(basis=basis, curve=Ref(collection="swmm:curves", key=values[5]))
            end = 6
        else:
            raise UnsupportedGeometry(f"Unsupported outlet relation: {values[4]}")
        record = n.Outlet(**common, offset=_offset(values[3]), rating=rating,
                          gated=_bool(values[end]) if len(values) > end else None)
    else:
        raise UnsupportedGeometry(f"Unsupported link section: {section}")
    return record, issues


def regulator_field_layout(record, values):
    """Explicit columns for a successfully parsed pump/regulator record.

    Declaration contribution refers to the structured value. A retained input
    can be hydraulically inactive (e.g. a ROADWAY gate) yet still supply that
    value; only columns actually discarded by this codec have no contribution.
    """
    coverage, assignments = [], []

    def add(path, index, role=None, contributes=True):
        coverage.append(path)
        if index < len(values):
            assignments.append((path, (index,), role or ('marker' if values[index] == '*' else 'value'), contributes))

    def reference(path, index, value):
        add(path, index)
        if value is not None:
            add((*path, 'key'), index)
            add((*path, 'collection'), index, 'derived')

    add(('id',), 0)
    reference(('inlet',), 1, record.inlet)
    reference(('outlet',), 2, record.outlet)
    if type(record) is n.Pump:
        reference(('curve',), 3, record.curve)
        for index, name in enumerate(('initially_on','startup_depth','shutoff_depth'),4):
            add((name,), index)
    elif type(record) is n.Orifice:
        for index, name in enumerate(('orientation','offset','coefficient','gated','opening_time'),3):
            add((name,), index)
    elif type(record) is n.Weir:
        for index, name in enumerate(('weir_type','crest_height','coefficient','gated','end_contractions',
                                      'end_coefficient','can_surcharge','road_width','road_surface'),3):
            ignored = name in ('road_width','road_surface') and record.weir_type != 'ROADWAY'
            add((name,), index, 'retained' if ignored else None, not ignored)
        reference(('coefficient_curve',),12,record.coefficient_curve)
    elif type(record) is n.Outlet:
        add(('offset',),3)
        coverage.extend((('rating',),('rating','basis')))
        if '/' in values[4]:
            add(('rating','basis'),4)
        if type(record.rating) is n.FunctionalRating:
            add(('rating','coefficient'),5)
            add(('rating','exponent'),6)
            end=7
        else:
            reference(('rating','curve'),5,record.rating.curve)
            end=6
        assignments.append((('rating',),tuple(range(4,end)),'value',True))
        add(('gated',),end)
    else:
        raise TypeError('No regulator field syntax declared for this type')
    return tuple(coverage),tuple(assignments)


def format_regulator(record):
    common = (record.inlet.key, record.outlet.key)
    if type(record) is n.Pump:
        return (*common, *optional_tail((record.curve.key if record.curve else None,
            _optional(record.initially_on, lambda value: "ON" if value else "OFF"),
            _optional(record.startup_depth), _optional(record.shutoff_depth)), ("*", "ON", "0", "0")))
    if type(record) is n.Orifice:
        return (*common, record.orientation, _offset_text(record.offset), number_text(record.coefficient),
            *optional_tail((_optional(record.gated, _yes), _optional(record.opening_time,
                lambda value: number_text(value.total_seconds() / 3600))), ("NO", "0")))
    if type(record) is n.Weir:
        return (*common, record.weir_type, _offset_text(record.crest_height), number_text(record.coefficient),
            *optional_tail((_optional(record.gated, _yes), _optional(record.end_contractions),
                _optional(record.end_coefficient), _optional(record.can_surcharge, _yes),
                _optional(record.road_width), record.road_surface,
                record.coefficient_curve.key if record.coefficient_curve else None),
                ("*", "*", "*", "*", "0", "*", "*")))
    if type(record) is n.Outlet:
        rating = record.rating
        if type(rating) is n.FunctionalRating:
            parameters = (number_text(rating.coefficient), number_text(rating.exponent))
        elif type(rating) is n.TabularRating:
            parameters = (rating.curve.key,)
        else:
            raise ValueError(f"No INP writer for {type(rating).__name__}")
        return (*common, _offset_text(record.offset), f"{rating.kind}/{rating.basis}", *parameters,
                *optional_tail((_optional(record.gated, _yes),)))
    raise ValueError(f"No INP writer for {type(record).__name__}")
