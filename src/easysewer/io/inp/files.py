"""All documented FILES directives, with native last-assignment semantics."""

from dataclasses import replace

from ...model.fields import validate_fields
from ...model.files import FILES_COLLECTION, InterfaceFile, guard_file_dependencies
from ...model.identity import Ref
from ...schema.registry import DecodedFeature, FeatureDescriptor
from ...schema.context_fields import FILES_FIELD_RULES
from ...schema.structured import EncodedRow, FeatureData, RecordEntry, SourceBinding
from ...validation import Diagnostic, DiagnosticSubject, Severity, SourceSpan, ValidationReport
from .climate import _file
from .field_sources import FieldSources
from ...validation._cooperative import checkpointed

_KINDS = ("RAINFALL", "RUNOFF", "HOTSTART", "RDII", "INFLOWS", "OUTFLOWS")
_MODES = ("NO", "SCRATCH", "USE", "SAVE")


def _subject(key, *path):
    return DiagnosticSubject(collection='swmm:files', key=key, path=path)


class FilesCodec:
    descriptor = FeatureDescriptor(key="swmm:files", sections=frozenset({"FILES"}), atomic_write=True,
                                   ordered_sections=frozenset({"FILES"}), requires=("swmm:network", "swmm:hydrology"))
    collections = (FILES_COLLECTION,)
    field_rules = FILES_FIELD_RULES
    mutation_guards = (guard_file_dependencies,)

    def decode(self, document, profile):
        records, seen, issues, failed = {}, [], [], set()
        lexical = {d.span.line for d in checkpointed(document.report.errors) if d.span}
        for line in checkpointed(document.records("FILES")):
            span = SourceSpan(source=document.source, line=line.number, column=1, end_column=len(line.content)+1)
            tokens, slot = line.values, None
            mode = kind = None
            try:
                if line.number in lexical or len(tokens) < 2:
                    raise ValueError("FILES requires a disposition and interface type")
                mode = next((m for m in checkpointed(_MODES) if tokens[0].upper().startswith(m)), None)
                kind = next((k for k in checkpointed(_KINDS) if tokens[1].upper().startswith(k)), None)
                if mode is None or kind is None:
                    issues.append(Diagnostic(code="files.unknown_directive", severity=Severity.WARNING, section="FILES", span=span,
                        message="Unknown interface directive remains source-owned"))
                    continue
                slot = (kind, mode if kind == "HOTSTART" else "ACTIVE")
                if len(tokens) < 3:
                    issues.append(Diagnostic(code="files.native_noop", severity=Severity.INFO, section="FILES", span=span,
                        message="Native FILES without a file name has no effect; retained as source text"))
                    continue
                if kind == "INFLOWS" and mode != "USE" or kind == "OUTFLOWS" and mode != "SAVE":
                    raise ValueError("Native INFLOWS requires USE and OUTFLOWS requires SAVE")
                if mode not in ("USE", "SAVE"):
                    # An unstructured later assignment must not leave an earlier
                    # active binding that would misrepresent the native state.
                    failed.add(slot)
                    issues.append(Diagnostic(code="files.undocumented_mode", severity=Severity.WARNING, section="FILES", span=span,
                        message="Undocumented NO/SCRATCH assignment remains source-owned with its affected slot"))
                    continue
                file = replace(_file(tokens[2], None if kind == "RDII" else document.source),
                               direction="input" if mode == "USE" else "output")
                value = InterfaceFile(kind=kind, mode=mode, file=file)
                ValidationReport(diagnostics=tuple(validate_fields(value))).raise_for_errors()
                if slot in records:
                    issues.append(Diagnostic(code="files.replaced_assignment", severity=Severity.INFO, section="FILES", span=span,
                        message=f"Later {kind} assignment replaces the earlier native slot"))
                records[slot] = value
                seen.append((line.number, slot))
                if tokens[:2] != (mode, kind) or len(tokens) > 3:
                    issues.append(Diagnostic(code="files.native_coercion", severity=Severity.WARNING, section="FILES", span=span,
                        message="Native FILES uses keyword prefixes and ignores trailing tokens"))
                if kind == "RDII" and not file.path_type(file.path).is_absolute():
                    issues.append(Diagnostic(code="files.rdii_working_directory", severity=Severity.WARNING, section="FILES", span=span,
                        message="SWMM 5.2.4 resolves this RDII path against the process working directory; bind its file base explicitly"))
            except (ValueError, TypeError) as error:
                if slot is not None:
                    failed.add(slot)
                issues.append(Diagnostic(code="files.invalid_input", message=str(error), section="FILES", span=span,
                    subject=_subject((kind, mode)) if kind is not None and mode in ('USE', 'SAVE') else None))
        for slot in checkpointed(failed):
            records.pop(slot, None)
        bindings = tuple(SourceBinding(line=line, key=records[slot].key) for line, slot in checkpointed(seen) if slot in records)
        sources = FieldSources()
        lines = {line.number: line for line in checkpointed(document.records('FILES'))}
        paths = ((('kind',), (1,), 'value'), (('mode',), (0,), 'value'),
                 (('file',), (0, 2), 'derived'), (('file', 'path'), (2,), 'value'),
                 (('file', 'direction'), (0,), 'derived'), (('file', 'flavor'), (2,), 'derived'),
                 (('file', 'base_directory'), (2,), 'derived'))
        owners = {Ref(collection='swmm:files', key=v.key).canonical: v for v in checkpointed(records.values())}
        for binding in checkpointed(bindings):
            owner = Ref(collection='swmm:files', key=binding.key)
            for path, tokens, role in checkpointed(paths):
                sources.cover(owner, path)
                sources.add(owner, path, lines[binding.line], tokens, role=role)
        # A lexical failure can hide both disposition and kind. Do not claim
        # complete assignment history for any surviving native slot in that case.
        if lexical.intersection(lines):
            for owner in checkpointed(owners): sources.block(owner, ())
        return DecodedFeature(value=FeatureData(records=tuple(RecordEntry(collection="swmm:files", value=v) for v in checkpointed(records.values())), bindings=bindings,
                                               **sources.finish(owners)),
            claimed_lines=frozenset(b.line for b in checkpointed(bindings)), report=ValidationReport(diagnostics=tuple(issues)))

    def encode(self, store, profile):
        for value in checkpointed(store.collection("swmm:files").values()):
            path = value.file.path
            if value.kind == "RDII" and value.file.base_directory is not None:
                path = str(value.file.resolve())
            yield EncodedRow(key=value.key, section="FILES", values=(value.mode, value.kind, path),
                             owners=(Ref(collection="swmm:files", key=value.key),))

    def validate(self, store, profile):
        seen = {}
        for value in store.collection("swmm:files").values():
            slot = value.key if value.kind == "HOTSTART" else (value.kind, "ACTIVE")
            if slot in seen:
                yield Diagnostic(code="files.conflicting_modes", message=f"{value.kind} cannot be both used and saved in one model", object_id=value.kind,
                    subject=_subject(value.key, 'mode'), related=(_subject(seen[slot], 'mode'),))
            seen[slot] = value.key

    def validate_run(self, store, profile):
        bindings = {(v.kind, v.mode) for v in store.collection("swmm:files").values()}
        if ("RUNOFF", "USE") not in bindings or not store.collection("swmm:subcatchments"):
            return
        yield Diagnostic(code="files.runoff_replay_scope", severity=Severity.WARNING,
            subject=_subject(('RUNOFF', 'USE'), 'file', 'path'),
            related=tuple(DiagnosticSubject(collection='swmm:subcatchments', key=key)
                          for key in store.collection('swmm:subcatchments')),
            message="RUNOFF replays sampled runoff and quality, not full hydrology: rain reports and controls use current gages; rain-history accumulation requires the backend capability easysewer:runoff-rain-clock:1. Infiltration, snowpack, LID and groundwater water-balance statistics are not recomputed. Hydrology ignore flags do not filter cached flows.")
        if ("HOTSTART", "SAVE") in bindings:
            yield Diagnostic(code="files.runoff_hotstart_partial", severity=Severity.WARNING,
                subject=_subject(('HOTSTART', 'SAVE'), 'file', 'path'),
                related=(_subject(('RUNOFF', 'USE'), 'file', 'path'),) + (
                    (_subject(('HOTSTART', 'USE'), 'file', 'path'),)
                    if ('HOTSTART', 'USE') in bindings else ()),
                message="HOTSTART saved during RUNOFF replay contains replayed runoff/quality and groundwater flow, elevation and moisture, but other hydrologic states remain initialized or inherited from USE HOTSTART; it is not a complete hydrologic continuation state.")

    def validate_export(self, store, profile, *, directory, path_policy):
        if path_policy == "preserve":
            return
        for value in store.collection("swmm:files").values():
            if value.kind == "RDII" and value.file.base_directory is None and not value.file.path_type(value.file.path).is_absolute():
                yield Diagnostic(code="files.unbound_working_directory", message="Bind the RDII FileReference.base_directory before rebasing export; the INP directory is not its native base",
                    subject=_subject(value.key, 'file', 'base_directory'), related=(_subject(value.key, 'file', 'path'),))

    def validate_document(self, document, profile):
        for line in document.records("FILES"):
            if len(line.values) >= 3 and len(line.values[2].encode(document.encoding)) > 259:
                mode = next((m for m in _MODES if line.values[0].upper().startswith(m)), None)
                kind = next((k for k in _KINDS if line.values[1].upper().startswith(k)), None)
                yield Diagnostic(code="files.native_path_limit", message="Interface path exceeds the fixed engine's 259-byte buffer", section="FILES",
                    subject=_subject((kind, mode), 'file', 'path') if kind and mode in ('USE', 'SAVE') else None,
                    span=SourceSpan(source=document.source, line=line.number, column=1, end_column=len(line.content)+1))

    def file_uses(self, store, profile):
        from ...model.file_resources import FileUse
        from ...model.options import get_options
        from ...model.hydrology import FileRainfall
        options = get_options(store)
        for value in checkpointed(store.collection("swmm:files").values()):
            active = True
            if value.kind == "RAINFALL":
                active = not options.ignore_rainfall and any(isinstance(g.source, FileRainfall) for g in checkpointed(store.collection("swmm:raingages").values()))
            elif value.kind == "RDII":
                active = not options.ignore_rainfall and not options.ignore_rdii
            elif value.kind == "RUNOFF":
                active = bool(store.collection("swmm:subcatchments"))
            elif value.kind in ("INFLOWS", "OUTFLOWS"):
                active = bool(store.collection("swmm:nodes")) and not options.ignore_routing
            yield FileUse(owner=Ref(collection="swmm:files", key=value.key), path=("file",), file=value.file,
                role="swmm:interface." + value.kind.lower(), format="swmm:" + value.kind.lower() + ".interface",
                base="working_directory" if value.kind == "RDII" else "document",
                access="write" if value.mode == "SAVE" else "read", active=active)
