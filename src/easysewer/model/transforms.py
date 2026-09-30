"""Explicit physical transformations. Partial coverage fails before commit."""

from dataclasses import fields, is_dataclass, replace
from enum import Enum

from ..validation import Diagnostic, DiagnosticSubject, ValidationError, ValidationReport
from .diagnostics import local_path
from .identity import Ref
from .network import Conduit, Pump, Orifice, Weir, Outlet
from .options import DayTime, MonthDay, Options, get_options
from .units import ConversionContext, UnitContext
from .values import FileReference, Offset, Point


def _reject(code, message, field=None, *, subject=None, related=()):
    ValidationReport(diagnostics=(Diagnostic(code=code, message=message, field=field,
        subject=subject, related=related),)).raise_for_errors()


def _apply_candidates(store, candidates, permission, diagnostics=None):
    """Resolve candidate errors before rollback, including precommit guards."""
    candidates = tuple(candidates)
    with store.transaction(validate=False), store._permit_context_change(permission):
        try:
            for namespace, key, record in candidates:
                rows = store.collection(namespace)
                if key in rows: rows.replace(key, record)
                else: rows.add(record)
            store.validate().raise_for_errors()
        except ValidationError as error:
            if diagnostics is None:
                raise
            values = tuple((Ref(collection=namespace, key=key), value) for namespace, key, value in candidates)
            raise ValidationError(diagnostics(error.report, candidates=values)) from error


def convert_units(store, target: UnitContext, profile, schema, *, basis="engine", _diagnostics=None):
    if basis not in ("engine", "physical"):
        raise ValueError("Unit conversion basis must be engine or physical")
    rules = profile.unit_rules if basis == "engine" else None
    option = DiagnosticSubject(collection='swmm:options', key='settings', path=('flow_units',))
    if basis == "engine" and rules is None:
        _reject("units.missing_profile", f"Profile {profile.key} has no engine unit rules", subject=option)
    options = get_options(store)
    original = UnitContext(flow_units=options.flow_units or profile.option_default("flow_units"))
    if original == target:
        return
    if store.opaque_constraints:
        _reject("units.incomplete_coverage", "Cannot convert units while source-owned records have unknown dimensions", subject=option)
    equation = options.force_main_equation or profile.option_default("force_main_equation")
    snapshot = store.clone()
    uses = schema.resource_uses(snapshot, profile)
    context = ConversionContext(source=original, target=target, rules=rules, profile=profile,
        record=lambda ref: snapshot.collection(ref.collection)[ref.key], referenced_by=snapshot.referenced_by,
        resource_uses=lambda ref: tuple(use for use in uses if use.target.canonical == ref.canonical))
    custom = {rule.value_type: rule.convert for rule in schema.unit_transforms(profile)}

    def convert(value, owner, path=()):
        def subject(at=path):
            return DiagnosticSubject(collection=owner.collection, key=owner.key, path=at or ())
        def related(at=path):
            values = [option]
            if at and any(type(part) is int for part in at): values.append(subject(()))
            values.extend(DiagnosticSubject(collection=use.owner.collection, key=use.owner.key, path=use.path)
                          for use in snapshot.referenced_by(owner))
            return tuple(dict.fromkeys(values))
        def reject(code, message, at=path):
            field = None if not at else ''.join(('[' + str(part) + ']') if type(part) is int
                else ('.' if i else '') + part for i, part in enumerate(at))
            _reject(code, message, field, subject=subject(at), related=related(at))
        if type(value) in custom:
            def child_convert(child, *, path=None):
                # A value's identity is not a field address. Extensions can
                # explicitly supply a relative typed path; otherwise use only
                # the owning record as diagnostic context.
                return convert(child, owner, None if path is None or current_path is None else current_path + path)
            current_path = path
            try:
                converted = custom[type(value)](value, replace(context, convert_value=child_convert))
            except ValidationError as error:
                issues = []
                for issue in error.report.diagnostics:
                    if issue.subject is None or issue.subject.collection is None:
                        suffix = issue.subject.path if issue.subject is not None else local_path(issue.field)
                        at = None if path is None else path + suffix
                        issue = replace(issue, subject=subject(at),
                            related=tuple(dict.fromkeys((*issue.related, *related(at)))))
                    issues.append(issue)
                raise ValidationError(ValidationReport(diagnostics=tuple(issues))) from error
            except (KeyError, TypeError, ValueError, OverflowError) as error:
                reject('units.transform_failed', str(error) or type(error).__name__)
            if type(converted) is not type(value):
                reject("units.invalid_transform", "Unit transforms must preserve the exact value type")
            return converted
        if isinstance(value, (Ref, FileReference, Point, DayTime, MonthDay)):
            return value
        if isinstance(value, tuple):
            return tuple(convert(item, owner, None if path is None else path + (index,)) for index, item in enumerate(value))
        if not is_dataclass(value):
            return value
        updates = {}
        for item in fields(value):
            child = getattr(value, item.name)
            name = None if path is None else path + (item.name,)
            dimension = item.metadata.get("dimension")
            if isinstance(child, bool) or child is None or isinstance(child, Enum):
                after = child
            elif dimension and isinstance(child, (int, float)):
                if dimension == "offset":
                    dimension = "length"
                elif dimension == "force_main_roughness":
                    dimension = "rain_depth" if equation == "D-W" else "ratio"
                try:
                    after = original.convert(child, dimension=dimension, to=target, rules=rules)
                except ValueError as error:
                    reject("units.unsupported_dimension", str(error), name)
                if item.metadata.get("integer"):
                    after = int(after)
            elif isinstance(child, (int, float)) and not isinstance(value, Options):
                reject("units.missing_dimension", f"No unit rule declared for {type(value).__name__}.{item.name}", name)
            else:
                after = convert(child, owner, name)
            if after != child:
                if not item.init:
                    reject("units.immutable_field", "Converted fields must support replacement", name)
                updates[item.name] = after
        return replace(value, **updates) if updates else value

    # Construct all candidates before changing the graph; failures cannot leave
    # partially converted values even without an outer user transaction.
    candidates = [(spec.key, key, convert(record, Ref(collection=spec.key, key=key))) for spec in store.specifications
                  for key, record in store.collection(spec.key).items()]
    updated = next((record for namespace, _, record in candidates if namespace == 'swmm:options'), get_options(store))
    candidates.append(('swmm:options', 'settings', replace(updated, flow_units=target.flow_units)))
    _apply_candidates(store, candidates, 'units', _diagnostics)


