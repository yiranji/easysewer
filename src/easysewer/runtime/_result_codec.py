"""Closed, versioned data codecs for result archives; no payload-directed imports."""

from dataclasses import fields, replace
from datetime import date, datetime, time, timedelta
import hashlib
import math
import types
from typing import Literal, Union, get_args, get_origin, get_type_hints

from .backend import BackendArtifact, BackendInfo, EngineObjects, MassBalance, NativeFailure
from .results import (ResourceSnapshot, RunSnapshot, FileArtifact, RunFailure,
                      ProducedCache, CacheConsumption, RunContinuation, RunResult, DirectoryArtifact, DirectoryGroupArtifact)
from ._directory_tree import DirectoryEntry, DirectoryManifest, DirectoryLimits
from ._directory_graph import DirectoryView, DirectoryLayout, DirectoryGraphState, DirectoryGroupSnapshot
from .cache_reuse import CacheContext, CacheEvidence, CacheReuse
from ..io.json import JsonDocument
from ..io.hotstart_manifest import HotstartManifest
from ..io.cache_manifest import CacheManifest
from ..io.output_metadata import OutputMetadata
from ..io.report_document import ReportDocument, ReportCapture, ReportBlock, ReportMessage
from ..model.identity import Ref
from ..model.options import Options, DayTime, MonthDay
from ..model.report import ReportSelection
from ..model.units import UnitContext, UnitRules
from ..model.values import FileReference
from ..schema.profiles import SwmmProfile
from ..schema.option_profile import ResolvedOptions, OptionAdjustment
from ..validation import Diagnostic, DiagnosticSubject, DiagnosticLocation, SourceSpan, Severity, ValidationReport
from ..results.applicability import ResultContext, ResultApplicability
from ..results.tables import ResultTable


