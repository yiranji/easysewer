"""Interface-file bindings and external identity dependencies, without file IO."""

from dataclasses import dataclass
from typing import Literal

from .fields import validate_fields
from .store import CollectionSpec
from .values import FileReference
from ..validation import Diagnostic, DiagnosticSubject


@dataclass(frozen=True, kw_only=True)
class InterfaceFile:
    kind: Literal["RAINFALL", "RUNOFF", "HOTSTART", "RDII", "INFLOWS", "OUTFLOWS"]
    mode: Literal["USE", "SAVE"]
    file: FileReference

    @property
    def key(self):
        return self.kind, self.mode

    def validate_local(self):
        if self.kind == "INFLOWS" and self.mode != "USE" or self.kind == "OUTFLOWS" and self.mode != "SAVE":
            yield Diagnostic(code="files.mode", message="INFLOWS requires USE; OUTFLOWS requires SAVE")
        direction = "input" if self.mode == "USE" else "output"
        if self.file.direction != direction:
            yield Diagnostic(code="files.direction", message=f"{self.mode} requires an {direction} file reference", field="file")


def guard_file_dependencies(store, action, targets, *, before=None, after=None):
    if store._context_change_allowed("file_rebase"):
        return
    targets = tuple(targets)
    affected = {ref.collection for ref in targets}
    domains = {
        "HOTSTART": {"swmm:nodes", "swmm:links", "swmm:subcatchments", "swmm:pollutants", "swmm:landuses", "swmm:aquifers", "swmm:groundwater", "swmm:snowpacks"},
        "RUNOFF": {"swmm:subcatchments", "swmm:pollutants"},
        "RAINFALL": {"swmm:raingages"},
        "RDII": {"swmm:nodes", "swmm:rdii", "swmm:hydrographs"},
        "INFLOWS": {"swmm:nodes", "swmm:pollutants"},
    }
    for binding in store.collection("swmm:files").values():
        if binding.mode != "USE" or binding.kind not in domains:
            continue
        if affected & domains[binding.kind]:
            structural = action != "replace" or type(before) is not type(after)
            # Hotstart hydrology layouts depend on optional groundwater/snow
            # state; attached rainfall interfaces depend on gage source fields.
            coupled = affected & {"swmm:subcatchments", "swmm:pollutants", "swmm:landuses", "swmm:aquifers", "swmm:groundwater", "swmm:snowpacks", "swmm:raingages", "swmm:rdii", "swmm:hydrographs"}
            if structural or coupled and before != after:
                yield Diagnostic(code="files.external_identity_dependency", feature="swmm:files", object_id=str(binding.key),
                    subject=DiagnosticSubject(collection='swmm:files', key=binding.key, path=('file', 'path')),
                    related=tuple(DiagnosticSubject(collection=ref.collection, key=ref.key) for ref in targets
                                  if ref.collection in domains[binding.kind]),
                    message=f"Cannot {action} objects consumed by USE {binding.kind}; detach the binding and explicitly regenerate/remap its external data")
        if binding.kind in ("HOTSTART", "RUNOFF") and "swmm:options" in affected:
            previous = getattr(before, "flow_units", None) or "CFS"
            current = getattr(after, "flow_units", None) or "CFS"
            if previous != current:
                yield Diagnostic(code="files.external_unit_dependency", feature="swmm:files", object_id=str(binding.key),
                    subject=DiagnosticSubject(collection='swmm:files', key=binding.key, path=('file', 'path')),
                    related=(DiagnosticSubject(collection='swmm:options', key='settings', path=('flow_units',)),),
                    message=f"USE {binding.kind} encodes the original flow units; changing the model unit label does not convert that file")


FILES_COLLECTION = CollectionSpec(key="swmm:files", record_type=InterfaceFile, key_of=lambda value: value.key, validate=validate_fields)
