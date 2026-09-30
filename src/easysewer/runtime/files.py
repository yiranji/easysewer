"""Read-only file inventory and preflight. It never runs or loads native SWMM."""

from dataclasses import dataclass, replace
from functools import partial
import os
from pathlib import Path, PureWindowsPath

from ..io.interface_inspection import InterfaceInspection, inspect_interface
from ..io.data_inspection import inspect_data as inspect_external_data
from ..model.file_resources import FileUse, file_diagnostic, file_subject
from ..model.diagnostics import DiagnosticResolver
from ..validation import Diagnostic, DiagnosticSubject, Severity, ValidationReport
from ..validation._cooperative import checkpoint as _checkpoint, checkpointed, checkpoint_scope
from .directory_resources import directory_adapters as _directory_adapters


@dataclass(frozen=True, kw_only=True)
class FileCheck:
    use: FileUse
    resolved_path: str | None
    available: bool | None
    inspection: InterfaceInspection | None = None


@dataclass(frozen=True, kw_only=True)
class FilePreflight:
    checks: tuple[FileCheck, ...]
    report: ValidationReport

    @property
    def complete(self):
        return self.report.is_valid and not any(d.severity == Severity.WARNING for d in self.report.diagnostics) and all(
            not c.use.active or not c.use.required or c.use.access == 'write' or
            c.use.role == 'swmm:temporary_directory' and c.available or
            c.inspection is not None and c.inspection.status == 'validated' for c in self.checks)


def check_files(model, *, input_directory=None, working_directory=None, overwrite=False, inspect_data=True,
                encoding="utf-8", max_bytes=64*1024*1024, inspectors=None, interface_manifests=None,
                backend_capabilities=None, directory_adapters=None, checkpoint=None, _prepared_directory_outputs=()):
    """Check explicit references without creating directories or modifying files.

    Extra inspectors are trusted callables keyed by the declared format. The
    byte budget limits inspection only; exceeding it is visible as incomplete,
    never treated as a successfully validated data format.
    Model validation and native session ownership are separate run gates.
    interface_manifests accepts explicit HOTSTART/RUNOFF/RDII caller assertions;
    sidecars are never found or read automatically.
    backend_capabilities supplies explicit backend facts without loading native
    code. None leaves required execution capabilities pending; an empty iterable
    explicitly declares none. Runner supplies its selected backend's probe.
    checkpoint is an optional trusted no-argument cooperative work check. Its
    exceptions propagate unchanged. Built-in readers poll during bounded work;
    custom inspectors are checked before and after their existing call contract.
    Exceptions from explicit file/directory inspectors also propagate unchanged;
    return an inspection report to describe invalid resource content.
    """
    with checkpoint_scope(checkpoint):
        return _check_files(model,input_directory=input_directory,working_directory=working_directory,
            overwrite=overwrite,inspect_data=inspect_data,encoding=encoding,max_bytes=max_bytes,
            inspectors=inspectors,interface_manifests=interface_manifests,
            backend_capabilities=backend_capabilities, directory_adapters=directory_adapters,
            prepared_directory_outputs=_prepared_directory_outputs)