# Explicit wire fields are part of archive versions 1.2/1.3. Adding a dataclass field
# must not silently change this format or discard an unregistered new value.
DECLARATIONS = (
    ('run:directory-limits', DirectoryLimits, 'total_bytes entries depth'),
    ('run:directory-view', DirectoryView, 'key path access required limits kind'),
    ('run:directory-layout', DirectoryLayout, 'roots views limits'),
    ('run:directory-graph-state', DirectoryGraphState, 'layout tree'),
    ('run:directory-group', DirectoryGroupSnapshot, 'key relative_path initial_relative_path state'),
    ('run:directory-group-artifact', DirectoryGroupArtifact, 'group artifact current'),
    ('run:result', RunResult, 'run_id status diagnostics artifacts snapshot backend engine_objects mass_balance failure native_completed produced_caches consumed_caches output_metadata report_document failure_report report_tables retained_directory backend_results continuations directory_artifacts directory_group_artifacts'),
    ('run:continuation', RunContinuation, 'attempt_id execution_run_id checkpoint_sha256 state_sha256 execution_sha256 started_at simulation_seconds steps config_json'),
    ('run:backend-artifact', BackendArtifact, 'role relative_path'),
    ('run:snapshot', RunSnapshot, 'run_id created_at model_json config_json model_sha256 input_bytes input_sha256 profile units options backend resources execution_directory contract backend_settings'),
    ('run:artifact', FileArtifact, 'role path sha256 size complete owner field declared_path'),
    ('run:resource', ResourceSnapshot, 'owner field role format kind access active required original_path relative_path sha256 size tree initial_relative_path directory_group'),
    ('run:directory-artifact', DirectoryArtifact, 'role path original_path manifest files complete owner field declared_path'),
    ('run:directory-entry', DirectoryEntry, 'path kind sha256 size hardlink_to'),
    ('run:directory-manifest', DirectoryManifest, 'entries contract'),
    ('run:failure', RunFailure, 'stage exception_type message native cleanup stderr worker_returncode'),
    ('run:native-failure', NativeFailure, 'stage code message'),
    ('run:backend', BackendInfo, 'key available reason library sha256 engine_version platform architecture abi profiles capabilities isolation origin implementation numerical_policy output_semantics'),
    ('run:objects', EngineObjects, 'groups'),
    ('run:balance', MassBalance, 'runoff_percent flow_percent quality_percent raw_percentages result_context'),
    ('run:produced-cache', ProducedCache, 'kind artifact manifest producer applicability reuse_evidence'),
    ('run:consumed-cache', CacheConsumption, 'kind sha256 producer_run_id producer_input_sha256 producer_model_sha256 producer_engine_sha256 consumer_model_sha256 verification reuse'),
    ('run:cache-context', CacheContext, 'policy kind input_sha256 engine_sha256 facts limitations'),
    ('run:cache-evidence', CacheEvidence, 'cache_sha256 context'),
    ('run:cache-reuse', CacheReuse, 'policy status intent origin differences reasons producer_context_sha256 consumer_context_sha256'),
    ('result:context', ResultContext, 'policy facts evidence'),
    ('result:applicability', ResultApplicability, 'status reasons evidence'),
    ('result:output-metadata', OutputMetadata, 'engine_version flow_units groups pollutant_units variable_codes periods report_step saved_start first_time last_time output_offset period_bytes semantics averages producer input_properties identifier_encoding result_context'),
    ('report:document', ReportDocument, 'raw text requested_encoding encoding diagnostics source profile decoding_strategy repaired_byte_offsets blocks messages message_contexts'),
    ('report:capture', ReportCapture, 'document truncated'),
    ('report:block', ReportBlock, 'title kind start_line end_line text target'),
    ('report:message', ReportMessage, 'diagnostic raw_text input_line input_section input_text input_report_span model_time'),
    ('core:diagnostic', Diagnostic, 'code message severity span feature section object_id field subject related locations'),
    ('core:diagnostic-subject', DiagnosticSubject, 'collection key path'),
    ('core:diagnostic-location', DiagnosticLocation, 'subject status original spans source_sha256'),
    ('core:span', SourceSpan, 'line column end_column source'),
    ('core:validation', ValidationReport, 'diagnostics'),
    ('core:ref', Ref, 'collection key'),
    ('core:file', FileReference, 'path base_directory flavor direction'),
    ('core:units', UnitContext, 'flow_units'),
    ('core:unit-rules', UnitRules, 'key flow_from_cfs si_per_us'),
    ('core:profile', SwmmProfile, 'key engine_version sections option_defaults unit_rules section_terminators transect_offset_power transect_mixed_width_scaling climate_defaults report_defaults'),
    ('core:resolved-options', ResolvedOptions, 'values start end report_start duration defaults_used adjustments report'),
    ('core:option-adjustment', OptionAdjustment, 'field requested effective reason'),
    ('core:options', Options, 'flow_units infiltration flow_routing link_offsets force_main_equation ignore_rainfall ignore_snowmelt ignore_groundwater ignore_rdii ignore_routing ignore_quality allow_ponding skip_steady_state sys_flow_tol lat_flow_tol start_date start_time end_date end_time report_start_date report_start_time sweep_start sweep_end dry_days report_step wet_step dry_step routing_step rule_step lengthening_step variable_step minimum_step inertial_damping normal_flow_limited surcharge_method min_surface_area min_slope max_trials head_tolerance threads slope_weighting compatibility temp_directory'),
    ('core:day-time', DayTime, 'clock day_offset'),
    ('core:month-day', MonthDay, 'month day'),
    ('core:report-selection', ReportSelection, 'mode members'),
)
GRAPH_TYPES=(DirectoryLimits,DirectoryView,DirectoryLayout,DirectoryGraphState,DirectoryGroupSnapshot,DirectoryGroupArtifact)
BY_KEY = {key: (kind, tuple(names.split())) for key, kind, names in DECLARATIONS}
BY_TYPE = {kind: (key, names) for key, (kind, names) in BY_KEY.items()}


def exact(data, names):
    if type(data) is not dict or set(data) != set(names):
        raise ValueError('Archive record has missing or unknown fields')