def convert_link_offsets(store, target, profile, *, _diagnostics=None):
    if target not in ("DEPTH", "ELEVATION"):
        raise ValueError("Offset mode must be DEPTH or ELEVATION")
    options = get_options(store)
    current = options.link_offsets or profile.option_default("link_offsets")
    if current == target:
        return
    if store.opaque_constraints:
        _reject("offsets.incomplete_coverage", "Cannot convert offsets while source-owned link parameters remain",
            subject=DiagnosticSubject(collection='swmm:options', key='settings', path=('link_offsets',)))
    nodes = store.collection("swmm:nodes")
    updates = []
    for record in store.collection("swmm:links").values():
        def subject(*path):
            return DiagnosticSubject(collection='swmm:links', key=record.id, path=path)
        if isinstance(record, Pump):
            continue
        if isinstance(record, Conduit):
            offsets = (("inlet_offset", record.inlet), ("outlet_offset", record.outlet))
        elif isinstance(record, (Orifice, Outlet)):
            offsets = (("offset", record.inlet),)
        elif isinstance(record, Weir):
            offsets = (("crest_height", record.inlet),)
        else:
            _reject("offsets.unsupported_variant", f"Offset conversion is not implemented for {type(record).__name__}", subject=subject())
        values = {}
        for field, ref in offsets:
            value = getattr(record, field)
            elevation = nodes[ref.key].elevation
            if current == "DEPTH":
                values[field] = max(value or 0, 0) + elevation
            else:
                if value is None:
                    _reject("offsets.missing_elevation", "Absolute offsets must be set before conversion", field,
                        subject=subject(field), related=(DiagnosticSubject(collection=ref.collection, key=ref.key, path=('elevation',)),
                            DiagnosticSubject(collection='swmm:options', key='settings', path=('link_offsets',))))
                values[field] = 0 if value is Offset.NODE_INVERT else max(value - elevation, 0)
        updates.append(replace(record, **values))
    candidates = [('swmm:links', record.id, record) for record in updates]
    candidates.append(('swmm:options', 'settings', replace(options, link_offsets=target)))
    _apply_candidates(store, candidates, 'offsets', _diagnostics)


def rebase_files(store, directory, policy="relative"):
    def rebase(value, owner, path=()):
        if isinstance(value, FileReference):
            if policy == "preserve":
                return value
            try:
                return replace(value, path=value.for_directory(directory, policy=policy), base_directory=directory)
            except ValueError as error:
                _reject('files.rebase_path', str(error), subject=DiagnosticSubject(
                    collection=owner.collection, key=owner.key, path=(*path, 'path')))
        if isinstance(value, tuple):
            return tuple(rebase(child, owner, (*path, index)) for index, child in enumerate(value))
        if is_dataclass(value):
            changes = {item.name: rebase(getattr(value, item.name), owner, (*path, item.name)) for item in fields(value) if item.init}
            return replace(value, **changes)
        return value

    with store._permit_context_change("file_rebase"):
        for spec in store.specifications:
            for key, value in store.collection(spec.key).items():
                changed = rebase(value, Ref(collection=spec.key, key=key))
                if changed != value:
                    store.collection(spec.key).replace(key, changed)
