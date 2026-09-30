"""Reporting choices, including native cumulative object-selection flags."""

from dataclasses import dataclass, replace
from typing import Literal

from .fields import validate_fields
from .identity import Ref
from .store import CollectionSpec
from ..validation import Diagnostic, Severity


@dataclass(frozen=True, kw_only=True)
class ReportSelection:
    mode: Literal["ALL", "NONE", "SELECTED"]
    members: tuple[Ref, ...] = ()

    def validate_local(self):
        if self.mode == "SELECTED" and not self.members:
            yield Diagnostic(code="report.empty_selection", message="SELECTED needs at least one object")
        if len({ref.canonical for ref in self.members}) != len(self.members):
            yield Diagnostic(code="report.duplicate_member", message="Selection members must be unique")
        if self.members and str(self.members[0].key).upper() in ("ALL", "NONE"):
            yield Diagnostic(code="report.reserved_first_member", message="Native SWMM treats first member ALL/NONE as a selector; put another selected ID first")


@dataclass(frozen=True, kw_only=True)
class ReportOptions:
    disabled: bool | None = None
    input: bool | None = None
    continuity: bool | None = None
    flow_stats: bool | None = None
    controls: bool | None = None
    averages: bool | None = None
    subcatchments: ReportSelection | None = None
    nodes: ReportSelection | None = None
    links: ReportSelection | None = None

    def validate_local(self):
        for name in ("subcatchments", "nodes", "links"):
            selection = getattr(self, name)
            if selection:
                if any(ref.collection != "swmm:" + name for ref in selection.members):
                    yield Diagnostic(code="report.member_namespace", message=f"Selection requires swmm:{name} references", field=name)
                if selection.mode == "NONE" and selection.members:
                    yield Diagnostic(code="report.sticky_members", severity=Severity.WARNING, field=name,
                        message="Native NONE does not clear previously marked objects; they remain in OUT")


REPORT_DEFAULTS = (("disabled", False), ("input", False), ("continuity", True), ("flow_stats", True),
                   ("controls", False), ("averages", False), ("subcatchments", ReportSelection(mode="NONE")),
                   ("nodes", ReportSelection(mode="NONE")), ("links", ReportSelection(mode="NONE")))


@dataclass(frozen=True, kw_only=True)
class ResolvedReport:
    settings: ReportOptions
    subcatchments: tuple[Ref, ...]
    nodes: tuple[Ref, ...]
    links: tuple[Ref, ...]


def get_report(store):
    return store.collection("swmm:report").get("settings", ReportOptions())


def resolve_report(store, profile):
    options = get_report(store)
    defaults = dict(profile.report_defaults)
    if set(defaults) != {key for key, _ in REPORT_DEFAULTS}:
        raise ValueError(f"Profile {profile.key} does not declare all reporting defaults")
    settings = replace(options, **{key: default for key, default in defaults.items() if getattr(options, key) is None})
    selections = {}
    for name in ("subcatchments", "nodes", "links"):
        selection = getattr(settings, name)
        members = {ref.canonical for ref in selection.members}
        # Native output indices follow object construction order, not list order.
        selections[name] = tuple(ref for key in store.collection("swmm:" + name)
            if (ref := Ref(collection="swmm:" + name, key=key)).canonical in members or selection.mode == "ALL")
    return ResolvedReport(settings=settings, **selections)


REPORT_COLLECTION = CollectionSpec(key="swmm:report", record_type=ReportOptions, key_of=lambda _: "settings", validate=validate_fields)
