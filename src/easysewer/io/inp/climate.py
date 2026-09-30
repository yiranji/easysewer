"""State-aware climate configuration, evaporation and adjustment codecs."""

from dataclasses import fields, replace
from pathlib import Path, PureWindowsPath

from ...model import climate as c
from ...model.fields import validate_fields
from ...model.identity import Ref, canonical_key
from ...model.values import FileReference
from ...schema.registry import DecodedFeature, FeatureDescriptor
from ...schema.structured import EncodedRow, FeatureData, FeatureEncoding, OmittedRecord, RecordEntry, SourceBinding
from ...validation import Diagnostic, Severity, SourceSpan, ValidationReport
from .formatting import optional_tail
from .geometry import finite_number, number_text
from .options import _date
from .field_sources import FieldSources
from .climate_sources import climate_sources
from ...schema.climate_fields import CLIMATE_FIELD_RULES
from ...validation._cooperative import checkpointed

_OWNER = Ref(collection="swmm:climate", key="settings")
_LOCAL_FIELDS = {"INFIL": "infiltration", "DSTORE": "depression_storage", "N-PERV": "pervious_roughness"}
_ADJUST_FIELDS = {"TEMPERATURE": ("temperature", c.MonthlyTemperatureChanges),
                  "EVAPORATION": ("evaporation", c.MonthlyEvaporation),
                  "RAINFALL": ("rainfall", c.MonthlyFactors), "CONDUCTIVITY": ("conductivity", c.MonthlyFactors)}


class _Unsupported(ValueError):
    pass


def _count(tokens, *allowed):
    if len(tokens) not in allowed:
        raise ValueError(f"{tokens[0]} expects one of these field counts: {allowed}")


def _yes(token):
    if token.upper() not in ("YES", "NO"):
        raise ValueError("Expected YES or NO")
    return token.upper() == "YES"


def _file(path, source):
    windows = bool(PureWindowsPath(path).drive or (source and PureWindowsPath(source).drive))
    base = str((PureWindowsPath if windows else Path)(source).parent) if source else None
    return FileReference(path=path, base_directory=base, flavor="windows" if windows else "native")


def _adjust_keyword(token):
    token = token.upper()
    for prefix, full in (("TEMP", "TEMPERATURE"), ("EVAP", "EVAPORATION"), ("RAIN", "RAINFALL"), ("CONDUCT", "CONDUCTIVITY")):
        if token.startswith(prefix):
            return full
    return token


