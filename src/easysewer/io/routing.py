"""Strict, editable SWMM routing/RDII text interfaces with source preservation."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
import math
from pathlib import Path

from ..model.identity import canonical_key, validate_identifier
from ..validation import Diagnostic, SourceSpan, ValidationReport
from ..validation._cooperative import checkpoint, checkpointed
from ._atomic import write_bytes

FLOW_UNITS = ("CFS", "GPM", "MGD", "CMS", "LPS", "MLD")


@dataclass(frozen=True, kw_only=True)
class InterfaceConstituent:
    name: str
    units: str


@dataclass(frozen=True, kw_only=True)
class InterfaceFrame:
    time: datetime
    values: tuple[tuple[float, ...], ...]


@dataclass(frozen=True, kw_only=True)
class RoutingInterface:
    step: timedelta
    constituents: tuple[InterfaceConstituent, ...]
    nodes: tuple[str, ...]
    frames: tuple[InterfaceFrame, ...]
    title: str = ""
    _original: bytes | None = field(default=None, init=False, repr=False, compare=False)
    _encoding: str = field(default="utf-8", init=False, repr=False, compare=False)

    def __post_init__(self):
        if type(self.step) is not timedelta or self.step.microseconds or not 0 < self.step.total_seconds() <= 2147483647:
            raise ValueError("Routing time step must be a positive native whole-second duration")
        if not isinstance(self.title, str) or any(c in self.title for c in '\r\n\x00'):
            raise ValueError("Interface title must be one physical line")
        if any(type(v) is not tuple for v in (self.constituents, self.nodes, self.frames)):
            raise TypeError("Interface collections must be immutable tuples")
        if any(not isinstance(c, InterfaceConstituent) for c in self.constituents) or any(not isinstance(f, InterfaceFrame) for f in self.frames):
            raise TypeError("Interface constituents and frames require their declared value types")
        if not self.constituents or self.constituents[0].name != "FLOW" or self.constituents[0].units not in FLOW_UNITS:
            raise ValueError("First interface constituent must be FLOW with declared flow units")
        for i, constituent in enumerate(self.constituents):
            validate_identifier(constituent.name)
            if i and constituent.units not in ("MG/L", "UG/L", "#/L"):
                raise ValueError("Unknown water quality concentration units")
        if len({canonical_key(c.name) for c in self.constituents}) != len(self.constituents):
            raise ValueError("Duplicate interface constituent")
        if not self.nodes or len({canonical_key(n) for n in self.nodes}) != len(self.nodes):
            raise ValueError("Interface requires unique node IDs")
        for node in self.nodes:
            validate_identifier(node)
        previous = None
        for frame in checkpointed(self.frames):
            if type(frame.time) is not datetime or frame.time.tzinfo is not None or frame.time.microsecond or frame.time.fold:
                raise ValueError("Interface timestamps require local whole-second datetimes")
            if previous is not None and frame.time <= previous:
                raise ValueError("Interface frames must be strictly chronological")
            previous = frame.time
            if type(frame.values) is not tuple or len(frame.values) != len(self.nodes):
                raise ValueError("Each frame requires one value row per declared node")
            for row in frame.values:
                if type(row) is not tuple or len(row) != len(self.constituents):
                    raise ValueError("Each node row requires all declared constituent values")
                if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in row):
                    raise ValueError("Interface values must be finite numbers")

    @classmethod
    def from_bytes(cls, data, *, encoding="utf-8", source=None):
        if not isinstance(data, bytes):
            raise TypeError("Routing input must be immutable bytes")
        if not data.startswith(b'SWMM5'):
            raise ValueError("Native routing interfaces require a BOM-free ASCII-compatible signature")
        lines = [line[:-1] if line.endswith('\r') else line for line in data.decode(encoding, errors="strict").split('\n')]
        index = 0
        def line():
            nonlocal index
            if index % 256 == 0:
                checkpoint()
            if index >= len(lines):
                raise ValueError("Incomplete routing interface")
            value = lines[index]; index += 1
            if '\x00' in value or '\r' in value or len(value.encode(encoding)) >= 1023:
                raise ValueError("Interface line violates native physical line limits")
            return value
        def count():
            value = int(line().split()[0])
            if not 0 < value <= len(lines):
                raise ValueError("Declared count exceeds the available interface lines")
            return value
        try:
            if line().split()[0] != "SWMM5":
                raise ValueError("Missing SWMM5 text interface signature")
            title = line()
            step = timedelta(seconds=int(line().split()[0]))
            constituents = tuple(InterfaceConstituent(name=(tokens := line().split())[0], units=tokens[1]) for _ in range(count()))
            nodes = tuple(line().split()[0] for _ in range(count()))
            line()  # The native format has a required heading line, possibly blank.
            frames = []
            while index < len(lines):
                checkpoint()
                if not lines[index].strip() and all(not s.strip() for s in lines[index:]):
                    break
                values, stamp = [], None
                for node in nodes:
                    tokens = line().split()
                    if len(tokens) != 7 + len(constituents):
                        raise ValueError("Incomplete/extra interface data columns")
                    if canonical_key(tokens[0]) != canonical_key(node):
                        raise ValueError("Rows must match declared node order and identity")
                    current = datetime(*(int(t) for t in tokens[1:7]))
                    if stamp is not None and current != stamp:
                        raise ValueError("All node rows in a frame must share a timestamp")
                    stamp = current
                    values.append(tuple(float(t) for t in tokens[7:]))
                frames.append(InterfaceFrame(time=stamp, values=tuple(values)))
            result = cls(step=step, title=title, constituents=constituents, nodes=nodes, frames=tuple(frames))
        except (ValueError, IndexError, OverflowError) as error:
            number = max(1, min(index, len(lines)))
            ValidationReport(diagnostics=(Diagnostic(code="interface.invalid_text", message=str(error),
                span=SourceSpan(source=source, line=number, column=1, end_column=len(lines[number-1])+1 if lines else 1)),)).raise_for_errors()
        object.__setattr__(result, '_original', data)
        object.__setattr__(result, '_encoding', encoding)
        return result

    @classmethod
    def read(cls, path, *, encoding="utf-8"):
        path = Path(path)
        return cls.from_bytes(path.read_bytes(), encoding=encoding, source=str(path))

    def to_bytes(self, *, encoding=None, normalize=False):
        encoding = encoding or self._encoding
        if self._original is not None and not normalize and encoding == self._encoding:
            return self._original
        lines = ["SWMM5 Interface File", self.title, str(int(self.step.total_seconds())), str(len(self.constituents)),
                 *(c.name + ' ' + c.units for c in self.constituents), str(len(self.nodes)), *self.nodes,
                 'Node Year Mon Day Hr Min Sec ' + ' '.join(c.name for c in self.constituents)]
        for frame in self.frames:
            stamp = frame.time.strftime('%Y %m %d %H %M %S')
            lines.extend(f"{node} {stamp} " + ' '.join(repr(float(x)) for x in values) for node, values in zip(self.nodes, frame.values))
        if any(len(line.encode(encoding)) >= 1023 for line in lines):
            raise ValueError("Interface row exceeds the native line buffer")
        data = ('\n'.join(lines) + '\n').encode(encoding)
        if not data.startswith(b'SWMM5 '):
            raise ValueError("Native routing interfaces require a BOM-free ASCII-compatible encoding")
        return data

    def write(self, path, *, encoding=None, normalize=False):
        return write_bytes(path, self.to_bytes(encoding=encoding, normalize=normalize))
