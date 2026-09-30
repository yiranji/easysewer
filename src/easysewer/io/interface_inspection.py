"""Read-only checks of fixed 5.2.4 interface formats; no native libraries."""

from dataclasses import dataclass
import math
import struct

from ..model.hydrology import FileRainfall
from ..model.identity import capability_key
from ..validation import Diagnostic, Severity, ValidationReport
from ..validation._cooperative import checkpointed
from .routing import FLOW_UNITS, RoutingInterface


@dataclass(frozen=True, kw_only=True)
class InterfaceInspection:
    format: str
    status: str
    facts: tuple[tuple[str, object], ...] = ()
    report: ValidationReport = ValidationReport()
    required_capabilities: tuple[str, ...] = ()

    def __post_init__(self):
        if type(self.required_capabilities) is not tuple:
            raise TypeError('Inspection capabilities must be an immutable tuple')
        for key in self.required_capabilities:
            capability_key(key)
        if len(set(self.required_capabilities)) != len(self.required_capabilities):
            raise ValueError('Duplicate inspection capability')


def inspect_interface(data, kind, model, *, encoding="utf-8", source=None, manifest=None):
    if manifest is not None:
        from .hotstart_manifest import HotstartManifest
        from .cache_manifest import CacheManifest
        if not (kind == 'HOTSTART' and type(manifest) is HotstartManifest or
                kind in ('RUNOFF','RDII') and type(manifest) is CacheManifest and manifest.kind == kind):
            raise TypeError('Interface manifest does not match the requested format')
    issues, facts, required = [], [], []
    status = "validated"
    def count(namespace, section):
        if namespace in {s.key for s in model._store.specifications}:
            return len(model.collection(namespace))
        if model.document is not None and model.document.records(section):
            return None
        return 0
    def check_count(name, actual, namespace, section):
        expected = count(namespace, section)
        if expected is None:
            issues.append(Diagnostic(code="files.unstructured_dependency", severity=Severity.WARNING,
                message=f"Cannot check {name}: model {section} remains unstructured"))
        elif expected != actual:
            raise ValueError(f"Interface {name} count {actual} differs from model count {expected}")
    def ints(offset, number):
        return struct.unpack_from('<' + 'i'*number, data, offset)
    def units(flag):
        if flag not in range(6) or FLOW_UNITS[flag] != model.units.flow_units:
            raise ValueError("Binary interface flow units do not match the model")
        facts.append(("flow_units", FLOW_UNITS[flag]))
    def finite_record(offset, fmt):
        values = struct.unpack_from(fmt, data, offset)
        if not all(math.isfinite(v) for v in values):
            raise ValueError("Nonfinite interface sample")
        return values
    try:
        if kind in ("INFLOWS", "RDII") and not data.startswith(b'SWMM5-RDII'):
            if manifest is not None:
                raise ValueError('Binary RDII evidence cannot be applied to a text interface')
            document = RoutingInterface.from_bytes(data, encoding=encoding, source=source)
            if not document.frames:
                raise ValueError("Interface contains no complete frames")
            if kind == "RDII" and len(document.constituents) != 1:
                raise ValueError("RDII text interfaces must contain FLOW only")
            for node in document.nodes:
                if node not in model.nodes:
                    if kind == "RDII":
                        raise ValueError(f"RDII interface node {node} is absent from model")
                    issues.append(Diagnostic(code="files.unmatched_interface_node", severity=Severity.WARNING,
                        message=f"Native routing ignores interface node {node} absent from this model"))
            pollutants = count("swmm:pollutants", "POLLUTANTS")
            if len(document.constituents) > 1:
                if pollutants == 0:
                    required.append('easysewer:routing-io:1')
                if pollutants is None or model._store.opaque_constraints:
                    issues.append(Diagnostic(code="files.pollutant_mapping_pending", severity=Severity.WARNING,
                        message="Unstructured target dependencies prevent complete pollutant mapping"))
                    status = "partial"
                else:
                    from ..model.identity import canonical_key
                    for constituent in document.constituents[1:]:
                        if constituent.name not in model.pollutants:
                            issues.append(Diagnostic(code='files.unmatched_interface_pollutant',severity=Severity.WARNING,
                                message=f'Native ignores interface pollutant {constituent.name} absent from this model'))
                        elif model.pollutants[constituent.name].units != constituent.units:
                            raise ValueError(f'Interface concentration units differ for pollutant {constituent.name}')
                    recorded={canonical_key(c.name) for c in document.constituents[1:]}
                    for id in model.pollutants:
                        if canonical_key(id) not in recorded:
                            issues.append(Diagnostic(code='files.missing_interface_pollutant',severity=Severity.WARNING,
                                message=f'Interface has no column for target pollutant {id}; native supplies no interface quality for it'))
            # Text values remain editable doubles. Execution additionally needs
            # finite internal flow and float32-representable consumed results.
            factors = dict(zip(FLOW_UNITS, (1., 448.831, .64632, .02832, 28.317, 2.4466)))
            maximum = 3.4028234663852886e38
            for frame in checkpointed(document.frames):
                for node, row in zip(document.nodes, frame.values):
                    flow = row[0] / factors[document.constituents[0].units]
                    if not math.isfinite(flow):
                        raise ValueError('Interface flow cannot be represented in internal CFS')
                    if node not in model.nodes:
                        continue
                    if abs(flow) > maximum or abs(flow * factors[model.units.flow_units]) > maximum:
                        raise ValueError('Consumed interface flow exceeds native float32 result range')
                    if pollutants is not None:
                        for constituent, value in zip(document.constituents[1:], row[1:]):
                            if constituent.name in model.pollutants and abs(value) > maximum:
                                raise ValueError('Consumed interface quality exceeds native float32 result range')
            facts.extend((("nodes", document.nodes), ("frames", len(document.frames)),
                          ("flow_units", document.constituents[0].units), ("step_seconds", int(document.step.total_seconds()))))
        elif kind == "RUNOFF":
            stamp = b'SWMM5-RUNOFF'
            if not data.startswith(stamp):
                raise ValueError("Invalid runoff interface signature")
            nc, np, flag, steps = ints(len(stamp), 4)
            if nc < 0 or np < 0 or steps <= 0:
                raise ValueError("Invalid runoff interface counts")
            check_count("subcatchment", nc, "swmm:subcatchments", "SUBCATCHMENTS")
            check_count("pollutant", np, "swmm:pollutants", "POLLUTANTS")
            units(flag)
            size = 4 + nc*(8+np)*4
            offset = len(stamp)+16
            if len(data) != offset + steps*size:
                raise ValueError("Runoff interface is truncated or contains trailing records")
            duration = 0.0
            elapsed_ms = 0.0
            for i in checkpointed(range(steps)):
                values = finite_record(offset+i*size, '<'+'f'*(size//4))
                if values[0] <= 0:
                    raise ValueError("Runoff time step must be positive")
                next_ms = elapsed_ms + values[0] * 1000.0
                if next_ms <= elapsed_ms:
                    raise ValueError("Runoff step does not advance the native elapsed clock")
                elapsed_ms = next_ms
                duration += values[0]
            facts.extend((("steps", steps), ("duration_seconds", duration), ("subcatchments", nc)))
            from .runoff_cache import RunoffLayout
            from ._hotstart_model import LayoutUnavailable
            try:
                layout = RunoffLayout.from_model(model)
            except LayoutUnavailable as error:
                layout = None
                issues.append(Diagnostic(code='files.cache_layout_pending', severity=Severity.WARNING, message=str(error)))
            status = 'state_checked' if layout is not None else 'partial'
            if manifest is not None:
                manifest.verify(bytes(data), layout=layout or manifest.layout)
                status = 'validated' if layout is not None else 'partial'
                facts.append(('sha256', manifest.sha256))
                issues.append(Diagnostic(code='files.asserted_identity', severity=Severity.INFO,
                    message='Cache bytes and positional identity match caller-asserted evidence; producer provenance and physical reuse appropriateness are separate checks'))
            else:
                issues.append(Diagnostic(code="files.positional_identity", severity=Severity.WARNING,
                    message="Runoff files contain no subcatchment IDs; caller must establish the original object order"))
        elif kind == "RAINFALL":
            stamp = b'SWMM5-RAIN'
            if not data.startswith(stamp):
                raise ValueError("Invalid rainfall interface signature")
            number, = ints(len(stamp), 1)
            header_size = len(stamp)+4+number*(1025+12)
            if number <= 0 or header_size > len(data) or len(data) > 2147483647:
                raise ValueError("Invalid rainfall interface station count")
            stations = {}
            ranges = []
            for i in checkpointed(range(number)):
                offset = len(stamp)+4+i*1037
                name_bytes = data[offset:offset+1025]
                if b'\0' not in name_bytes:
                    raise ValueError("Unterminated rainfall station ID")
                name = name_bytes.split(b'\0', 1)[0].decode(encoding, errors='strict')
                if not name:
                    raise ValueError("Empty rainfall station ID")
                interval, start, end = ints(offset+1025, 3)
                if interval <= 0 or not header_size <= start <= end <= len(data) or (end-start)%12:
                    raise ValueError("Invalid rainfall interval or record offsets")
                stations.setdefault(name, (interval, start, end))
                if start < end:
                    ranges.append((start % 12, start, end))
            # Shared/aligned overlapping spans have one validation pass.
            # Adjacent stations may restart their calendar and stay separate.
            merged = []
            for phase, start, end in sorted(ranges):
                if merged and phase == merged[-1][0] and start < merged[-1][2]:
                    old_phase, old_start, old_end = merged[-1]
                    merged[-1] = (old_phase, old_start, max(old_end, end))
                else:
                    merged.append((phase, start, end))
            for phase, start, end in merged:
                previous = None
                for position in checkpointed(range(start, end, 12)):
                    stamp_value, value = finite_record(position, '<df')
                    if not -693593 <= stamp_value < 2958466:
                        raise ValueError("Rainfall date exceeds the native calendar range")
                    if previous is not None and stamp_value <= previous:
                        raise ValueError("Rainfall interface times are not increasing")
                    previous = stamp_value
            for gage in model.raingages.values():
                if isinstance(gage.source, FileRainfall):
                    info = stations.get(gage.source.station)
                    if info is None or info[1] == info[2]:
                        raise ValueError(f"Missing/empty rainfall station {gage.source.station}")
            facts.append(("stations", tuple(stations)))
        elif kind == "RDII":
            from .runoff_cache import RdiiData, RdiiLayout
            from ._hotstart_model import LayoutUnavailable
            document = RdiiData.from_bytes(bytes(data))
            if any(i >= len(model.nodes) for i in document.node_indices):
                raise ValueError("RDII file contains out-of-range native node indices")
            try:
                layout = RdiiLayout.from_model(model)
            except LayoutUnavailable as error:
                layout = None
                issues.append(Diagnostic(code='files.cache_layout_pending', severity=Severity.WARNING, message=str(error)))
            if layout is not None:
                document.validate_layout(layout)
            facts.extend((("node_indices", document.node_indices), ("step_seconds", int(document.step.total_seconds())),
                          ('frames', len(document.frames)), ('flow_units', 'CFS')))
            status = 'state_checked' if layout is not None else 'partial'
            if manifest is not None:
                manifest.verify(bytes(data), layout=layout or manifest.layout)
                status = 'validated' if layout is not None else 'partial'
                facts.append(('sha256', manifest.sha256))
                issues.append(Diagnostic(code='files.asserted_identity', severity=Severity.INFO,
                    message='Cache bytes, node order and nodal bindings match caller-asserted evidence; cached rainfall/parameter provenance remains a separate check'))
            else:
                issues.append(Diagnostic(code='files.positional_identity', severity=Severity.WARNING,
                    message='RDII file indices contain no node names; caller must establish the producer node order and bindings'))
        elif kind == "HOTSTART":
            from .hotstart import HotstartData, HotstartLayout, read_header
            from ._hotstart_model import LayoutUnavailable
            data = bytes(data)
            version, values, offset = read_header(data)
            for name, section in (('nodes','JUNCTIONS'), ('links','CONDUITS'), ('pollutants','POLLUTANTS'), ('subcatchments','SUBCATCHMENTS'), ('landuses','LANDUSES')):
                if name in values:
                    check_count(name, values[name], 'swmm:'+name, section)
            units(values['units'])
            facts.extend((("version", version), *((k,v) for k,v in values.items() if k != 'units')))
            try:
                layout = HotstartLayout.from_model(model)
            except LayoutUnavailable as error:
                layout = None
                issues.append(Diagnostic(code="files.hotstart_layout_pending", severity=Severity.WARNING, message=str(error)))
            if layout is not None:
                HotstartData.from_bytes(data, layout=layout)
                facts.extend((("state_bytes", len(data)-offset), ("node_ids", tuple(n.id for n in layout.nodes))))
                status = 'state_checked'
            else:
                if len(data) <= offset:
                    raise ValueError('Hotstart has no state payload')
                status = 'header_only'
            if manifest is not None:
                manifest.verify(data, layout=layout or manifest.layout)
                facts.append(('sha256', manifest.sha256))
                status = 'validated' if layout is not None else 'partial'
                issues.append(Diagnostic(code='files.asserted_identity', severity=Severity.INFO,
                    message='State bytes match the caller-asserted manifest; this is not authenticated producer evidence or a complete simulation checkpoint'))
            else:
                issues.append(Diagnostic(code="files.positional_identity", severity=Severity.WARNING,
                    message="Hotstart files contain no object IDs; state parsing alone cannot establish producer order/types/bindings"))
        else:
            return InterfaceInspection(format=kind, status="unsupported", report=ValidationReport(diagnostics=(Diagnostic(
                code="files.format_not_implemented", severity=Severity.WARNING, message=f"No data-format inspector registered for {kind}"),)))
    except (ValueError, struct.error, OverflowError) as error:
        if hasattr(error, 'report'):
            issues.extend(error.report.diagnostics)
        else:
            issues.append(Diagnostic(code="files.invalid_format", message=str(error), field=source))
        status = "invalid"
    return InterfaceInspection(format=kind, status=status, facts=tuple(facts),
        required_capabilities=tuple(required), report=ValidationReport(diagnostics=tuple(issues)))