class ClimateCodec:
    def file_uses(self, store, profile):
        from ...model.file_resources import FileUse
        settings = store.collection("swmm:climate").get("settings")
        if settings is not None and settings.file is not None:
            yield FileUse(owner=_OWNER, path=("file", "file"), file=settings.file.file,
                          role="swmm:climate", format="swmm:climate.data")

    descriptor = FeatureDescriptor(key="swmm:climate", sections=frozenset({"TEMPERATURE", "EVAPORATION", "ADJUSTMENTS"}), atomic_write=True)
    collections = (c.CLIMATE_COLLECTIONS[0],)
    unit_transforms = c.CLIMATE_UNIT_TRANSFORMS
    field_rules = CLIMATE_FIELD_RULES

    def decode(self, document, profile):
        issues, records, bindings = [], [], []
        source_fields = FieldSources()
        lexical_errors = {issue.span.line for issue in checkpointed(document.report.errors) if issue.span}

        def issue(line, code, message, severity=Severity.ERROR):
            issues.append(Diagnostic(code=code, message=message, severity=severity, section=line.section,
                span=SourceSpan(source=document.source, line=line.number, column=1, end_column=len(line.content) + 1)))

        global_lines = []
        for line in checkpointed(document.lines):
            if line.kind != "data" or line.section not in self.descriptor.sections:
                continue
            if line.section == "ADJUSTMENTS" and line.values[0].upper() in _LOCAL_FIELDS:
                # The hydrology feature owns local assignments so moving a
                # SUBCATCHMENTS declaration cannot silently reset its patterns.
                continue
            else:
                global_lines.append(line)

        climate, global_bindings, seen = c.Climate(), [], set()
        failed = any(line.number in lexical_errors for line in checkpointed(global_lines))
        for line in checkpointed(global_lines):
            if line.number in lexical_errors:
                continue
            tokens, section = line.values, line.section
            keyword = tokens[0].upper()
            try:
                if section == "TEMPERATURE":
                    if keyword == "TIMESERIES":
                        _count(tokens, 2)
                        climate = replace(climate, temperature=c.SeriesTemperature(series=Ref(collection="swmm:timeseries", key=tokens[1])))
                        field = "temperature"
                    elif keyword == "FILE":
                        _count(tokens, 2, 3, 4)
                        file = c.ClimateFile(file=_file(tokens[1], document.source),
                            start_date=_date(tokens[2]) if len(tokens) > 2 and tokens[2] != "*" else None,
                            units=tokens[3].upper() if len(tokens) > 3 else None)
                        climate = replace(climate, file=file, temperature=c.FileTemperature())
                        field = "file"
                    elif keyword == "WINDSPEED":
                        if len(tokens) < 2:
                            raise ValueError("WINDSPEED requires MONTHLY values or FILE")
                        if tokens[1].upper() == "FILE":
                            _count(tokens, 2)
                            wind = c.FileWind()
                        elif tokens[1].upper() == "MONTHLY":
                            _count(tokens, 14)
                            wind = c.MonthlyWindSpeeds(values=tuple(finite_number(token) for token in checkpointed(tokens[2:])))
                        else:
                            raise _Unsupported(f"Unsupported wind source: {tokens[1]}")
                        climate = replace(climate, wind=wind)
                        field = "wind"
                    elif keyword == "SNOWMELT":
                        _count(tokens, 7)
                        climate = replace(climate, snowmelt=c.Snowmelt(**dict(zip(
                            (field.name for field in checkpointed(fields(c.Snowmelt))), (finite_number(token) for token in checkpointed(tokens[1:]))))))
                        field = "snowmelt"
                    elif keyword == "ADC":
                        _count(tokens, 12)
                        if tokens[1].upper().startswith("IMPERV"):
                            field = "impervious_depletion"
                        elif tokens[1].upper().startswith("PERV"):
                            field = "pervious_depletion"
                        else:
                            raise _Unsupported(f"Unsupported depletion area: {tokens[1]}")
                        climate = replace(climate, **{field: c.ArealDepletion(fractions=tuple(finite_number(token) for token in checkpointed(tokens[2:])))})
                    else:
                        raise _Unsupported(f"Unsupported temperature keyword: {keyword}")
                elif section == "EVAPORATION":
                    evap = climate.evaporation or c.Evaporation()
                    field = "source"
                    if keyword == "CONSTANT":
                        _count(tokens, 2)
                        evap = replace(evap, source=c.ConstantEvaporation(rate=finite_number(tokens[1])))
                    elif keyword == "MONTHLY":
                        _count(tokens, 13)
                        evap = replace(evap, source=c.MonthlyEvaporation(values=tuple(finite_number(token) for token in checkpointed(tokens[1:]))))
                    elif keyword == "TIMESERIES":
                        _count(tokens, 2)
                        evap = replace(evap, source=c.SeriesEvaporation(series=Ref(collection="swmm:timeseries", key=tokens[1])))
                    elif keyword == "TEMPERATURE":
                        _count(tokens, 1)
                        evap = replace(evap, source=c.TemperatureEvaporation())
                    elif keyword == "FILE":
                        _count(tokens, 1, 13)
                        evap = replace(evap, source=c.FileEvaporation(pan_coefficients=c.MonthlyFactors(
                            values=tuple(finite_number(token) for token in checkpointed(tokens[1:]))) if len(tokens) > 1 else None))
                        if len(tokens) == 1:
                            issue(line, "climate.native_file_evap_syntax", "Native 5.2.4 rejects bare FILE despite the manual; normalize to twelve default pan coefficients", Severity.WARNING)
                    elif keyword == "RECOVERY":
                        _count(tokens, 2)
                        field = "recovery_pattern"
                        evap = replace(evap, recovery_pattern=Ref(collection="swmm:patterns", key=tokens[1]))
                    elif keyword == "DRY_ONLY":
                        _count(tokens, 2)
                        field = "dry_only"
                        evap = replace(evap, dry_only=_yes(tokens[1]))
                    else:
                        raise _Unsupported(f"Unsupported evaporation keyword: {keyword}")
                    climate = replace(climate, evaporation=evap)
                else:
                    keyword = _adjust_keyword(keyword)
                    if keyword not in _ADJUST_FIELDS:
                        raise _Unsupported(f"Unsupported adjustment keyword: {keyword}")
                    _count(tokens, 13)
                    field, value_type = _ADJUST_FIELDS[keyword]
                    climate = replace(climate, adjustments=replace(climate.adjustments or c.ClimateAdjustments(),
                        **{field: value_type(values=tuple(finite_number(token) for token in checkpointed(tokens[1:])))}))
                ValidationReport(diagnostics=tuple(validate_fields(climate))).raise_for_errors()
                key = section, field
                if key in seen or (section == "TEMPERATURE" and field in ("file", "temperature") and
                                   ("TEMPERATURE", "file" if field == "temperature" else "temperature") in seen):
                    issue(line, "climate.repeated_assignment", "Later assignment determines the active value/source; a climate file remains available to other consumers", Severity.INFO)
                seen.add(key)
                global_bindings.append(SourceBinding(line=line.number, key=key))
            except (ValueError, TypeError, OverflowError) as error:
                failed = True
                issue(line, "climate.unsupported_input" if isinstance(error, _Unsupported) else "climate.invalid_input", str(error),
                      Severity.WARNING if isinstance(error, _Unsupported) else Severity.ERROR)
        if global_lines and not failed:
            climate_sources(source_fields, _OWNER, climate, global_lines)
            records.append(RecordEntry(collection="swmm:climate", value=climate))
            # A FILE following TIMESERIES replaces that source. Bind the
            # superseded declaration to the effective FILE row in this section.
            for binding in checkpointed(global_bindings):
                if binding.key == ("TEMPERATURE", "temperature") and isinstance(climate.temperature, c.FileTemperature):
                    binding = replace(binding, key=("TEMPERATURE", "file"))
                bindings.append(binding)

        originals = {_OWNER.canonical: climate} if records else {}
        return DecodedFeature(value=FeatureData(records=tuple(records), bindings=tuple(bindings), **source_fields.finish(originals)),
                              claimed_lines=frozenset(item.line for item in checkpointed(bindings)), report=ValidationReport(diagnostics=tuple(issues)))

    def encode(self, store, profile):
        rows, omitted = [], []

        def emit(section, field, values):
            rows.append(EncodedRow(key=(section, field), section=section, values=tuple(values), owners=(_OWNER,)))

        if store.collection("swmm:climate"):
            climate = c.get_climate(store)
            # FILE always precedes TIMESERIES when both exist: one declares
            # shared data, the second selects the final air-temperature source.
            if climate.file is not None:
                file = climate.file
                emit("TEMPERATURE", "file", ("FILE", file.file.path, *optional_tail((
                    file.start_date.strftime("%m/%d/%Y") if file.start_date else None, file.units), ("*", ""))))
            if isinstance(climate.temperature, c.SeriesTemperature):
                emit("TEMPERATURE", "temperature", ("TIMESERIES", climate.temperature.series.key))
            if climate.wind is not None:
                emit("TEMPERATURE", "wind", ("WINDSPEED", "FILE") if isinstance(climate.wind, c.FileWind)
                     else ("WINDSPEED", "MONTHLY", *(number_text(value) for value in checkpointed(climate.wind.values))))
            if climate.snowmelt is not None:
                emit("TEMPERATURE", "snowmelt", ("SNOWMELT", *(number_text(getattr(climate.snowmelt, field.name)) for field in checkpointed(fields(c.Snowmelt)))))
            for field, name in checkpointed((("impervious_depletion", "IMPERVIOUS"), ("pervious_depletion", "PERVIOUS"))):
                adc = getattr(climate, field)
                if adc is not None:
                    emit("TEMPERATURE", field, ("ADC", name, *(number_text(value) for value in checkpointed(adc.fractions))))
            evap = climate.evaporation
            if evap is not None:
                source = evap.source
                if isinstance(source, c.ConstantEvaporation):
                    values = ("CONSTANT", number_text(source.rate))
                elif isinstance(source, c.MonthlyEvaporation):
                    values = ("MONTHLY", *(number_text(value) for value in checkpointed(source.values)))
                elif isinstance(source, c.SeriesEvaporation):
                    values = ("TIMESERIES", source.series.key)
                elif isinstance(source, c.TemperatureEvaporation):
                    values = ("TEMPERATURE",)
                elif isinstance(source, c.FileEvaporation):
                    # The documented bare FILE form fails native's item check.
                    values = ("FILE", *(number_text(value) for value in (checkpointed(source.pan_coefficients.values if source.pan_coefficients else (1.0,) * 12))))
                else:
                    values = ()
                if values:
                    emit("EVAPORATION", "source", values)
                if evap.recovery_pattern is not None:
                    emit("EVAPORATION", "recovery_pattern", ("RECOVERY", evap.recovery_pattern.key))
                if evap.dry_only is not None:
                    emit("EVAPORATION", "dry_only", ("DRY_ONLY", "YES" if evap.dry_only else "NO"))
            if climate.adjustments is not None:
                for keyword, (field, _) in checkpointed(_ADJUST_FIELDS.items()):
                    value = getattr(climate.adjustments, field)
                    if value is not None:
                        emit("ADJUSTMENTS", field, (keyword, *(number_text(item) for item in checkpointed(value.values))))
            if not rows:
                omitted.append(OmittedRecord(owner=_OWNER, reason="No climate settings are explicitly set"))
        return FeatureEncoding(rows=tuple(rows), omitted=tuple(omitted))

    def validate(self, store, profile):
        return c.validate_climate(store, profile)

    def validate_run(self, store, profile):
        return c.validate_climate(store, profile, for_run=True)

    def resource_uses(self, store, profile):
        return c.climate_resource_uses(store)

    def validate_document(self, document, profile):
        series = {canonical_key(line.values[0]) for line in document.records("TIMESERIES")}
        patterns = {canonical_key(line.values[0]) for line in document.records("PATTERNS")}
        if not document.records("SUBCATCHMENTS") and any(line.values[0].upper() == "FILE" for line in document.records("TEMPERATURE")):
            yield Diagnostic(code="climate.native_file_without_runoff", severity=Severity.WARNING, section="TEMPERATURE",
                message="Native 5.2.4 can leave the climate file open without subcatchments; use process isolation until backend lifecycle handling is available. OUT climate fields also require runoff output")
        for line in document.lines:
            if line.kind != "data" or line.section not in ("TEMPERATURE", "EVAPORATION"):
                continue
            code, message = None, None
            if line.section == "EVAPORATION" and len(line.values) == 1 and line.values[0].upper() == "FILE":
                code, message = "climate.native_file_evap_syntax", "Normalize bare evaporation FILE to twelve pan coefficients before running"
            elif len(line.values) >= 2 and line.values[0].upper() in ("TIMESERIES", "RECOVERY"):
                names = series if line.values[0].upper() == "TIMESERIES" else patterns
                if canonical_key(line.values[1]) not in names:
                    code, message = "climate.source_missing_reference", "Even an overridden native source declaration must reference an existing resource; normalize or repair it"
            if code:
                yield Diagnostic(code=code, message=message, section=line.section, span=SourceSpan(
                    source=document.source, line=line.number, column=1, end_column=len(line.content) + 1))
