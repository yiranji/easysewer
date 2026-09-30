"""Explicit, atomic concentration-unit conversion across feature boundaries.

Callbacks inspect one immutable snapshot and return plans, never mutate a store.
Every reference to a resource being rewritten must have an agreeing proposal.
"""

from collections import Counter
from dataclasses import dataclass
from typing import Callable

from ..validation import Diagnostic, DiagnosticSubject, ValidationError, ValidationReport
from .diagnostics import bind_subject
from .identity import Ref, ReferenceUse, canonical_key, references, require_immutable


@dataclass(frozen=True, kw_only=True)
class PollutantUnitTransform:
    value_type: type
    convert: Callable

    def __post_init__(self):
        if not isinstance(self.value_type, type) or not callable(self.convert):
            raise TypeError('A pollutant-unit transform requires a type and a callable')


@dataclass(frozen=True, kw_only=True)
class PollutantResourceConversion:
    """One owner's consent to replace the resource referenced at this path."""
    target: Ref
    path: tuple[str | int, ...]
    value: object

    def __post_init__(self):
        if not isinstance(self.target, Ref):
            raise TypeError('A resource proposal requires a Ref target')
        if type(self.path) is not tuple or not self.path or any(type(p) not in (str, int) for p in self.path):
            raise TypeError('A resource proposal requires a nonempty immutable field path')
        require_immutable(self.value)


@dataclass(frozen=True, kw_only=True)
class PollutantUnitConversion:
    value: object
    resources: tuple[PollutantResourceConversion, ...] = ()

    def __post_init__(self):
        require_immutable(self.value)
        if type(self.resources) is not tuple or any(type(r) is not PollutantResourceConversion for r in self.resources):
            raise TypeError('Resource proposals must be a tuple of PollutantResourceConversion values')


@dataclass(frozen=True, kw_only=True)
class PollutantConversionContext:
    owner: Ref
    target: Ref
    source_units: str
    target_units: str
    factor: float
    profile: object
    record: Callable
    referenced_by: Callable


def convert_pollutant_units(store, id, units, *, schema, profile, _diagnostics=None):
    """Convert MG/L <-> UG/L using active feature registrations.

    All registered record callbacks run once, including indirect consumers.
    Direct consumers without a callback, opaque content and uncovered resource
    users block conversion. Record identities and reference dependencies remain.
    """
    target = Ref(collection='swmm:pollutants', key=id)
    pollutant = store.collection(target.collection)[target.key]
    if units == pollutant.units:
        return

    def reject(message, owner=target):
        own = owner.canonical == target.canonical
        primary = DiagnosticSubject(collection=owner.collection, key=owner.key, path=('units',) if own else ())
        related = [DiagnosticSubject(collection=target.collection, key=target.key, path=('units',))]
        related.extend(DiagnosticSubject(collection=use.owner.collection, key=use.owner.key, path=use.path)
                       for use in store.referenced_by(owner))
        ValidationReport(diagnostics=(Diagnostic(code='quality.unit_conversion',
            message=message, object_id=str(owner.key), feature=owner.collection, subject=primary,
            related=tuple(v for v in dict.fromkeys(related) if v != primary)),)).raise_for_errors()

    if {units, pollutant.units} != {'MG/L', 'UG/L'}:
        reject('Only mass concentration units MG/L and UG/L have a defined conversion; count/mass needs independent physical information')
    if store.opaque_constraints:
        reject('Unstructured records may use the pollutant or its concentration; conversion cannot prove complete coverage')
    rules = {rule.value_type: rule.convert for rule in schema.pollutant_unit_transforms(profile)}
    rows, specs, uses = {}, {}, {}
    for spec in store.specifications:
        for key, value in store.collection(spec.key).items():
            owner = Ref(collection=spec.key, key=key).canonical
            rows[owner], specs[owner] = value, spec
            for path, ref in references(value):
                uses.setdefault(ref.canonical, []).append(ReferenceUse(owner=owner, target=ref, path=path))
    uses = {key: tuple(value) for key, value in uses.items()}

    def record(ref):
        return rows[ref.canonical]

    def referenced_by(ref):
        return uses.get(ref.canonical, ())

    for owner in (target.canonical, *(use.owner for use in referenced_by(target))):
        if type(rows[owner]) not in rules:
            reject('An extension consumer needs an explicit pollutant-unit transform', owner)

    def checked(owner, value):
        before = rows[owner]
        require_immutable(value)
        if type(value) is not type(before):
            raise TypeError('A unit transform must preserve the exact record type')
        if canonical_key(specs[owner].key_of(value)) != owner.key:
            raise ValueError('A unit transform cannot change record identity')
        # A formula transform can wrap leaves/output in unit factors. Reference
        # paths then move within the AST, while dependencies stay identical.
        old_refs = Counter(ref.canonical for _, ref in references(before))
        new_refs = Counter(ref.canonical for _, ref in references(value))
        if old_refs != new_refs:
            raise ValueError('A unit transform cannot change referenced identities or their multiplicities')
        if specs[owner].validate is not None:
            report = ValidationReport(diagnostics=tuple(bind_subject(issue, owner) for issue in specs[owner].validate(value)))
            if not report.is_valid and _diagnostics is not None:
                report = _diagnostics(report, candidates=((owner, value),))
            report.raise_for_errors()

    changes, proposals, consents = {}, {}, {}
    for owner, value in rows.items():
        callback = rules.get(type(value))
        if callback is None:
            continue
        context = PollutantConversionContext(owner=owner, target=target, source_units=pollutant.units,
            target_units=units, factor=1000. if units == 'UG/L' else .001,
            profile=profile, record=record, referenced_by=referenced_by)
        try:
            plan = callback(value, context)
            if type(plan) is not PollutantUnitConversion:
                raise TypeError('A pollutant-unit transform must return PollutantUnitConversion')
            checked(owner, plan.value)
            if plan.value != value:
                changes[owner] = plan.value
            for proposal in plan.resources:
                resource = proposal.target.canonical
                if not any(use.owner == owner and use.path == proposal.path for use in referenced_by(resource)):
                    raise ValueError('Resource proposal path does not address an existing owner reference')
                checked(resource, proposal.value)
                consent = owner, proposal.path
                if consent in consents.setdefault(resource, set()):
                    raise ValueError('Duplicate resource proposal for one reference path')
                consents[resource].add(consent)
                if resource in proposals and proposals[resource] != proposal.value:
                    raise ValueError('Consumers disagree on a shared resource conversion')
                proposals[resource] = proposal.value
        except ValidationError as error:
            report = ValidationReport(diagnostics=tuple(bind_subject(issue, owner) for issue in error.report.diagnostics))
            raise ValidationError(report) from error
        except (KeyError, TypeError, ValueError) as error:
            reject(str(error), owner)
    for resource, value in proposals.items():
        expected = {(use.owner, use.path) for use in referenced_by(resource)}
        if consents[resource] != expected:
            reject('Resource is shared with another consumer without an agreeing conversion proposal; split its data explicitly before conversion', resource)
        if resource in changes and changes[resource] != value:
            reject('Record transform and consumer proposals disagree on the resource conversion', resource)
        if value != rows[resource]:
            changes[resource] = value
    converted = changes.get(target.canonical, pollutant)
    if converted.units != units:
        reject('The pollutant transform did not establish the requested units')
    from .transforms import _apply_candidates
    _apply_candidates(store, ((owner.collection, owner.key, value) for owner, value in changes.items()),
                      'pollutant_units', _diagnostics)