def _check_files(model, *, input_directory, working_directory, overwrite, inspect_data,
                 encoding, max_bytes, inspectors, interface_manifests, backend_capabilities, directory_adapters, prepared_directory_outputs):
    if type(overwrite) is not bool or type(inspect_data) is not bool or type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError("Invalid file preflight policy")
    if isinstance(backend_capabilities, str):
        raise TypeError('Backend capabilities must be an iterable of complete IDs')
    if backend_capabilities is not None:
        from ..model.identity import capability_key
        backend_capabilities = frozenset(capability_key(key) for key in backend_capabilities)
    # Only Runner's already-created private output roots bypass the existence
    # diagnostic. This does not authorize publication or existing output files.
    prepared_directory_outputs=frozenset(Path(p).absolute() for p in prepared_directory_outputs)
    directory_adapters = _directory_adapters(directory_adapters)
    inspectors = dict(inspectors or {})
    if any(not callable(v) for v in inspectors.values()):
        raise TypeError("Inspectors must be explicit trusted callables")
    from ..io.hotstart_manifest import HotstartManifest
    from ..io.cache_manifest import CacheManifest
    manifests = dict(interface_manifests or {})
    if any(not (k == 'HOTSTART' and type(v) is HotstartManifest or
                k in ('RUNOFF', 'RDII') and type(v) is CacheManifest and v.kind == k) for k, v in manifests.items()):
        raise TypeError('interface_manifests requires a matching HOTSTART, RUNOFF or RDII manifest')
    used_manifests = set()
    checks, issues, resolved = [], [], []
    callback_error = None
    def invoke_inspector(inspector, *args, **kwargs):
        nonlocal callback_error
        try:
            return inspector(*args, **kwargs)
        except (OSError, ValueError) as error:
            # Only caller code crosses the resource-diagnostic boundary intact.
            # Framework path, budget and content errors remain diagnostics.
            callback_error = error
            raise
    resolver = DiagnosticResolver(model)
    for use in checkpointed(model.file_uses(),interval=1):
        target, available, inspection = None, None, None
        callback_error = None
        if not use.active:
            # Inactive inputs still belong to the caller. An active SAVE must
            # not overwrite them merely because this run does not read them.
            try:
                base = working_directory if use.base == 'working_directory' else input_directory
                pure = use.file.resolve(relative_to=base)
                if isinstance(pure, PureWindowsPath) == (os.name == 'nt'):
                    path = Path(pure).resolve(); target = str(path)
                    if use.access != 'write':
                        resolved.append((use, path))
            except (OSError, ValueError):
                pass  # An unbound inactive reference is not a run dependency.
            checks.append(FileCheck(use=use, resolved_path=target, available=None))
            issues.append(Diagnostic(code="files.inactive_consumer", severity=Severity.INFO,
                subject=file_subject(use),
                message=f"{use.role} is inactive under the declared model options"))
            continue
        severity = Severity.ERROR if use.required else Severity.WARNING
        try:
            base = working_directory if use.base == 'working_directory' else input_directory
            pure = use.file.resolve(relative_to=base)
            if isinstance(pure, PureWindowsPath) != (os.name == 'nt'):
                raise ValueError("Foreign path flavor needs an explicit host mapping before filesystem access")
            path = Path(pure).resolve()
            target = str(path)
            resolved.append((use, path))
            if use.kind == 'directory':
                if path.exists() and not path.is_dir():
                    raise ValueError("Expected a directory resource")
                available = path.is_dir()
                if use.access=='write' and use.role!='swmm:temporary_directory' and available and not overwrite and path not in prepared_directory_outputs:
                    raise ValueError('Output directory already exists and overwrite is false')
                if use.access != 'write' and not available:
                    raise ValueError("Required input directory is missing")
                if use.access != 'write' and not os.access(path, os.R_OK):
                    raise ValueError("Input directory is not readable")
                if use.role != 'swmm:temporary_directory':
                    adapter = directory_adapters.get(use.format)
                    if adapter is None:
                        issues.append(Diagnostic(code='files.directory_adapter_missing', severity=Severity.WARNING,
                            subject=file_subject(use), message=f'Directory format {use.format} requires an explicit adapter'))
                    elif use.access != 'write' and inspect_data:
                        inspecting = replace(adapter, inspector=partial(invoke_inspector, adapter.inspector))
                        inspection = inspecting.inspect(Path(pure).absolute(), use=use, model=model,
                            encoding=encoding, source=target, max_bytes=max_bytes)
            elif use.access == 'write':
                if path.exists() and not path.is_file():
                    raise ValueError("Output file path is not a regular file")
                if path.exists() and not overwrite:
                    raise ValueError("Output already exists and overwrite is false")
                if path.exists() and not os.access(path, os.W_OK):
                    raise ValueError("Existing output is not writable")
                available = path.is_file()
            else:
                if not path.is_file():
                    raise ValueError("Required input is missing or is not a regular file")
                available = True
                if use.access == 'read_write' and not os.access(path, os.W_OK):
                    raise ValueError("Fixed native reader opens this input with write access")
                with path.open('rb') as stream:
                    chunks=[];size=0
                    if inspect_data:
                        while size <= max_bytes:
                            _checkpoint()
                            chunk=stream.read(min(65536,max_bytes+1-size))
                            _checkpoint()
                            if not chunk:
                                break
                            chunks.append(chunk);size+=len(chunk)
                        data=b''.join(chunks)
                        del chunks
                    else:
                        data=stream.read(0)
                    _checkpoint()
                if inspect_data:
                    if len(data) > max_bytes:
                        inspection = InterfaceInspection(format=use.format, status='limited', report=ValidationReport(diagnostics=(Diagnostic(
                            code="files.inspection_limit", severity=Severity.WARNING, message="Data exceeds the explicit inspection byte budget; format verification is incomplete"),)))
                    elif use.format in inspectors:
                        if use.role.startswith('swmm:interface.') and use.role.rsplit('.', 1)[1].upper() in manifests:
                            raise ValueError('A custom interface inspector cannot bypass a supplied manifest')
                        _checkpoint()
                        inspection = invoke_inspector(inspectors[use.format], data, use=use, model=model, encoding=encoding, source=target)
                        _checkpoint()
                        if not isinstance(inspection, InterfaceInspection):
                            raise TypeError("File inspector must return InterfaceInspection")
                    elif use.role.startswith('swmm:interface.'):
                        kind = use.role.rsplit('.',1)[1].upper()
                        inspection = inspect_interface(data, kind, model, encoding=encoding, source=target, manifest=manifests.get(kind))
                        if kind in manifests:
                            used_manifests.add(kind)
                    else:
                        inspection = inspect_external_data(data, use=use, model=model, encoding=encoding, source=target)
            if inspection is not None:
                missing = set(inspection.required_capabilities) - (backend_capabilities or frozenset())
                if missing and inspection.report.is_valid:
                    pending = backend_capabilities is None
                    diagnostic = Diagnostic(code='files.backend_capabilities_pending' if pending else 'files.backend_capabilities_missing',
                        severity=Severity.WARNING if pending else Severity.ERROR,
                        message=f'Interface requires explicit backend capabilities: {sorted(missing)}')
                    inspection = replace(inspection,
                        status='pending' if pending else 'unsupported',
                        report=ValidationReport(diagnostics=inspection.report.diagnostics + (diagnostic,)))
                inspection = replace(inspection, report=ValidationReport(diagnostics=tuple(
                    file_diagnostic(issue, use, resolver, source=target)
                    for issue in inspection.report.diagnostics)))
                issues.extend(inspection.report.diagnostics)
            if use.access == 'write':
                parent = path if use.kind == 'directory' and path.exists() else path.parent
                while not parent.exists() and parent != parent.parent:
                    parent = parent.parent
                if not parent.is_dir() or not os.access(parent, os.W_OK):
                    raise ValueError("Nearest existing output parent is not a writable directory")
        except (OSError, ValueError) as error:
            if error is callback_error:
                raise
            available = False
            issues.append(Diagnostic(code="files.unavailable", severity=severity, message=str(error),
                subject=file_subject(use),
                feature=use.owner.collection, object_id=str(use.owner.key), field='.'.join(map(str,use.path))))
        checks.append(FileCheck(use=use, resolved_path=target, available=available, inspection=inspection))
    directory_identities = {}
    def aliases(use, path):
        if not path.exists():
            return frozenset()
        if use.kind == 'file':
            info = path.stat()
            return frozenset(((info.st_dev, info.st_ino),))
        if path not in directory_identities:
            from ._directory_tree import DirectoryLimits, tree_file_identities
            adapter = directory_adapters.get(use.format)
            directory_identities[path] = tree_file_identities(path,
                limits=adapter.limits if adapter is not None else DirectoryLimits())
        return directory_identities[path]
    for index, (use, path) in enumerate(checkpointed(resolved,interval=1)):
        if use.access != 'write' or use.role == 'swmm:temporary_directory':
            continue
        for other, other_path in checkpointed(resolved[:index]+resolved[index+1:]):
            same_file = path == other_path
            if not same_file and path.exists() and other_path.exists():
                try:
                    same_file = path.samefile(other_path)
                except OSError:
                    pass  # Access errors have their own per-resource diagnostic.
            contains = ((use.kind == 'directory' and other_path.is_relative_to(path)) or
                        (other.kind == 'directory' and path.is_relative_to(other_path)))
            if other.role == 'swmm:temporary_directory':
                continue
            # Explicit active output trees own their declared output children.
            # Inputs, inactive consumers and equal/aliased files remain conflicts.
            if contains and not same_file and use.active and other.active and other.access=='write' and all(
                    v.format in directory_adapters for v in (use,other) if v.kind=='directory'):
                continue
            if contains and not same_file and use.active and other.active and other.kind=='directory' and other.access in ('read','read_write') and other.format in directory_adapters:
                writable=any(v.active and v.kind=='directory' and v.access=='read_write' and v.format in directory_adapters and path!=p and path.is_relative_to(p) and (p.is_relative_to(other_path) or other_path.is_relative_to(p)) for v,p in resolved)
                if writable and path!=other_path and path.is_relative_to(other_path):continue
            if not same_file and not contains and (use.kind == 'directory' or other.kind == 'directory'):
                try:
                    same_file = bool(aliases(use, path) & aliases(other, other_path))
                except (OSError, ValueError) as error:
                    issues.append(Diagnostic(code='files.directory_alias_incomplete',
                        message=f'Directory alias protection is incomplete: {error}',
                        subject=file_subject(use), related=(file_subject(other),)))
                    break
            if same_file or contains:
                issues.append(Diagnostic(code="files.path_collision", message=f"Output {path} aliases another declared input/output",
                    subject=file_subject(use), related=(file_subject(other),)))
                break
    if model._store.opaque_constraints:
        issues.append(Diagnostic(code="files.opaque_consumers", severity=Severity.WARNING,
            message="Unstructured model content may contain additional files or external identities; inventory is incomplete"))
    for kind in manifests.keys() - used_manifests:
        issues.append(Diagnostic(code='files.unused_manifest', severity=Severity.WARNING,
            subject=DiagnosticSubject(collection='swmm:files', key=(kind, 'USE'), path=('file', 'path')),
            message=f'{kind} manifest was supplied but its active input was not inspected'))
    return FilePreflight(checks=tuple(checks), report=ValidationReport(diagnostics=tuple(
        issue if issue.locations else resolver.resolve(issue) for issue in issues)))
