"""Candidate 2.0 facade. File formats and native backends load only on demand."""

from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from ..validation import Diagnostic, ValidationError, ValidationReport
from ..validation._cooperative import checkpoint_scope, checkpointed


class Model:
    def __init__(self, *, schema=None, profile=None):
        from ..io.inp.network import default_schema
        from ..schema import EPA_SWMM_5_2_4
        self._schema = (schema if schema is not None else default_schema()).snapshot()
        self._profile = profile or EPA_SWMM_5_2_4
        self._store = self._schema.new_store()
        self._source = None
        self._json_source = None

    @property
    def profile(self):
        return self._profile

    @property
    def nodes(self):
        return self.collection("swmm:nodes")

    @property
    def links(self):
        return self.collection("swmm:links")

    @property
    def curves(self):
        return self.collection("swmm:curves")

    @property
    def timeseries(self):
        return self.collection("swmm:timeseries")

    @property
    def patterns(self):
        return self.collection("swmm:patterns")

    @property
    def transects(self):
        return self.collection("swmm:transects")

    @property
    def streets(self):
        return self.collection("swmm:streets")

    @property
    def inlets(self):
        return self.collection("swmm:inlets")

    @property
    def inlet_usage(self):
        return self.collection("swmm:inlet_usage")

    @property
    def inflows(self):
        return self.collection("swmm:inflows")

    @property
    def dwf(self):
        return self.collection("swmm:dwf")

    @property
    def hydrographs(self):
        return self.collection("swmm:hydrographs")

    @property
    def rdii(self):
        return self.collection("swmm:rdii")

    @property
    def pollutants(self):
        return self.collection("swmm:pollutants")

    @property
    def aquifers(self):
        return self.collection("swmm:aquifers")

    @property
    def groundwater(self):
        return self.collection("swmm:groundwater")

    @property
    def gwf(self):
        return self.collection("swmm:gwf")

    @property
    def treatment(self):
        return self.collection('swmm:treatment')

    @property
    def lid_controls(self):
        return self.collection('swmm:lid_controls')

    @property
    def lid_usage(self):
        return self.collection('swmm:lid_usage')

    @property
    def landuses(self):
        return self.collection("swmm:landuses")

    @property
    def coverages(self):
        return self.collection("swmm:coverages")

    @property
    def loadings(self):
        return self.collection("swmm:loadings")

    @property
    def buildup(self):
        return self.collection("swmm:buildup")

    @property
    def washoff(self):
        return self.collection("swmm:washoff")

    @property
    def controls(self):
        return self.collection("swmm:controls")

    @property
    def report(self):
        from .report import get_report
        return get_report(self._store)

    @property
    def effective_report(self):
        from .report import resolve_report
        return resolve_report(self._store, self.profile)

    @property
    def title(self):
        from .project import ProjectTitle
        return self.collection("swmm:title").get("text", ProjectTitle())

    @property
    def metadata(self):
        return self.collection("easysewer:metadata")

    @property
    def tags(self):
        return self.collection('swmm:tags')

    @property
    def labels(self):
        from .project import MapLabels
        return self.collection('swmm:labels').get('layer', MapLabels())

    def update_labels(self, **changes):
        return self._update_settings('swmm:labels', 'layer', self.labels, changes)

    @property
    def profiles(self):
        return self.collection('swmm:profiles')

    @property
    def events(self):
        from .events import EventSchedule
        return self.collection('swmm:events').get('schedule', EventSchedule())

    @property
    def files(self):
        return self.collection("swmm:files")

    @property
    def map(self):
        from .project import MapSettings
        return self.collection("swmm:map").get("settings", MapSettings())

    @property
    def backdrop(self):
        from .project import Backdrop
        return self.collection("swmm:backdrop").get("image", Backdrop())

    @property
    def effective_map(self):
        from .project import resolve_map
        return resolve_map(self.map, self.backdrop)

    def _update_settings(self, namespace, key, current, changes):
        from .fields import validate_fields
        from .identity import Ref
        from .diagnostics import bind_subject
        value = replace(current, **changes)
        owner = Ref(collection=namespace, key=key)
        report = ValidationReport(diagnostics=tuple(bind_subject(issue, owner) for issue in validate_fields(value)))
        self._resolve_diagnostics(report, candidates=((owner, value),)).raise_for_errors()
        rows = self.collection(namespace)
        if key in rows:
            rows.replace(key, value)
        else:
            rows.add(value)
        return value

    def update_report(self, **changes):
        return self._update_settings("swmm:report", "settings", self.report, changes)

    def update_title(self, **changes):
        return self._update_settings("swmm:title", "text", self.title, changes)

    def update_map(self, **changes):
        return self._update_settings("swmm:map", "settings", self.map, changes)

    def update_events(self, **changes):
        return self._update_settings('swmm:events', 'schedule', self.events, changes)

    def update_backdrop(self, **changes):
        return self._update_settings("swmm:backdrop", "image", self.backdrop, changes)

    def set_annotation(self, key, value):
        from .project import JsonAnnotation
        annotation = JsonAnnotation.from_value(key, value)
        if key in self.metadata:
            self.metadata.replace(key, annotation)
        else:
            self.metadata.add(annotation)
        return annotation

    @property
    def climate(self):
        from .climate import get_climate
        return get_climate(self._store)

    @property
    def raingages(self):
        return self.collection("swmm:raingages")

    @property
    def subcatchments(self):
        return self.collection("swmm:subcatchments")

    @property
    def snowpacks(self):
        return self.collection("swmm:snowpacks")

    @property
    def effective_climate(self):
        from .climate import resolve_climate
        return resolve_climate(self.climate, self.options, self.profile)

    @property
    def subcatchment_adjustments(self):
        return self.collection("swmm:subcatchment_adjustments")

    def update_climate(self, **changes):
        return self._update_settings('swmm:climate', 'settings', self.climate, changes)

    @property
    def options(self):
        from .options import get_options
        return get_options(self._store)

    @property
    def units(self):
        from .units import UnitContext
        return UnitContext(flow_units=self.options.flow_units or self.profile.option_default("flow_units"))

    @property
    def effective_options(self):
        from .fields import validate_fields
        from .identity import Ref
        from .diagnostics import bind_subject
        from .options import option_diagnostics
        from ..schema.option_profile import resolve_options
        owner = Ref(collection='swmm:options', key='settings')
        report = ValidationReport(diagnostics=tuple(bind_subject(issue, owner) for issue in validate_fields(self.options)))
        self._resolve_diagnostics(report).raise_for_errors()
        try:
            return resolve_options(self.options, self.profile)
        except ValidationError as error:
            report = ValidationReport(diagnostics=tuple(option_diagnostics(error.report.diagnostics)))
            raise ValidationError(self._resolve_diagnostics(report)) from error

    def update_options(self, **changes):
        return self._update_settings('swmm:options', 'settings', self.options, changes)

    def reinterpret_units(self, flow_units):
        from .units import UnitContext
        with self._store._permit_context_change("units"):
            return self.update_options(flow_units=UnitContext(flow_units=flow_units).flow_units)

    def convert_units(self, flow_units, *, basis="engine"):
        from .transforms import convert_units
        from .units import UnitContext
        self.validate().raise_for_errors()
        with self._diagnostic_boundary():
            convert_units(self._store, UnitContext(flow_units=flow_units), self.profile, self._schema,
                basis=basis, _diagnostics=self._resolve_diagnostics)

    def convert_pollutant_units(self, id, units):
        from .pollutant_units import convert_pollutant_units
        self.validate().raise_for_errors()
        with self._diagnostic_boundary(), self.transaction():
            convert_pollutant_units(self._store, id, units, schema=self._schema, profile=self.profile,
                _diagnostics=self._resolve_diagnostics)

    def reinterpret_pollutant_units(self, id, units):
        if units not in ('MG/L','UG/L','#/L'):
            raise ValueError('Unknown pollutant concentration units')
        with self._store._permit_context_change('pollutant_units'):
            return self.pollutants.update(id, units=units)

    def convert_link_offsets(self, mode):
        from .transforms import convert_link_offsets
        self.validate().raise_for_errors()
        with self._diagnostic_boundary():
            convert_link_offsets(self._store, mode, self.profile, _diagnostics=self._resolve_diagnostics)

    def reinterpret_link_offsets(self, mode):
        with self.transaction(), self._store._permit_context_change("offsets"):
            self.update_options(link_offsets=mode)

    def reinterpret_force_main_equation(self, equation):
        with self._store._permit_context_change("force_main_equation"):
            return self.update_options(force_main_equation=equation)

    @property
    def document(self):
        """Original source snapshot; export projects current semantic values onto it."""
        return None if self._source is None else self._source.decoded.document

    @property
    def support(self):
        return None if self._source is None else self._source.decoded

    def collection(self, key):
        from .store import RecordCollection
        return RecordCollection(self._store, self._store._specs[key], _diagnostics=self._resolve_diagnostics)

    def _resolve_diagnostics(self, report, *, candidates=()):
        from .diagnostics import DiagnosticResolver
        resolver = DiagnosticResolver(self, candidates=candidates)
        return ValidationReport(diagnostics=tuple(resolver.resolve(issue) for issue in report.diagnostics))

    @contextmanager
    def _diagnostic_boundary(self):
        try:
            yield
        except ValidationError as error:
            from .diagnostics import DiagnosticResolver
            resolver = DiagnosticResolver(self)
            # A precommit rejection may already carry candidate evidence; the
            # rolled-back graph must not overwrite it with the valid old value.
            report = ValidationReport(diagnostics=tuple(issue if issue.locations else resolver.resolve(issue)
                for issue in error.report.diagnostics))
            raise ValidationError(report) from error

    def referenced_by(self, ref):
        return self._store.referenced_by(ref)

    def provenance(self, ref):
        """Original INP record declarations and lifecycle association, if known."""
        from .provenance import record_provenance
        return record_provenance(self, ref)

    def field_provenance(self, ref, path):
        """Inspect a path in the original record, including original tuple indexes."""
        from .inspection import original_field
        return original_field(self, ref, path)

    def inspect_field(self, ref, path):
        """Inspect current field facts and safely associated original declarations."""
        from .inspection import inspect_field
        return inspect_field(self, ref, path)

    def deletion_plan(self, ref):
        return self._store.deletion_plan(ref)

    @contextmanager
    def transaction(self):
        with self._store.transaction(validate=False):
            yield self
            self.validate().raise_for_errors()

    def copy(self, *, checkpoint=None):
        with checkpoint_scope(checkpoint):
            result = Model(schema=self._schema, profile=self.profile)
            result._store = self._store.clone()
            result._source = self._source
            result._json_source = self._json_source
            return result

    def run(self, config, *, runner=None, **options):
        """Execute an independent snapshot through the candidate runtime API."""
        if runner is None:
            from ..runtime import Runner
            runner = Runner()
        return runner.run(self, config, **options)

    @classmethod
    def from_document(cls, document, *, schema=None, profile=None, strict=False):
        from ..io.inp.semantic import ModelSource
        result = cls(schema=schema, profile=profile)
        result._store, result._source = ModelSource.read(document, result._schema, result.profile)
        if strict:
            result.validate().raise_for_errors()
        return result

    @classmethod
    def from_inp(cls, path, *, encoding=None, **kwargs):
        from ..io.inp import InpDocument
        return cls.from_document(InpDocument.read(path, encoding=encoding), **kwargs)

    @classmethod
    def from_json_document(cls, document, **kwargs):
        from ..io.json.model import read_model
        return read_model(document, **kwargs)

    @classmethod
    def from_json(cls, path, **kwargs):
        from ..io.json import JsonDocument
        return cls.from_json_document(JsonDocument.read(path), **kwargs)

    @property
    def json_document(self):
        return self._json_source.document if self._json_source else None

    @property
    def json_migration(self):
        return self._json_source.migration if self._json_source else None

    def to_json_document(self, *, checkpoint=None):
        from ..io.json.model import write_model
        with checkpoint_scope(checkpoint):
            return write_model(self)

    def to_json(self, path):
        return self.to_json_document().write(path)

    def json_schema(self):
        from ..io.json.model import model_schema
        return model_schema(self._schema)

    def validate(self, *, for_run=False, normalize=False, checkpoint=None):
        with checkpoint_scope(checkpoint):
            from .usage import validate_uses
            from .diagnostics import DiagnosticResolver
            issues = list(self._source.report.diagnostics if self._source else ())
            if self._json_source:
                issues.extend(self._json_source.report(for_run=for_run).diagnostics)
                issues.extend(self._json_source.validate_changes(self))
            issues.extend(self._store.validate().diagnostics)
            for descriptor, codec in checkpointed(self._schema.bindings):
                if self.profile.key in descriptor.profiles:
                    issues.extend(checkpointed(codec.validate(self._store, self.profile)))
                    if for_run and callable(getattr(codec, "validate_run", None)):
                        issues.extend(checkpointed(codec.validate_run(self._store, self.profile)))
            issues.extend(validate_uses(self._store, self._schema.resource_uses(self._store, self.profile)))
            if for_run and ValidationReport(diagnostics=tuple(issues)).is_valid:
                from ..io.inp.semantic import render_new
                document = (self._source.render(self._schema, self._store, self.profile, normalize=normalize)
                            if self._source else render_new(self._schema, self._store, self.profile))
                issues.extend(self._validate_native_document(document).diagnostics)
            resolver = DiagnosticResolver(self)
            return ValidationReport(diagnostics=tuple(resolver.resolve(issue) for issue in checkpointed(issues)))

    def _validate_native_document(self, document):
        """Check the exact encoded document that a caller intends to execute."""
        from ..io.inp.semantic import native_document_diagnostics
        issues = list(document.report.diagnostics)
        issues.extend(native_document_diagnostics(document, self.profile))
        for descriptor, codec in checkpointed(self._schema.bindings):
            if self.profile.key in descriptor.profiles and callable(getattr(codec, "validate_document", None)):
                issues.extend(checkpointed(codec.validate_document(document, self.profile)))
        return ValidationReport(diagnostics=tuple(issues))

    def resource_uses(self, ref):
        return tuple(use for use in self._schema.resource_uses(self._store, self.profile)
                     if use.target.canonical == ref.canonical)

    def file_uses(self):
        return self._schema.file_uses(self._store, self.profile)

    def resolve_files(self, *, input_directory=None, working_directory=None):
        from .file_resources import resolve_files
        return resolve_files(self, input_directory=input_directory, working_directory=working_directory)

    def to_document(self, *, normalize=False, checkpoint=None):
        with checkpoint_scope(checkpoint):
            from ..io.inp.semantic import render_new
            if self._json_source:
                self._json_source.require_inp()
            self.validate().raise_for_errors()
            if self._source is not None:
                return self._source.render(self._schema, self._store, self.profile, normalize=normalize)
            return render_new(self._schema, self._store, self.profile)

    def to_inp(self, path, *, encoding=None, path_policy="relative", normalize=False):
        from .transforms import rebase_files
        path = Path(path).resolve()
        for descriptor, codec in self._schema.bindings:
            if self.profile.key in descriptor.profiles and callable(getattr(codec, "validate_export", None)):
                self._resolve_diagnostics(ValidationReport(diagnostics=tuple(codec.validate_export(self._store, self.profile,
                    directory=str(path.parent), path_policy=path_policy)))).raise_for_errors()
        if self.document is not None and self.document.source is not None:
            origin = Path(self.document.source).parent
            if origin != path.parent and self._store.opaque_constraints:
                ValidationReport(diagnostics=tuple(Diagnostic(code="inp.unresolved_file_paths", span=constraint.span,
                    message="Cannot rebase unstructured external-file references; retain the source directory or add their codecs")
                    for constraint in self._store.opaque_constraints)).raise_for_errors()
        snapshot = self.copy()
        with snapshot._diagnostic_boundary():
            rebase_files(snapshot._store, str(path.parent), path_policy)
        snapshot.to_document(normalize=normalize).write(path, encoding=encoding)