def matches(value, annotation):
    origin, args = get_origin(annotation), get_args(annotation)
    if origin in (Union, types.UnionType):
        return any(matches(value, a) for a in args)
    if origin is Literal:
        return any(type(value) is type(a) and value == a for a in args)
    if origin in (tuple, frozenset):
        if type(value) is not origin:
            return False
        if origin is frozenset:
            return all(matches(v, args[0]) for v in value)
        if len(args) == 2 and args[1] is Ellipsis:
            return all(matches(v, args[0]) for v in value)
        return len(value) == len(args) and all(matches(v, a) for v, a in zip(value, args))
    if annotation is object:
        return True
    if annotation is float:
        return type(value) in (float, int) and math.isfinite(value)
    return type(value) is annotation


def check_fields(kind, values, names):
    if {f.name for f in fields(kind)} != set(names):
        raise ValueError('Archive codec needs an explicit migration for '+kind.__name__)
    hints = get_type_hints(kind)
    for name in names:
        if not matches(values[name], hints[name]):
            raise TypeError('Invalid archive field '+kind.__name__+'.'+name)


class Codec:
    def __init__(self, blobs, *, result_version='1.2'):
        if result_version not in ('1.0','1.1','1.2','1.3','1.4','1.5','1.6','1.7','1.8','1.9'):raise ValueError('Unsupported result codec version')
        self.blobs = blobs
        self.result_version = result_version

    def report_digest(self, value, depth):
        """Bind every decoded field without repeating raw-backed report text.

        The source record is versioned separately from the decoder profile. A
        changed decoder must still reproduce the saved evidence fingerprint;
        it must never silently replace previously observed blocks/diagnostics.
        """
        _, names = BY_TYPE[ReportDocument]
        values = {name: getattr(value, name) for name in names}
        check_fields(ReportDocument, values, names)

        def text_digest(text):
            if type(text) is not str:
                raise TypeError('Expected report text')
            hashed = hashlib.sha256()
            for start in range(0, len(text), 65536):
                hashed.update(text[start:start+65536].encode('utf-8'))
            return hashed.hexdigest()

        del values['raw']  # independently bound by the raw blob's SHA-256
        values['text'] = None if value.text is None else text_digest(value.text)
        values['blocks'] = tuple(replace(block, text=text_digest(block.text)) for block in value.blocks)
        encoded = {name: self.encode(item, depth+1) for name, item in values.items()}
        return hashlib.sha256(JsonDocument.from_data(encoded).to_bytes()).hexdigest()

    def encode(self, value, depth=0):
        if depth > 64:
            raise ValueError('Archive object nesting exceeds 64')
        child = lambda v: self.encode(v, depth+1)
        if value is None or type(value) in (str, bool, int, float):
            return value
        if type(value) is Severity:
            return {'type': 'core:severity', 'value': value.value}
        if type(value) is bytes:
            return dict(type='core:bytes', **self.blobs.put_bytes(value))
        if type(value) in (tuple, frozenset):
            values = sorted(value) if type(value) is frozenset else value
            return {'type': 'core:set' if type(value) is frozenset else 'core:tuple', 'values': [child(v) for v in values]}
        if type(value) in (date, datetime, time):
            return {'type': 'core:'+type(value).__name__, 'value': value.isoformat(), 'fold': getattr(value, 'fold', 0)}
        if type(value) is timedelta:
            return {'type': 'core:duration', 'days': value.days, 'seconds': value.seconds, 'microseconds': value.microseconds}
        if type(value) is JsonDocument:
            return {'type': 'core:json', 'raw': child(value.to_bytes()), 'source': value.source}
        for kind, key in ((HotstartManifest, 'cache:hotstart'), (CacheManifest, 'cache:interface')):
            if type(value) is kind:
                return {'type': key, 'raw': child(value.to_bytes())}
        if type(value) is ResultTable:
            return {'type': 'result:table', 'raw': child(value.to_json_document(
                version='1.2' if self.result_version in ('1.2','1.3','1.4','1.5','1.6','1.7','1.8','1.9') else '1.1').to_bytes())}
        if type(value) is ReportDocument and self.result_version in ('1.3','1.4','1.5','1.6','1.7','1.8','1.9'):
            return dict(type='report:source', version='1.0', raw=child(value.raw),
                requested_encoding=value.requested_encoding, source=value.source, profile=value.profile,
                decoded_sha256=self.report_digest(value, depth))
        declaration = BY_TYPE.get(type(value))
        if declaration is None:
            raise TypeError('Unsupported result archive value: '+type(value).__name__)
        key, names = declaration
        values = {name: getattr(value, name) for name in names}
        check_fields(type(value), values, names)
        if self.result_version not in ('1.2','1.3','1.4','1.5','1.6','1.7','1.8','1.9'):
            if type(value) in (DiagnosticSubject, DiagnosticLocation):
                raise ValueError('Legacy result codec cannot retain diagnostic locations')
            if type(value) is Diagnostic:
                if value.subject is not None or value.related or value.locations:
                    raise ValueError('Legacy result codec cannot retain diagnostic locations')
                for name in ('subject', 'related', 'locations'):del values[name]
        if type(value) is RunResult and self.result_version=='1.0':
            if value.continuations:raise ValueError('Result archive 1.0 cannot retain continuation history')
            del values['continuations']
        if self.result_version not in ('1.4','1.5','1.6','1.7','1.8','1.9'):
            if type(value) in (DirectoryArtifact, DirectoryEntry, DirectoryManifest):
                raise ValueError('Legacy result codec cannot retain directory evidence')
            if type(value) is ResourceSnapshot:
                if value.tree is not None:raise ValueError('Legacy result codec cannot retain directory resources')
                del values['tree']
            if type(value) is RunResult:
                if value.directory_artifacts:raise ValueError('Legacy result codec cannot retain directory artifacts')
                del values['directory_artifacts']
        if type(value) is ResourceSnapshot and self.result_version not in ('1.5','1.6','1.7','1.8','1.9'):
            if value.initial_relative_path is not None:
                raise ValueError('Legacy result codec cannot retain independent initial directory evidence')
            del values['initial_relative_path']
        if self.result_version not in ('1.6','1.7','1.8','1.9') and (type(value) is DirectoryArtifact and value.manifest is None or
                type(value) is ResourceSnapshot and value.initial_relative_path is not None and value.tree is None):
            raise ValueError('Legacy result codec cannot retain explicit directory absence')
        if type(value) is DirectoryEntry and self.result_version not in ('1.7','1.8','1.9'):
            if value.hardlink_to is not None:raise ValueError('Legacy result codec cannot retain directory hardlinks')
            del values['hardlink_to']
        if type(value) is DirectoryManifest and value.has_hardlinks and self.result_version not in ('1.7','1.8','1.9'):
            raise ValueError('Legacy result codec cannot retain directory topology')
        if self.result_version not in ('1.8','1.9'):
            if type(value) in GRAPH_TYPES:raise ValueError('Legacy result codec cannot retain directory groups')
            if type(value) is ResourceSnapshot:
                if value.directory_group is not None:raise ValueError('Legacy result codec cannot retain directory groups')
                del values['directory_group']
            if type(value) is RunResult:
                if value.directory_group_artifacts:raise ValueError('Legacy result codec cannot retain directory group artifacts')
                del values['directory_group_artifacts']
        if type(value) is DirectoryView and self.result_version!='1.9':
            if value.kind!='directory':raise ValueError('Legacy graph codec cannot retain file views')
            del values['kind']
        if type(value) is DirectoryArtifact:
            value.verify()
        if type(value) is FileArtifact:
            self.blobs.put_artifact(value)
        encoded={'type': key, 'fields': {name: child(v) for name, v in values.items()}}
        if type(value) is DirectoryArtifact:value.verify()
        return encoded

    def decode(self, data, depth=0):
        if depth > 64:
            raise ValueError('Archive object nesting exceeds 64')
        child = lambda v: self.decode(v, depth+1)
        if data is None or type(data) in (str, bool, int, float):
            return data
        if type(data) is not dict or type(data.get('type')) is not str:
            raise ValueError('Invalid typed archive value')
        key = data['type']
        if key == 'report:source':
            if self.result_version not in ('1.3','1.4','1.5','1.6','1.7','1.8','1.9'):
                raise ValueError('Legacy result codec cannot contain compact report evidence')
            exact(data, ('type', 'version', 'raw', 'requested_encoding', 'source', 'profile', 'decoded_sha256'))
            if data['version'] != '1.0':
                raise ValueError('Unknown compact report contract')
            for name in ('requested_encoding', 'source', 'profile'):
                if data[name] is not None and type(data[name]) is not str:
                    raise TypeError('Invalid report '+name)
            raw = child(data['raw'])
            if type(raw) is not bytes:
                raise TypeError('Expected report source bytes')
            value = ReportDocument.from_bytes(raw, encoding=data['requested_encoding'],
                source=data['source'], profile=data['profile'], max_bytes=max(1, len(raw)))
            if self.report_digest(value, depth) != data['decoded_sha256']:
                raise ValueError('Archived report decoding differs from its raw bytes')
            return value
        if key == 'core:bytes':
            exact(data, ('type', 'sha256', 'size'))
            return self.blobs.get_bytes(data['sha256'], data['size'])
        if key in ('core:tuple', 'core:set'):
            exact(data, ('type', 'values'))
            if type(data['values']) is not list:
                raise TypeError('Expected an archive value array')
            values = tuple(child(v) for v in data['values'])
            if key == 'core:tuple':
                return values
            if any(type(v) is not str for v in values) or len(set(values)) != len(values):
                raise ValueError('Archive sets require unique strings')
            return frozenset(values)
        if key == 'core:severity':
            exact(data, ('type', 'value'))
            return Severity(data['value'])
        if key in ('core:date', 'core:datetime', 'core:time'):
            exact(data, ('type', 'value', 'fold'))
            if type(data['value']) is not str or type(data['fold']) is not int or data['fold'] not in (0, 1):
                raise ValueError('Invalid archive calendar value')
            kind = {'core:date': date, 'core:datetime': datetime, 'core:time': time}[key]
            value = kind.fromisoformat(data['value'])
            if kind is date:
                if data['fold']:
                    raise ValueError('Dates have no fold')
                return value
            return value.replace(fold=data['fold'])
        if key == 'core:duration':
            exact(data, ('type', 'days', 'seconds', 'microseconds'))
            if any(type(data[k]) is not int for k in ('days', 'seconds', 'microseconds')):
                raise TypeError('Duration components must be integers')
            if not 0 <= data['seconds'] < 86400 or not 0 <= data['microseconds'] < 1_000_000:
                raise ValueError('Duration must use normalized components')
            return timedelta(days=data['days'], seconds=data['seconds'], microseconds=data['microseconds'])
        if key == 'core:json':
            exact(data, ('type', 'raw', 'source'))
            if data['source'] is not None and type(data['source']) is not str:
                raise TypeError('Invalid JSON source identity')
            return JsonDocument.from_bytes(child(data['raw']), source=data['source'])
        if key in ('cache:hotstart', 'cache:interface', 'result:table'):
            exact(data, ('type', 'raw'))
            raw = child(data['raw'])
            if key == 'result:table':
                document = JsonDocument.from_bytes(raw)
                if self.result_version not in ('1.2','1.3','1.4','1.5','1.6','1.7','1.8','1.9') and document.data.get('schema_version') not in ('1.0','1.1'):
                    raise ValueError('Legacy archive cannot contain new result-table diagnostics')
                return ResultTable.from_json_document(document)
            kind = HotstartManifest if key == 'cache:hotstart' else CacheManifest
            return kind.from_bytes(raw)
        if key not in BY_KEY:
            raise ValueError('Unknown result archive type: '+key)
        exact(data, ('type', 'fields'))
        kind, names = BY_KEY[key]
        if self.result_version not in ('1.2','1.3','1.4','1.5','1.6','1.7','1.8','1.9') and kind in (DiagnosticSubject, DiagnosticLocation):
            raise ValueError('Legacy result codec cannot contain diagnostic locations')
        if self.result_version not in ('1.4','1.5','1.6','1.7','1.8','1.9') and kind in (DirectoryArtifact, DirectoryEntry, DirectoryManifest):
            raise ValueError('Legacy result codec cannot contain directory evidence')
        legacy=kind is RunResult and self.result_version=='1.0'
        legacy_diagnostic=kind is Diagnostic and self.result_version not in ('1.2','1.3','1.4','1.5','1.6','1.7','1.8','1.9')
        wire_names=tuple(n for n in names if not (legacy and n=='continuations') and not (
            legacy_diagnostic and n in ('subject','related','locations')) and not (
            self.result_version not in ('1.4','1.5','1.6','1.7','1.8','1.9') and ((kind is ResourceSnapshot and n=='tree') or (kind is RunResult and n=='directory_artifacts'))))
        if kind is ResourceSnapshot and self.result_version not in ('1.5','1.6','1.7','1.8','1.9'):
            wire_names = tuple(n for n in wire_names if n != 'initial_relative_path')
        if kind is DirectoryEntry and self.result_version not in ('1.7','1.8','1.9'):
            wire_names = tuple(n for n in wire_names if n != 'hardlink_to')
        if self.result_version not in ('1.8','1.9'):
            if kind in GRAPH_TYPES:raise ValueError('Legacy result codec cannot contain directory groups')
            if kind is ResourceSnapshot:wire_names=tuple(n for n in wire_names if n!='directory_group')
            if kind is RunResult:wire_names=tuple(n for n in wire_names if n!='directory_group_artifacts')
        if kind is DirectoryView and self.result_version!='1.9':wire_names=tuple(n for n in wire_names if n!='kind')
        exact(data['fields'], wire_names)
        values = {name: child(v) for name, v in data['fields'].items()}
        if kind is DirectoryView and self.result_version!='1.9':values['kind']='directory'
        if legacy:values['continuations']=()
        if legacy_diagnostic:values.update(subject=None,related=(),locations=())
        if self.result_version not in ('1.4','1.5','1.6','1.7','1.8','1.9'):
            if kind is ResourceSnapshot:values['tree']=None
            if kind is RunResult:values['directory_artifacts']=()
        if kind is ResourceSnapshot and self.result_version not in ('1.5','1.6','1.7','1.8','1.9'):values['initial_relative_path']=None
        if kind is DirectoryEntry and self.result_version not in ('1.7','1.8','1.9'):values['hardlink_to']=None
        if self.result_version not in ('1.8','1.9'):
            if kind is ResourceSnapshot:values['directory_group']=None
            if kind is RunResult:values['directory_group_artifacts']=()
        check_fields(kind, values, names)
        if kind is DirectoryArtifact:values['path']=None
        if kind is FileArtifact:
            values['path'] = str(self.blobs.get_path(values['sha256'], values['size']))
        value = kind(**values)
        if kind is DirectoryManifest and value.has_hardlinks and self.result_version not in ('1.7','1.8','1.9'):
            raise ValueError('Legacy result codec cannot contain directory topology')
        if self.result_version not in ('1.6','1.7','1.8','1.9') and (kind is DirectoryArtifact and value.manifest is None or
                kind is ResourceSnapshot and value.initial_relative_path is not None and value.tree is None):
            raise ValueError('Legacy result codec cannot contain explicit directory absence')
        if kind is ReportDocument:
            from ..io.report_document import _matches_source
            if not _matches_source(value):
                raise ValueError('Archived report decoding differs from its raw bytes')
        return value
