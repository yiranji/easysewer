"""Hydrologic aggregates and their state-sensitive, multi-section input syntax."""

from collections import OrderedDict
from dataclasses import fields, replace
from datetime import timedelta
import math

from ...model import hydrology as h, climate as c
from ...model.fields import validate_fields
from ...model.identity import Ref, canonical_key
from ...model.options import Options
from ...model.values import Point
from ...schema import DecodedFeature, FeatureDescriptor
from ...schema.structured import EncodedRow, FeatureData, RecordEntry, SourceBinding
from ...schema.hydrology_fields import HYDROLOGY_FIELD_RULES
from ...schema.snow_fields import SNOW_FIELD_RULES, ADJUSTMENT_FIELD_RULES
from ...validation import Diagnostic, Severity, SourceSpan, ValidationReport
from .climate import _file
from .durations import DurationCodec
from .geometry import finite_number, number_text
from .options import OptionsCodec, _date, _native_time
from .field_sources import FieldSources
from .hydrology_sources import hydrology_sources
from ...validation._cooperative import checkpointed

_LOCAL = {"INFIL": "infiltration", "DSTORE": "depression_storage", "N-PERV": "pervious_roughness"}
_METHODS = ("HORTON", "MODIFIED_HORTON", "GREEN_AMPT", "MODIFIED_GREEN_AMPT", "CURVE_NUMBER")
_SNOW = {"PLOWABLE": "plowable", "IMPERVIOUS": "impervious", "PERVIOUS": "pervious", "REMOVAL": "removal"}
_NODE_SECTIONS = {"JUNCTIONS", "OUTFALLS", "STORAGE", "DIVIDERS"}
_DURATION = DurationCodec(numeric_unit="hours", resolution=timedelta(seconds=1))


class _Unsupported(ValueError):
    pass


def _count(values, *counts):
    if len(values) not in counts:
        raise ValueError(f"Expected one of these field counts: {counts}; got {len(values)}")


def _key(section, identity, suffix=None):
    result = section, canonical_key(identity)
    return result if suffix is None else (*result, str(suffix))


def parse_infiltration(values, options, profile):
    method = values[-1].upper() if values[-1].upper() in _METHODS else None
    tokens = values[1:-1] if method else values[1:]
    effective = method or options.infiltration or profile.option_default("infiltration")
    values = tuple(finite_number(value) for value in tokens)
    ignored = False
    if effective in ("HORTON", "MODIFIED_HORTON"):
        _count(values, 4, 5)
        parameters = h.Horton(**dict(zip(("maximum_rate", "minimum_rate", "decay", "drying_time", "maximum_volume"), values)))
    elif effective in ("GREEN_AMPT", "MODIFIED_GREEN_AMPT"):
        _count(values, 3)
        parameters = h.GreenAmpt(suction=values[0], conductivity=values[1], initial_deficit=values[2])
    else:
        _count(values, 3)
        parameters = h.CurveNumber(curve_number=values[0], drying_time=values[2])
        ignored = values[1] != 0
    return h.Infiltration(parameters=parameters, method=method), ignored


def format_infiltration(value):
    parameters = value.parameters
    if isinstance(parameters, h.Horton):
        values = tuple(getattr(parameters, field.name) for field in fields(parameters))
        if values[-1] is None:
            values = values[:-1]
    elif isinstance(parameters, h.GreenAmpt):
        values = parameters.suction, parameters.conductivity, parameters.initial_deficit
    elif isinstance(parameters, h.CurveNumber):
        values = parameters.curve_number, 0, parameters.drying_time
    else:
        raise TypeError("No infiltration writer for this parameter type")
    return tuple(number_text(item) for item in values) + ((value.method,) if value.method else ())


