"""Persistable execution settings, separate from simulation model options."""

import codecs
from dataclasses import dataclass, field, fields, replace
from datetime import timedelta
import re

from ..model.identity import capability_key, namespace_key
from ..model.values import FileReference
from ..validation import Diagnostic, ValidationReport


@dataclass(frozen=True, kw_only=True)
class ReportReadOptions:
    encoding: str | None = None
    on_decode_error: str = "preserve"
    tables: tuple[str, ...] = ()
    on_table_error: str = "preserve"
    max_bytes: int = 64 * 1024 * 1024

    def __post_init__(self):
        if self.encoding is not None:
            if type(self.encoding) is not str:
                raise TypeError("Report encoding must be a string")
            try:
                codecs.lookup(self.encoding)
            except LookupError as error:
                raise ValueError(f"Unknown report encoding: {self.encoding}") from error
        if self.on_decode_error not in ("preserve", "raise"):
            raise ValueError("Report decoding must preserve undecoded bytes or raise")
        if type(self.tables) is not tuple:
            raise TypeError("Report tables must be a tuple of namespaced table keys")
        for key in self.tables:
            namespace_key(key)
        if len(set(self.tables)) != len(self.tables):
            raise ValueError('Duplicate requested report table')
        if self.on_table_error not in ('preserve', 'raise'):
            raise ValueError('Report tables must preserve unfamiliar layouts or raise')
        if type(self.max_bytes) is not int or self.max_bytes <= 0:
            raise ValueError('Report max_bytes must be a positive integer')


@dataclass(frozen=True, kw_only=True)
class RunPaths:
    input: FileReference
    report: FileReference
    output: FileReference