class HydrologyCodec:
    def file_uses(self, store, profile):
        from ...model.file_resources import FileUse
        from ...model.options import get_options
        ignore = get_options(store).ignore_rainfall
        reused = ("RAINFALL", "USE") in store.collection("swmm:files") if "swmm:files" in {s.key for s in checkpointed(store.specifications)} else False
        for record in checkpointed(store.collection("swmm:raingages").values()):
            if isinstance(record.source, h.FileRainfall):
                yield FileUse(owner=Ref(collection="swmm:raingages", key=record.id), path=("source", "file"), file=record.source.file,
                    role="swmm:rainfall", format="swmm:rainfall.data", active=not ignore and not reused)

    descriptor = FeatureDescriptor(key="swmm:hydrology", sections=frozenset({
        "RAINGAGES", "SYMBOLS", "SUBCATCHMENTS", "SUBAREAS", "INFILTRATION", "POLYGONS", "SNOWPACKS", "ADJUSTMENTS"}),
        # Numeric edits can replace their source rows without moving unrelated
        # blocks or introducing repeated section headers. Identity/order changes
        # still rebuild the feature through ordered_sections below.
        ordered_sections=frozenset({"RAINGAGES", "SUBCATCHMENTS", "SNOWPACKS"}))
    collections = h.HYDROLOGY_COLLECTIONS + (c.CLIMATE_COLLECTIONS[1],)
    field_rules = HYDROLOGY_FIELD_RULES + SNOW_FIELD_RULES + ADJUSTMENT_FIELD_RULES

    def decode(self, document, profile):
        records, bindings, issues = [], [], []
        source_fields = FieldSources()
        groups = {name: OrderedDict() for name in checkpointed(("raingages", "subcatchments", "snowpacks", "subcatchment_adjustments"))}
        lexical = {issue.span.line for issue in checkpointed(document.report.errors) if issue.span}
        option_rows = OptionsCodec().decode(document, profile).value.records
        options = option_rows[0].value if option_rows else Options()
        node_names = {canonical_key(line.values[0]) for line in checkpointed(document.lines) if line.kind == "data" and line.section in _NODE_SECTIONS}
        catchment_names = {canonical_key(line.values[0]) for line in checkpointed(document.records("SUBCATCHMENTS"))}
        storage_names = {canonical_key(line.values[0]) for line in checkpointed(document.records('STORAGE'))}

        def issue(line, code, message, severity=Severity.ERROR):
            issues.append(Diagnostic(code=code, message=message, severity=severity, section=line.section,
                object_id=line.values[0], span=SourceSpan(source=document.source, line=line.number, column=1, end_column=len(line.content)+1)))

        for line in checkpointed(document.lines):
            if line.kind != "data" or line.section not in self.descriptor.sections:
                continue
            if (line.section == 'POLYGONS' and canonical_key(line.values[0]) in storage_names
                    and canonical_key(line.values[0]) not in catchment_names):
                continue  # The network codec owns storage polygons.
            if line.section == "ADJUSTMENTS":
                if line.values[0].upper() not in _LOCAL:
                    continue
                name = "subcatchment_adjustments"
                identity = line.values[1] if len(line.values) > 1 else "missing"
            else:
                name = "raingages" if line.section in ("RAINGAGES", "SYMBOLS") else "snowpacks" if line.section == "SNOWPACKS" else "subcatchments"
                identity = line.values[0]
            groups[name].setdefault(canonical_key(identity), []).append(line)

        for collection, entities in checkpointed(groups.items()):
            # Declaration order, not the position of a polygon/subarea line,
            # defines native entity identity order.
            primary = {"raingages": "RAINGAGES", "subcatchments": "SUBCATCHMENTS", "snowpacks": "SNOWPACKS"}.get(collection)
            ordered = sorted(entities.values(), key=lambda lines: min((line.number for line in checkpointed(lines) if line.section == primary), default=lines[0].number))
            for lines in checkpointed(ordered):
                if any(line.number in lexical for line in checkpointed(lines)):
                    continue
                line = lines[0]
                local_bindings = []
                try:
                    if collection == "raingages":
                        declarations = [line for line in checkpointed(lines) if line.section == "RAINGAGES"]
                        if len(declarations) != 1:
                            raise ValueError("Rain gage requires exactly one declaration")
                        line = declarations[0]
                        tokens = line.values
                        if len(tokens) < 6:
                            raise ValueError("Rain gage requires at least six fields")
                        form, source_kind = tokens[1].upper(), tokens[4].upper()
                        if source_kind == "TIMESERIES":
                            _count(tokens, 6)
                            source = h.SeriesRainfall(series=Ref(collection="swmm:timeseries", key=tokens[5]))
                        elif source_kind == "FILE":
                            if len(tokens) < 8:
                                raise _Unsupported("Native 5.2.4 requires station and IN/MM fields even for formats where they are ignored; inspect the file and supply these fields")
                            _count(tokens, 8, 9)
                            source = h.FileRainfall(file=_file(tokens[5], document.source), station=tokens[6], units=tokens[7].upper(),
                                start_date=_date(tokens[8]) if len(tokens) == 9 and tokens[8] != "*" else None)
                        else:
                            raise _Unsupported(f"Unsupported rainfall source {source_kind}")
                        if ":" in tokens[2]:
                            interval, changed = _native_time(tokens[2], integer_seconds=True)
                        else:
                            seconds = finite_number(tokens[2]) * 3600
                            native_seconds = math.floor(seconds + .5) if source_kind == "TIMESERIES" else math.trunc(seconds)
                            if not 1 <= native_seconds <= 2_147_483_647:
                                raise ValueError("Rain gage duration is outside the native positive integer range")
                            interval, changed = timedelta(seconds=native_seconds), abs(native_seconds-seconds) > 1e-9
                        if changed:
                            issue(line, "rainfall.interval_coercion", "Native uses whole seconds, rounding series intervals and truncating numeric file intervals", Severity.WARNING)
                        row = h.RainGage(id=tokens[0], form=form, interval=interval, snow_factor=finite_number(tokens[3]), source=source)
                        local_bindings.append(SourceBinding(line=line.number, key=_key("RAINGAGES", row.id)))
                        symbols = [item for item in checkpointed(lines) if item.section == "SYMBOLS"]
                        for line in checkpointed(symbols):
                            if len(line.values) < 3:
                                raise ValueError('Symbol requires ID, X and Y')
                            if len(line.values) > 3:
                                issue(line, 'geometry.ignored_columns', 'GUI ignores trailing coordinate fields; normalization removes them', Severity.WARNING)
                            if row.position is not None:
                                issue(line, "hydrology.repeated_assignment", "Later symbol coordinates take effect", Severity.INFO)
                            row = replace(row, position=Point(x=finite_number(line.values[1]), y=finite_number(line.values[2])))
                            local_bindings.append(SourceBinding(line=line.number, key=_key("SYMBOLS", row.id)))
                    elif collection == "subcatchments":
                        declarations = [line for line in checkpointed(lines) if line.section == "SUBCATCHMENTS"]
                        if len(declarations) != 1:
                            raise ValueError("Subcatchment requires exactly one declaration")
                        line = declarations[0]
                        tokens = line.values
                        _count(tokens, 8, 9)
                        outlet = canonical_key(tokens[2])
                        if outlet in node_names and outlet in catchment_names:
                            raise ValueError("Ambiguous outlet is both a node and subcatchment")
                        namespace = "swmm:subcatchments" if outlet in catchment_names else "swmm:nodes"
                        row = h.Subcatchment(id=tokens[0], rain_gage=Ref(collection="swmm:raingages", key=tokens[1]),
                            outlet=Ref(collection=namespace, key=tokens[2]), **dict(zip(("area", "impervious_percent", "width", "slope", "curb_length"),
                                (finite_number(value) for value in checkpointed(tokens[3:8])))),
                            snowpack=Ref(collection="swmm:snowpacks", key=tokens[8]) if len(tokens) == 9 else None)
                        local_bindings.append(SourceBinding(line=line.number, key=_key("SUBCATCHMENTS", row.id)))
                        for line in checkpointed(lines):
                            tokens = line.values
                            if line.section == "SUBAREAS":
                                if row.subareas is not None:
                                    issue(line, "hydrology.repeated_assignment", "Later subarea parameters take effect", Severity.INFO)
                                _count(tokens, 7, 8)
                                row = replace(row, subareas=h.Subareas(**dict(zip(("impervious_roughness", "pervious_roughness", "impervious_storage", "pervious_storage", "zero_storage_percent"),
                                    (finite_number(value) for value in checkpointed(tokens[1:6])))), route_to=tokens[6].upper(),
                                    routed_percent=finite_number(tokens[7]) if len(tokens) == 8 else None))
                            elif line.section == "INFILTRATION":
                                if row.infiltration is not None:
                                    issue(line, "hydrology.repeated_assignment", "Later infiltration parameters take effect", Severity.INFO)
                                infiltration, ignored = parse_infiltration(tokens, options, profile)
                                row = replace(row, infiltration=infiltration)
                                if ignored:
                                    issue(line, "infiltration.ignored_parameter", "Native ignores the second curve-number parameter; normalization writes zero", Severity.WARNING)
                            elif line.section == "POLYGONS":
                                if len(tokens) < 3:
                                    raise ValueError('Polygon vertex requires ID, X and Y')
                                if len(tokens) > 3:
                                    issue(line, 'geometry.ignored_columns', 'GUI ignores trailing coordinate fields; normalization removes them', Severity.WARNING)
                                row = replace(row, polygon=(*row.polygon, Point(x=finite_number(tokens[1]), y=finite_number(tokens[2]))))
                            else:
                                continue
                            local_bindings.append(SourceBinding(line=line.number, key=_key(line.section, row.id, len(row.polygon)-1 if line.section == "POLYGONS" else None)))
                    elif collection == "snowpacks":
                        row = h.Snowpack(id=lines[0].values[0])
                        for line in checkpointed(lines):
                            tokens = line.values
                            if len(tokens) < 2 or tokens[1].upper() not in _SNOW:
                                raise _Unsupported("Unsupported snowpack parameter group")
                            kind = tokens[1].upper()
                            field = _SNOW[kind]
                            if kind == "REMOVAL":
                                _count(tokens, 7, 8, 9)
                                value = h.SnowRemoval(**dict(zip(("threshold", "out_of_system", "to_impervious", "to_pervious", "immediate_melt", "to_subcatchment"),
                                    (finite_number(token) for token in checkpointed(tokens[2:8])))), destination=Ref(collection="swmm:subcatchments", key=tokens[8]) if len(tokens) == 9 else None)
                                if len(tokens) == 7:
                                    issue(line, "snowpack.native_removal_syntax", "Native requires the Fsub numeric field; normalization writes zero for the documented omitted value", Severity.WARNING)
                            else:
                                _count(tokens, 9)
                                value_type = h.PlowableSnow if kind == "PLOWABLE" else h.DepletableSnow
                                value = value_type(**dict(zip((item.name for item in checkpointed(fields(value_type))), (finite_number(token) for token in checkpointed(tokens[2:])))))
                            if getattr(row, field) is not None:
                                issue(line, "snowpack.repeated_assignment", "Later assignment replaces this snow parameter group", Severity.INFO)
                            row = replace(row, **{field: value})
                            local_bindings.append(SourceBinding(line=line.number, key=_key("SNOWPACKS", row.id, kind)))
                    else:
                        row = None
                        for line in checkpointed(lines):
                            tokens = line.values
                            _count(tokens, 3)
                            field = _LOCAL[tokens[0].upper()]
                            row = row or c.SubcatchmentAdjustments(subcatchment=Ref(collection="swmm:subcatchments", key=tokens[1]))
                            if getattr(row, field) is not None:
                                issue(line, "climate.repeated_assignment", "Later local pattern assignment takes effect", Severity.INFO)
                            row = replace(row, **{field: Ref(collection="swmm:patterns", key=tokens[2])})
                            local_bindings.append(SourceBinding(line=line.number, key=_key("ADJUSTMENTS", row.subcatchment.key, field)))
                    ValidationReport(diagnostics=tuple(validate_fields(row))).raise_for_errors()
                    identity = row.subcatchment.key if type(row) is c.SubcatchmentAdjustments else row.id
                    hydrology_sources(source_fields, Ref(collection=f'swmm:{collection}', key=identity), row, lines, options, profile)
                    records.append(RecordEntry(collection=f"swmm:{collection}", value=row))
                    bindings.extend(local_bindings)
                except (ValueError, TypeError, OverflowError) as error:
                    issue(line, "hydrology.unsupported_input" if isinstance(error, _Unsupported) else "hydrology.invalid_input", str(error),
                          Severity.WARNING if isinstance(error, _Unsupported) else Severity.ERROR)
        originals = {Ref(collection=r.collection, key=r.value.subcatchment.key if type(r.value) is c.SubcatchmentAdjustments else r.value.id).canonical: r.value for r in checkpointed(records)}
        return DecodedFeature(value=FeatureData(records=tuple(records), bindings=tuple(bindings), **source_fields.finish(originals)),
            claimed_lines=frozenset(item.line for item in checkpointed(bindings)), report=ValidationReport(diagnostics=tuple(issues)))

    def encode(self, store, profile):
        from ...schema.structured import FeatureEncoding, OmittedRecord
        rows, omitted = [], []

        def emit(section, collection, identity, values, suffix=None):
            rows.append(EncodedRow(key=_key(section, identity, suffix), section=section, values=tuple(values),
                owners=(Ref(collection=collection, key=identity),)))

        for row in checkpointed(store.collection("swmm:raingages").values()):
            source = row.source
            if isinstance(source, h.SeriesRainfall):
                tail = "TIMESERIES", source.series.key
            elif isinstance(source, h.FileRainfall):
                tail = ("FILE", source.file.path, source.station, source.units) + ((source.start_date.strftime("%m/%d/%Y"),) if source.start_date else ())
            else:
                raise TypeError("No rainfall writer for this source")
            emit("RAINGAGES", "swmm:raingages", row.id, (row.id, row.form, _DURATION.format(row.interval), number_text(row.snow_factor), *tail))
        for row in checkpointed(store.collection("swmm:subcatchments").values()):
            emit("SUBCATCHMENTS", "swmm:subcatchments", row.id, (row.id, row.rain_gage.key, row.outlet.key,
                *(number_text(getattr(row, field)) for field in checkpointed(("area", "impervious_percent", "width", "slope", "curb_length"))),
                *((row.snowpack.key,) if row.snowpack else ())))
        for row in checkpointed(store.collection("swmm:subcatchments").values()):
            if row.subareas is not None:
                a = row.subareas
                emit("SUBAREAS", "swmm:subcatchments", row.id, (row.id, *(number_text(getattr(a, field)) for field in checkpointed((
                    "impervious_roughness", "pervious_roughness", "impervious_storage", "pervious_storage", "zero_storage_percent"))), a.route_to,
                    *((number_text(a.routed_percent),) if a.routed_percent is not None else ())))
            if row.infiltration is not None:
                emit("INFILTRATION", "swmm:subcatchments", row.id, (row.id, *format_infiltration(row.infiltration)))
            for index, point in checkpointed(enumerate(row.polygon)):
                emit("POLYGONS", "swmm:subcatchments", row.id, (row.id, number_text(point.x), number_text(point.y)), index)
        for row in checkpointed(store.collection("swmm:raingages").values()):
            if row.position is not None:
                emit("SYMBOLS", "swmm:raingages", row.id, (row.id, number_text(row.position.x), number_text(row.position.y)))
        for row in checkpointed(store.collection("swmm:snowpacks").values()):
            for kind, field in checkpointed(_SNOW.items()):
                value = getattr(row, field)
                if value is None:
                    continue
                if kind == "REMOVAL":
                    values = tuple(number_text(getattr(value, field) or 0) for field in checkpointed((
                        "threshold", "out_of_system", "to_impervious", "to_pervious", "immediate_melt", "to_subcatchment")))
                    values += (value.destination.key,) if value.destination else ()
                else:
                    values = tuple(number_text(getattr(value, item.name)) for item in checkpointed(fields(value)))
                emit("SNOWPACKS", "swmm:snowpacks", row.id, (row.id, kind, *values), kind)
        for row in checkpointed(store.collection("swmm:subcatchment_adjustments").values()):
            count = 0
            for keyword, field in checkpointed(_LOCAL.items()):
                value = getattr(row, field)
                if value is not None:
                    emit("ADJUSTMENTS", "swmm:subcatchment_adjustments", row.subcatchment.key, (keyword, row.subcatchment.key, value.key), field)
                    count += 1
            if not count:
                omitted.append(OmittedRecord(owner=Ref(collection="swmm:subcatchment_adjustments", key=row.subcatchment.key), reason="No local adjustment patterns"))
        return FeatureEncoding(rows=tuple(rows), omitted=tuple(omitted))

    def validate(self, store, profile):
        return h.validate_hydrology(store, profile)

    def validate_run(self, store, profile):
        return h.validate_hydrology(store, profile, for_run=True)

    def resource_uses(self, store, profile):
        yield from h.hydrology_resource_uses(store)
        yield from c.subcatchment_resource_uses(store)

    def validate_document(self, document, profile):
        declarations = {canonical_key(line.values[0]): line.number for line in document.records("SUBCATCHMENTS")}
        for line in document.lines:
            if line.kind != "data":
                continue
            message, code = None, None
            if line.section == "RAINGAGES" and len(line.values) > 4 and line.values[4].upper() == "FILE" and len(line.values) < 8:
                code, message = "rainfall.native_file_fields", "Native FILE gages require station and IN/MM fields; inspect the external format and set them explicitly"
            elif line.section == "SUBAREAS" and declarations.get(canonical_key(line.values[0]), 0) > line.number:
                code, message = "subcatchment.native_order", "SUBAREAS before its SUBCATCHMENTS declaration uses uninitialized impervious area; explicitly normalize the hydrology group"
            elif line.section == "ADJUSTMENTS" and line.values[0].upper() in _LOCAL and len(line.values) >= 2 and declarations.get(canonical_key(line.values[1]), 0) > line.number:
                code, message = "subcatchment.native_order", "A later SUBCATCHMENTS declaration resets local adjustment patterns; explicitly normalize the hydrology group"
            elif line.section == "SNOWPACKS" and len(line.values) == 7 and line.values[1].upper() == "REMOVAL":
                code, message = "snowpack.native_removal_syntax", "Native requires Fsub even when the manual omits it; explicitly normalize to write zero"
            if code:
                yield Diagnostic(code=code, message=message, section=line.section, span=SourceSpan(source=document.source,
                    line=line.number, column=1, end_column=len(line.content)+1))

    def requires_atomic_write(self, document, profile):
        # Preserve the existing repair behavior for source-dependent native
        # ordering and legacy syntax, without rebuilding already valid blocks
        # for an unrelated numeric edit.
        return any(issue.severity == Severity.ERROR for issue in self.validate_document(document, profile))