@dataclass(frozen=True, kw_only=True)
class RunConfig:
    output_directory: FileReference
    input_directory: FileReference | None = None
    backend: str = "swmm:standard"
    artifact_stem: str = "model"
    overwrite: bool = False
    keep_failed_artifacts: bool = True
    progress_interval: timedelta = timedelta(seconds=1)
    cancellation_poll_interval: timedelta = timedelta(seconds=1)
    wall_time_limit: timedelta | None = None
    native_call_timeout: timedelta = timedelta(seconds=30)
    step_batch_size: int = 100
    file_inspection_limit: int = 64 * 1024 * 1024
    normalize_inp: bool = False
    report_read: ReportReadOptions = ReportReadOptions()
    required_capabilities: tuple[str, ...] = ()
    cache_reuse: tuple[tuple[str, str], ...] = ()
    # Extension settings are preserved and require explicit runtime support.
    extensions: object | None = None
    schema_version: str = "1.0"
    _source: object | None = field(default=None, repr=False, compare=False)
    _unknown_fields: tuple[str, ...] = field(default=(), repr=False)

    def __post_init__(self):
        from ..io.json import JsonDocument
        from ..io.json.scenario import check_version
        check_version(self.schema_version)
        namespace_key(self.backend)
        if not isinstance(self.output_directory, FileReference) or self.output_directory.direction != "output":
            raise ValueError("Output directory must be an output FileReference")
        if self.input_directory is not None and (not isinstance(self.input_directory, FileReference) or self.input_directory.direction != "input"):
            raise ValueError("Input directory must be an input FileReference")
        if type(self.artifact_stem) is not str or not re.fullmatch(r"[A-Za-z0-9_-]+", self.artifact_stem):
            raise ValueError("Artifact stem must be a simple portable filename without a path or extension")
        if self.artifact_stem.upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
            raise ValueError("Artifact stem is a reserved Windows filename")
        if type(self.overwrite) is not bool or type(self.keep_failed_artifacts) is not bool:
            raise TypeError("Artifact policies must be booleans")
        for name, value in (("progress_interval", self.progress_interval), ("cancellation_poll_interval", self.cancellation_poll_interval), ("wall_time_limit", self.wall_time_limit), ("native_call_timeout", self.native_call_timeout)):
            if value is None and name == "wall_time_limit":
                continue
            if type(value) is not timedelta or value <= timedelta(0):
                raise ValueError(f"{name} must be a positive timedelta")
        if type(self.step_batch_size) is not int or not 1 <= self.step_batch_size <= 100000:
            raise ValueError('step_batch_size must be an integer from 1 to 100000')
        if type(self.file_inspection_limit) is not int or self.file_inspection_limit <= 0:
            raise ValueError('file_inspection_limit must be a positive integer')
        if type(self.normalize_inp) is not bool:
            raise TypeError('normalize_inp must be bool')
        if not isinstance(self.report_read, ReportReadOptions) or type(self.required_capabilities) is not tuple:
            raise TypeError("Invalid report options or capability collection")
        for key in self.required_capabilities:
            capability_key(key)
        if type(self.cache_reuse) is not tuple or any(type(row) is not tuple or len(row)!=2
            or type(row[0]) is not str or not re.fullmatch('[A-Z][A-Z_]*',row[0])
            or row[1] not in ('inspect','require_match','frozen') for row in self.cache_reuse):
            raise ValueError('Cache reuse requires immutable (kind, intent) pairs')
        if len(dict(self.cache_reuse))!=len(self.cache_reuse):raise ValueError('Duplicate cache reuse kind')
        if self.extensions is not None and (not isinstance(self.extensions, JsonDocument) or type(self.extensions.data) is not dict):
            raise TypeError("Run extensions require a JSON object document")

    def validate_support(self, *, backends=(), capabilities=(), extensions=()):
        """Inspect caller-supplied capabilities without discovery, file IO or imports."""
        issues = []
        if self.backend not in backends:
            issues.append(Diagnostic(code="run.backend", message=f"Backend is unavailable: {self.backend}"))
        missing = set(self.required_capabilities) - set(capabilities)
        if missing:
            issues.append(Diagnostic(code="run.capabilities", message=f"Missing runtime capabilities: {sorted(missing)}"))
        unknown = set(self.extensions.data if self.extensions else ()) - set(extensions)
        if unknown or self._unknown_fields:
            issues.append(Diagnostic(code="run.unsupported_settings", message=f"Unrecognized runtime settings: {sorted(unknown) or self._unknown_fields}"))
        return ValidationReport(diagnostics=tuple(issues))

    def resolved_paths(self, *, relative_to=None):
        """Lexical plan only: does not create directories, overwrite or open files."""
        directory = self.output_directory.resolve(relative_to=relative_to)
        def reference(extension):
            return FileReference(path=str(directory / (self.artifact_stem + extension)),
                                 flavor=self.output_directory.flavor, direction="output")
        return RunPaths(input=reference(".inp"), report=reference(".rpt"), output=reference(".out"))

    def cache_intent(self, kind):
        return dict(self.cache_reuse).get(kind,'inspect')

    def to_json_document(self):
        from ..io.json.run import write_config
        return write_config(self)

    def to_json(self, path):
        return self.to_json_document().write(path)

    @classmethod
    def from_json_document(cls, document):
        from ..io.json.run import read_config
        return read_config(document)

    @classmethod
    def from_json(cls, path):
        from ..io.json import JsonDocument
        return cls.from_json_document(JsonDocument.read(path))


@dataclass(frozen=True, kw_only=True)
class ResumeConfig:
    """Operational settings for a new attempt; simulation settings stay bound."""
    output_directory: FileReference
    artifact_stem: str = 'model'
    overwrite: bool = False
    keep_failed_artifacts: bool = True
    progress_interval: timedelta = timedelta(seconds=1)
    cancellation_poll_interval: timedelta = timedelta(seconds=1)
    wall_time_limit: timedelta | None = None
    native_call_timeout: timedelta = timedelta(seconds=30)
    step_batch_size: int = 100
    report_read: ReportReadOptions | None = None

    def __post_init__(self):
        values={f.name:getattr(self,f.name) for f in fields(self) if f.name!='report_read'}
        RunConfig(**values,report_read=self.report_read if self.report_read is not None else ReportReadOptions())

    def apply(self, original):
        if type(original) is not RunConfig:raise TypeError('Expected original RunConfig')
        values={f.name:getattr(self,f.name) for f in fields(self) if f.name!='report_read'}
        return replace(original,**values,input_directory=None,
            report_read=self.report_read if self.report_read is not None else original.report_read,_source=None)
