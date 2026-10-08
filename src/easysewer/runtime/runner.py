"""Validated, isolated execution with captured inputs and owned publications."""

from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time
import uuid

from .backend import SessionCancelled, SessionError, SessionTimeout
from .config import ResumeConfig, RunConfig
from .files import check_files
from .native import StandardBackend, _NATIVE_SESSION_FACTORY
from .flexible import FlexiblePondingBackend
from .results import (CacheConsumption, FileArtifact, ProducedCache, RunError, RunFailure,
                      RunContinuation, RunProgress, RunResult, RunSnapshot)
from ._preparation import host_path, inventory, reject, stage, verify_resources, input_matches
from ._workspace import OutputTransaction, Workspace, copy_input, digest_file, fingerprint, file_state
from .checkpoint import CheckpointSchedule
from ._runner_checkpoint import RunnerCheckpoint, RunnerContext, capture as capture_checkpoint
from ..io.inp import InpDocument
from ..io.json import JsonDocument
from ..io._record_work import record_asdict, record_dumps, write_record_bytes
from ..validation._cooperative import checkpoint_scope, checkpointed
from ..model import Model
from ..model.identity import canonical_key
from ..model.file_resources import file_subject
from ..validation import Diagnostic, Severity, ValidationError, ValidationReport
from ..results.applicability import context_from_model
from .cache_reuse import CacheEvidence, CachePolicies, swmm_cache_policies, RDII_POLICY, HOTSTART_POLICY
from ..io._record_work import read_record_bytes


class _Interrupted(Exception):
    def __init__(self, status):
        self.status=status
        super().__init__('Whole-run deadline exceeded' if status=='timed_out' else 'Run cancelled')


class _SessionUse:
    """Remember successful context cleanup even when the body raises."""
    def __init__(self, context):
        self.context = context
        self.value = None
        self.closed = False

    def __enter__(self):
        self.value = self.context.__enter__()
        return self.value

    def __exit__(self, *args):
        result = self.context.__exit__(*args)
        self.closed = True
        return result

    def stopped(self):
        if not self.closed:
            return False
        unavailable = object()
        code = getattr(self.value, 'returncode', unavailable)
        return code is unavailable or type(code) is int


class _Cancellation:
    def __init__(self, event, deadline):
        self.event, self.deadline = event, deadline

    def status(self):
        if self.event is not None and self.event.is_set():return 'cancelled'
        if self.deadline is not None and time.monotonic()>=self.deadline:return 'timed_out'
        return None

    def is_set(self):return self.status() is not None

    def check(self):
        status=self.status()
        if status:raise _Interrupted(status)

    def timeout(self, maximum):
        self.check()
        return maximum if self.deadline is None else max(.001,min(maximum,self.deadline-time.monotonic()))


class Runner:
    """Return a RunResult for operational failures; bad API argument types raise.

    Backends and inspectors are explicitly trusted Python extensions. Callback
    errors become the primary run failure; raise_on_error preserves their cause.
    A Model must not be mutated concurrently while its initial copy is taken.
    """
    def __init__(self, *, backends=None, inspectors=None, report_tables=None, cache_policies=None,
                 directory_adapters=None, directory_limits=None):
        self.backends=dict(backends) if backends is not None else {
            'swmm:standard':StandardBackend(),'easysewer:flexible-ponding':FlexiblePondingBackend()}
        self.inspectors=dict(inspectors or {})
        from .directory_resources import directory_adapters as validate_adapters, DirectoryLimits
        self.directory_limits=DirectoryLimits() if directory_limits is None else directory_limits
        if type(self.directory_limits) is not DirectoryLimits:raise TypeError('Expected DirectoryLimits')
        self.directory_adapters={key:replace(adapter,limits=DirectoryLimits(
            total_bytes=min(adapter.limits.total_bytes,self.directory_limits.total_bytes),
            entries=min(adapter.limits.entries,self.directory_limits.entries),
            depth=min(adapter.limits.depth,self.directory_limits.depth)))
            for key,adapter in validate_adapters(directory_adapters).items()}
        from ..io.report import ReportTables, swmm_report_tables
        self.report_tables=swmm_report_tables() if report_tables is None else report_tables
        if not isinstance(self.report_tables, ReportTables):raise TypeError('Expected a ReportTables registry')
        self.cache_policies=swmm_cache_policies() if cache_policies is None else cache_policies
        if not isinstance(self.cache_policies,CachePolicies):raise TypeError('Expected a CachePolicies registry')

    def run(self, model, config, *, relative_to=None, progress=None, cancel_event=None,
            interface_manifests=None, producers=None, cache_evidence=None, checkpoints=None, raise_on_error=False):
        if not isinstance(model,Model) or type(config) is not RunConfig:
            raise TypeError('Runner requires a Model and RunConfig')
        return self._run(model,config,relative_to=relative_to,progress=progress,cancel_event=cancel_event,
            interface_manifests=interface_manifests,producers=producers,cache_evidence=cache_evidence,
            checkpoints=checkpoints,raise_on_error=raise_on_error)

    def resume(self, checkpoint, config, *, relative_to=None, progress=None, cancel_event=None,
               checkpoints=None, schema=None, raise_on_error=False):
        """Resume a complete Runner checkpoint into explicitly selected outputs.

        ResumeConfig changes operational settings only. The registered backend
        must load the same native bytes; stored library paths never select code.
        Archive verification/preparation failures return ordinary RunResults.
        """
        if type(config) is not ResumeConfig:raise TypeError('resume requires ResumeConfig')
        if type(checkpoint) is not RunnerCheckpoint and not isinstance(checkpoint,(str,os.PathLike)):
            raise TypeError('resume requires RunnerCheckpoint or its directory')
        return self._run(None,config,relative_to=relative_to,progress=progress,cancel_event=cancel_event,
            checkpoints=checkpoints,raise_on_error=raise_on_error,_resume=checkpoint,_resume_schema=schema)

    def _run(self, model, config, *, relative_to=None, progress=None, cancel_event=None,
             interface_manifests=None, producers=None, cache_evidence=None, checkpoints=None,
             raise_on_error=False, _resume=None, _resume_schema=None):
        if checkpoints is not None and type(checkpoints) is not CheckpointSchedule:
            raise TypeError('checkpoints requires CheckpointSchedule')
        if progress is not None and not callable(progress):raise TypeError('progress must be callable')
        if cancel_event is not None and not callable(getattr(cancel_event,'is_set',None)):
            raise TypeError('cancel_event must provide is_set()')
        if type(raise_on_error) is not bool:raise TypeError('raise_on_error must be bool')
        producers=dict(producers or {})
        if any(type(value) is not ProducedCache or value.kind!=kind for kind,value in producers.items()):
            raise TypeError('producers maps interface kinds to ProducedCache records')
        manifests=dict(interface_manifests or {})
        if producers.keys() & manifests.keys():raise ValueError('Supply either a producer or an asserted manifest for each interface')
        evidence_by_kind=dict(cache_evidence or {})
        if any(not isinstance(v,CacheEvidence) or v.context.kind!=k for k,v in evidence_by_kind.items()):
            raise TypeError('cache_evidence maps kinds to CacheEvidence')
        if producers.keys() & evidence_by_kind.keys():raise ValueError('Producer records already own their condition evidence')
        evidence_by_kind.update({k:v.reuse_evidence for k,v in producers.items() if v.reuse_evidence is not None})
        manifests.update({kind:value.manifest for kind,value in producers.items()})
        run_id=uuid.uuid4().hex
        created=datetime.now(timezone.utc);began=time.monotonic()
        deadline=began+config.wall_time_limit.total_seconds() if config.wall_time_limit else None
        cancellation=_Cancellation(cancel_event,deadline)
        captured=None
        issues=[];artifacts=[];produced=[];consumed=[]
        directory_artifacts=directory_group_artifacts=()
        snapshot=info=engine_objects=balance=failure=output_metadata=report_document=None
        failure_report=None
        report_tables=()
        execution_plan=backend_results=None
        backend_products=()
        workspace=transaction=session=None
        session_requested=False;session_use=None
        native_completed=False;status='failed';cause=None;phase='validation'
        resources=outputs=plans=()
        cache_contexts={}
        steps=0;elapsed=0.;duration=0.;next_progress=began
        asset_name='easysewer-assets-'+run_id
        source_files={};main_targets={};original_bytes=b''
        attempt_id=uuid.uuid4().hex;archive=None;continuations=()
        restored_outputs=();restored_stamps={};outputs_adopted=False;restore_outputs_unknown=False
        checkpoint_directory=None;next_checkpoint=checkpoints.interval.total_seconds() if checkpoints else None

        def resource_diagnostics(report):
            from ..model.diagnostics import DiagnosticResolver
            resolver=DiagnosticResolver(captured)
            report=replace(report,diagnostics=tuple(issue if issue.locations else resolver.resolve(issue)
                for issue in report.diagnostics))
            return execution_sources(report)

        def execution_sources(report):
            if archive is None:
                return report
            # Only fresh evidence for the frozen execution input gets this
            # label. Original and explicitly named external sources stay intact.
            source='checkpoint-input:'+snapshot.input_sha256
            def labelled(span):
                return replace(span,source=source) if span is not None and span.source is None else span
            return replace(report,diagnostics=tuple(replace(issue,span=labelled(issue.span),
                locations=tuple(replace(location,spans=tuple(labelled(span) for span in location.spans))
                    if location.source_sha256==snapshot.input_sha256 else location for location in issue.locations))
                for issue in report.diagnostics))

        def emit(name, *, force=False):
            nonlocal next_progress,phase
            cancellation.check()
            if transaction is not None and transaction.journal is not None and transaction.journal.data['phase']!=phase:
                transaction.journal.sync(transaction,phase=phase)
            now=time.monotonic()
            if progress is not None and (force or now>=next_progress):
                before=phase;phase='callback'
                progress(RunProgress(run_id=run_id,phase=name,wall_seconds=now-began,
                    simulation_seconds=elapsed,fraction=min(1.,elapsed/duration) if duration else 0.,steps=steps))
                phase=before
                next_progress=time.monotonic()+config.progress_interval.total_seconds()

        try:
            cancellation.check()
            with checkpoint_scope(cancellation.check):
                captured=model.copy() if model is not None else None
            if _resume is not None:
                phase='checkpoint_load'
                requested=_resume.directory if type(_resume) is RunnerCheckpoint else Path(_resume)
                if not Path(requested).is_absolute():
                    requested=(Path(relative_to) if relative_to is not None else Path.cwd())/requested
                archive=RunnerCheckpoint.load(requested,checkpoint=cancellation.check)
                if type(_resume) is RunnerCheckpoint and archive!=_resume:
                    reject('run.checkpoint_changed','Runner checkpoint changed since it was loaded')
                config=config.apply(archive.config)
                snapshot=archive.snapshot;info=snapshot.backend;run_id=snapshot.run_id
                asset_name=archive.context.asset_directory
                steps=archive.context.steps;elapsed=archive.simulation_seconds
                duration=snapshot.options.duration.total_seconds()
                continuations=(*archive.context.continuations,RunContinuation(attempt_id=attempt_id,
                    execution_run_id=run_id,checkpoint_sha256=archive.sha256,state_sha256=archive.state.sha256,
                    execution_sha256=archive.state._archive.binding.hex(),started_at=created,
                    simulation_seconds=elapsed,steps=steps,config_json=config.to_json_document().to_bytes()))
                issues.extend(archive.context.diagnostics.diagnostics)
                consumed.extend(archive.context.consumed_caches)
                from ._execution_model import execution_model
                captured=execution_model(snapshot,schema=_resume_schema,strict=False)
                if checkpoints:next_checkpoint=(int(elapsed/checkpoints.interval.total_seconds())+1)*checkpoints.interval.total_seconds()
            with checkpoint_scope(cancellation.check):
                validation=execution_sources(captured.validate())
            issues.extend(validation.diagnostics);validation.raise_for_errors()
            if config.backend not in self.backends:
                reject('run.backend','Requested backend is not registered: '+config.backend)
            for table_key in config.report_read.tables:
                if table_key not in self.report_tables.keys:
                    reject('run.report_tables','Unregistered RPT table: '+table_key)
            backend=self.backends[config.backend]
            phase='capability'
            probed=backend.probe(call_timeout=cancellation.timeout(config.native_call_timeout.total_seconds()),
                cancel_event=cancellation,poll_interval=config.cancellation_poll_interval.total_seconds())
            cancellation.check()
            if not probed.available:reject('run.backend',probed.reason or 'Backend is unavailable')
            if archive is not None:
                if replace(probed,library=info.library,origin=info.origin)!=info:
                    reject('run.checkpoint_backend','Selected backend differs from the checkpoint execution')
                info=replace(info,library=probed.library);snapshot=replace(snapshot,backend=info)
            else:info=probed
            support=config.validate_support(backends=self.backends,capabilities=info.capabilities,
                extensions=getattr(backend,'supported_extensions',()))
            issues.extend(support.diagnostics);support.raise_for_errors()
            if captured.profile.key not in info.profiles:
                reject('run.profile',f'Backend does not support {captured.profile.key}')
            validate_profile=getattr(backend,'validate_profile',None)
            if validate_profile:validate_profile(captured.profile)
            if archive is None:
                with checkpoint_scope(cancellation.check):
                    original_bytes=captured.to_json_document().to_bytes()
                    config_bytes=config.to_json_document().to_bytes()
                    model_sha=hashlib.sha256(original_bytes).hexdigest()
                options=captured.effective_options
                duration=options.duration.total_seconds()
                output_directory=host_path(config.output_directory,relative_to=relative_to).resolve()
                if config.input_directory:
                    input_directory=host_path(config.input_directory,relative_to=relative_to).resolve()
                elif captured.document is not None and captured.document.source:
                    input_directory=Path(captured.document.source).absolute().parent
                else:
                    input_directory=Path(relative_to).resolve() if relative_to is not None else None
                phase='inventory'
                with checkpoint_scope(cancellation.check):
                    plans=inventory(captured,input_directory=input_directory,working_directory=output_directory,directory_adapters=self.directory_adapters)
                protected=[plan.original for plan in plans if plan.original is not None and plan.use.access!='write']
                for document in (captured.document,captured.json_document):
                    if document is not None and document.source:protected.append(Path(document.source).absolute())
                run_paths=config.resolved_paths(relative_to=relative_to)
                main_targets={key:host_path(getattr(run_paths,key)).resolve() for key in ('input','report','output')}
                destinations=[(path,False) for path in main_targets.values()]
                from ._directory_outputs import layout as output_layout
                destinations.extend(output_layout(plans,asset_name).targets)
                destinations.append((output_directory/asset_name,True))
                transaction=OutputTransaction(run_id,overwrite=config.overwrite,protected=protected,
                    protected_directories=tuple(p.original for p in plans if p.original is not None and p.use.kind=='directory' and p.use.access!='write' and p.use.role!='swmm:temporary_directory'),
                    directory_limits=self.directory_limits)
                transaction.reserve(destinations, checkpoint=cancellation.check)
                temp_base=next((plan.original for plan in plans if plan.use.role=='swmm:temporary_directory'),output_directory)
                transaction.ensure_directory(temp_base)
                workspace=Workspace(temp_base,run_id)
                transaction.track_workspace(workspace,cleanup_on_crash=not config.keep_failed_artifacts)
                phase='capture';emit('preparing',force=True)
                with checkpoint_scope(cancellation.check):
                    resources,outputs,staging_issues=stage(captured,plans,workspace.path,asset_name,checkpoint=cancellation.check,max_bytes=min(config.file_inspection_limit,self.directory_limits.total_bytes))
                issues.extend(staging_issues)
                phase='preflight'
                transaction.journal.sync(transaction,phase=phase)
                with checkpoint_scope(cancellation.check):
                    validation=captured.validate(for_run=True,normalize=config.normalize_inp)
                issues.extend(validation.diagnostics);validation.raise_for_errors()
                from ._runner_directories import enabled
                directory_run=enabled(resources)
                if directory_run:
                    with checkpoint_scope(cancellation.check):
                        document=captured.to_document(normalize=config.normalize_inp)
                        # Execution uses a defined UTF-8 INP even if the original document
                        # was decoded from a different encoding. Resource bytes stay exact.
                        input_bytes=document.text.encode('utf-8')
                        document=InpDocument.from_bytes(input_bytes)
                        encoded_validation=captured._validate_native_document(document)
                        issues.extend(encoded_validation.diagnostics);encoded_validation.raise_for_errors()
                        source_files={key:workspace.path/('model'+suffix) for key,suffix in (('input','.inp'),('report','.rpt'),('output','.out'))}
                        write_record_bytes(source_files['input'],input_bytes,description='private INP')
                        snapshot=RunSnapshot(run_id=run_id,created_at=created,model_json=original_bytes,config_json=config_bytes,
                            model_sha256=model_sha,input_bytes=input_bytes,input_sha256=hashlib.sha256(input_bytes).hexdigest(),
                            profile=captured.profile,units=captured.units,options=captured.effective_options,backend=info,resources=resources,
                            execution_directory=str(workspace.path))
                    with checkpoint_scope(cancellation.check):
                        inspection_model_bytes=captured.to_json_document().to_bytes()
                files=check_files(captured,input_directory=workspace.path,working_directory=workspace.path,
                    max_bytes=config.file_inspection_limit,inspectors=self.inspectors,interface_manifests=manifests,
                    backend_capabilities=info.capabilities,directory_adapters=self.directory_adapters,checkpoint=cancellation.check,
                    _prepared_directory_outputs=tuple(workspace.path/r.relative_path for r in resources
                        if r.active and r.kind=='directory' and r.access=='write' and r.role!='swmm:temporary_directory'))
                issues.extend(files.report.diagnostics);files.report.raise_for_errors()
                for check in files.checks:
                    use=check.use
                    if use.active and use.required and use.access!='write' and use.role!='swmm:temporary_directory' and (
                        check.inspection is None or check.inspection.status!='validated'):
                        reject('run.incomplete_inspection',f'Required {use.role} inspection is incomplete; add an inspector or increase its byte budget',
                            subject=file_subject(use),diagnostics=resource_diagnostics)
                if not directory_run:
                    with checkpoint_scope(cancellation.check):
                        document=captured.to_document(normalize=config.normalize_inp)
                        # Execution uses a defined UTF-8 INP even if the original document
                        # was decoded from a different encoding. Resource bytes stay exact.
                        input_bytes=document.text.encode('utf-8')
                        document=InpDocument.from_bytes(input_bytes)
                        encoded_validation=captured._validate_native_document(document)
                        issues.extend(encoded_validation.diagnostics);encoded_validation.raise_for_errors()
                        source_files={key:workspace.path/('model'+suffix) for key,suffix in (('input','.inp'),('report','.rpt'),('output','.out'))}
                        write_record_bytes(source_files['input'],input_bytes,description='private INP')
                        snapshot=RunSnapshot(run_id=run_id,created_at=created,model_json=original_bytes,config_json=config_bytes,
                            model_sha256=model_sha,input_bytes=input_bytes,input_sha256=hashlib.sha256(input_bytes).hexdigest(),
                            profile=captured.profile,units=captured.units,options=captured.effective_options,backend=info,resources=resources,
                            execution_directory=str(workspace.path))
                else:
                    with checkpoint_scope(cancellation.check):
                        if captured.to_json_document().to_bytes()!=inspection_model_bytes or captured.to_document(normalize=config.normalize_inp).text.encode('utf-8')!=input_bytes:
                            reject('run.inspector_model_changed','A resource inspector changed the captured execution Model')
                with checkpoint_scope(cancellation.check):
                    prepare_run=getattr(backend,'prepare_run',None)
                    if prepare_run:
                        from .backend import BackendRunPlan
                        execution_plan=prepare_run(captured,config,snapshot,artifact_directory=Path(asset_name)/'backend')
                        if type(execution_plan) is not BackendRunPlan or type(execution_plan.parameters) is not JsonDocument:
                            raise TypeError('Backend preparation requires a BackendRunPlan with immutable JSON parameters')
                        issues.extend(execution_plan.diagnostics)
                        ValidationReport(diagnostics=execution_plan.diagnostics).raise_for_errors()
                        snapshot=replace(snapshot,backend_settings=execution_plan.parameters.to_bytes())
                        backend_products=execution_plan.artifacts
                        seen_products=set()
                        for product in backend_products:
                            path=workspace.path/product.relative_path
                            if not path.resolve().is_relative_to(workspace.path/asset_name) or path in seen_products or path.exists():
                                raise ValueError('Backend artifact must be a unique unused file below the run asset directory')
                            seen_products.add(path);path.parent.mkdir(parents=True,exist_ok=True)
                    active_kinds={record.role.rsplit('.',1)[1].upper() for record in resources
                                  if record.active and record.access!='write' and record.role.startswith('swmm:interface.')}
                    capture_kinds=active_kinds | {record.role.rsplit('.',1)[1].upper() for record in resources
                        if record.active and record.access=='write' and record.role.startswith('swmm:interface.')}
                    cache_contexts={kind:self.cache_policies.capture(kind,snapshot) for kind in capture_kinds}
                    for kind in (set(dict(config.cache_reuse)) | evidence_by_kind.keys()) - active_kinds:
                        issues.append(Diagnostic(code='run.unused_cache_policy',severity=Severity.WARNING,
                            message=f'{kind} reuse policy/evidence was supplied but this run does not consume it'))
                    for kind,evidence in producers.items():
                        if kind not in active_kinds:
                            issues.append(Diagnostic(code='run.unused_producer',severity=Severity.WARNING,
                                message=f'{kind} producer was supplied but this run does not consume it'))
                            continue
                        evidence.artifact.read_bytes()
                        reuse=evidence.reuse_evidence
                        if reuse is not None and (reuse.cache_sha256!=evidence.artifact.sha256
                            or reuse.context.input_sha256!=evidence.producer.input_sha256
                            or reuse.context.engine_sha256!=evidence.producer.backend.sha256):
                            reject('run.cache_evidence','Producer condition evidence differs from its artifact/snapshot')
                        if evidence.producer.backend.sha256!=info.sha256:
                            reject('run.cache_engine',f'{kind} producer used different native bytes')
                    for record in resources:
                        if record.active and record.access!='write' and record.role.startswith('swmm:interface.'):
                            kind=record.role.rsplit('.',1)[1].upper()
                            evidence=producers.get(kind);manifest=manifests.get(kind)
                            if manifest is not None and getattr(manifest,'engine_sha256',None) not in (None,info.sha256):
                                reject('run.cache_engine',f'{kind} asserted engine differs from this backend')
                            condition_evidence=evidence_by_kind.get(kind)
                            if condition_evidence is not None:
                                checked=next((check.inspection for check in files.checks
                                    if check.use.owner==record.owner and check.use.path==record.field),None)
                                facts=dict(checked.facts) if checked is not None else {}
                                policy=condition_evidence.context.policy
                                if (policy==RDII_POLICY and 'node_indices' not in facts or
                                    policy==HOTSTART_POLICY and facts.get('version')!=4):
                                    reject('run.cache_evidence','Condition policy does not cover the inspected cache format/version')
                            try:
                                reuse=self.cache_policies.assess(condition_evidence,cache_contexts[kind],
                                    cache_sha256=record.sha256,intent=config.cache_intent(kind),
                                    origin='runner-observed' if evidence else 'caller-asserted')
                            except ValueError as error:reject('run.cache_evidence',str(error))
                            consumed.append(CacheConsumption(kind=kind,sha256=record.sha256,
                                producer_run_id=evidence.producer.run_id if evidence else None,
                                producer_input_sha256=getattr(manifest,'producer_input_sha256',None),
                                producer_model_sha256=evidence.producer.model_sha256 if evidence else None,
                                producer_engine_sha256=getattr(manifest,'engine_sha256',None),consumer_model_sha256=model_sha,
                                verification='runner-observed-layout' if evidence else 'caller-asserted-layout' if manifest else 'format-only',reuse=reuse))
                            if not reuse.allowed:
                                reject('run.cache_conditions',f'{kind} requires matching captured production conditions: {reuse.status}; differences={reuse.differences}; reasons={reuse.reasons}')
                            if reuse.intent=='frozen':
                                issues.append(Diagnostic(code='run.cache_frozen',severity=Severity.WARNING,
                                    message=f'{kind} explicitly freezes cached history; production conditions are {reuse.status}: {reuse.differences or reuse.reasons}'))
                            elif kind in ('HOTSTART','RUNOFF','RDII') and reuse.status!='matched':
                                issues.append(Diagnostic(code='run.cache_physical_scope',severity=Severity.WARNING,
                                    message=f'{kind} production conditions are {reuse.status}: {reuse.differences or reuse.reasons}; bytes/layout checks alone do not certify physical reuse'))
            else:
                phase='checkpoint_prepare';emit('preparing',force=True)
                with checkpoint_scope(cancellation.check):
                    input_bytes=snapshot.input_bytes;original_bytes=snapshot.model_json;model_sha=snapshot.model_sha256
                    document=InpDocument.from_bytes(input_bytes)
                    output_directory=host_path(config.output_directory,relative_to=relative_to).resolve()
                    run_paths=config.resolved_paths(relative_to=relative_to)
                    main_targets={key:host_path(getattr(run_paths,key)).resolve() for key in ('input','report','output')}
                    destinations=[(path,False) for path in main_targets.values()]+[(output_directory/asset_name,True)]
                    for path,_ in destinations:
                        if path.is_relative_to(archive.directory) or archive.directory.is_relative_to(path):
                            reject('run.checkpoint_output','Output overlaps the checkpoint being resumed')
                    protected=[archive.directory]
                    for path in archive.directory.rglob('*'):
                        cancellation.check()
                        if path.is_file():protected.append(path)
                    transaction=OutputTransaction(attempt_id,overwrite=config.overwrite,protected=protected,directory_limits=self.directory_limits)
                    transaction.reserve(destinations, checkpoint=cancellation.check)
                    workspace=Workspace(output_directory,attempt_id,nested=True)
                    transaction.track_workspace(workspace,cleanup_on_crash=not config.keep_failed_artifacts)
                    materialized=archive.materialize(workspace.path,schema=_resume_schema,checkpoint=cancellation.check)
                    snapshot=replace(materialized,backend=info)
                    (workspace.path/asset_name).mkdir(exist_ok=True)
                    source_files={key:workspace.path/('model'+suffix) for key,suffix in (('input','.inp'),('report','.rpt'),('output','.out'))}
                    resources=snapshot.resources
                    uses={(v.owner.canonical,v.path):v for v in captured.file_uses()}
                    outputs=[]
                    for row in resources:
                        if row.active and row.access=='write' and row.role!='swmm:temporary_directory':
                            use=uses.get((row.owner.canonical,row.field))
                            if use is None or use.file.path!=row.relative_path or use.role!=row.role or use.access!='write':
                                reject('run.checkpoint_resources','Restored output consumer differs from its snapshot')
                            outputs.append((use,None,Path(row.relative_path)))
                    outputs=tuple(outputs)
                    cache_contexts={v.kind:v for v in archive.context.cache_contexts}
                    backend_products=archive.context.backend_artifacts
                    if snapshot.backend_settings is not None:
                        from .backend import BackendRunPlan
                        execution_plan=BackendRunPlan(parameters=JsonDocument.from_bytes(snapshot.backend_settings),artifacts=backend_products)
                    for product in backend_products:(workspace.path/product.relative_path).parent.mkdir(parents=True,exist_ok=True)
            if checkpoints is not None:
                checkpoint_directory=host_path(checkpoints.directory,relative_to=relative_to).resolve()
                if checkpoint_directory.is_relative_to(workspace.root):
                    reject('run.checkpoint_directory','Checkpoints must outlive the execution workspace')
                if archive is not None and checkpoint_directory.is_relative_to(archive.directory):
                    reject('run.checkpoint_directory','Checkpoint storage cannot modify the resumed archive')
                for target in transaction.targets:
                    if checkpoint_directory==target.path or target.directory and checkpoint_directory.is_relative_to(target.path):
                        reject('run.checkpoint_directory','Checkpoint storage overlaps a publication target')
                checkpoint_directory.mkdir(parents=True,exist_ok=True)
            phase='open';emit('opening',force=True)
            with checkpoint_scope(cancellation.check):
                verify_resources(resources,workspace.path,checkpoint=cancellation.check,diagnostics=resource_diagnostics,initial_execution=archive is None)
                from ._runner_directories import admission
                admission(resources,workspace.path/asset_name,limits=self.directory_limits,checkpoint=cancellation.check)
                if not input_matches(source_files['input'],input_bytes):reject('run.input_changed','Generated INP changed before open')
                expected=tuple(('swmm:'+name,tuple(captured.collection('swmm:'+name))) for name in ('raingages','subcatchments','nodes','links'))
            session_options={}
            # Only the known native session factory participates. An overriding
            # third-party factory must not acquire permissions merely by inheritance.
            if getattr(backend.session,'__func__',None) is _NATIVE_SESSION_FACTORY:
                session_options['_execution_guard']=transaction.prepare_execution(workspace)
            session_requested=True
            transaction.workspace_execution(workspace,state='starting')
            session_use=_SessionUse(backend.session(working_directory=workspace.path,
                    call_timeout=cancellation.timeout(config.native_call_timeout.total_seconds()),
                    poll_interval=config.cancellation_poll_interval.total_seconds(),cancel_event=cancellation,
                    **session_options))
            with session_use as session:
                transaction.workspace_execution(workspace,state='active',identity=getattr(session,'worker_identity',None))
                if session.info.sha256!=info.sha256 or session.info.engine_version!=info.engine_version:
                    reject('run.backend_changed','Actual session library differs from the capability probe')
                if archive is not None:
                    if replace(session.info,library=info.library,origin=info.origin)!=info:
                        reject('run.checkpoint_backend','Actual session differs from the checkpoint backend')
                else:
                    info=session.info;snapshot=replace(snapshot,backend=info)
                if checkpoints is not None or archive is not None:
                    opener=getattr(session,'open_checkpoint',None)
                    if not callable(opener):reject('run.checkpoint_backend','Backend does not provide checkpoint sessions')
                    checkpoint_options={}
                    # Scalar file consumers can also have Model-only identities
                    # and codec-specific ordering. Verify them with the captured
                    # schema in the parent before sending data-only declarations.
                    if getattr(backend.session,'__func__',None) is _NATIVE_SESSION_FACTORY:
                        checkpoint_options['schema']=captured._schema
                    engine_objects=opener(snapshot,expected=expected,**checkpoint_options)
                else:
                    engine_objects=session.open(source_files['input'],source_files['report'],source_files['output'],expected=expected)
                    if session.flow_units!=('CFS','GPM','MGD','CMS','LPS','MLD').index(captured.units.flow_units):
                        reject('run.engine_units','Loaded native units differ from the captured Model')
                    if execution_plan is not None:session.configure_execution(execution_plan.parameters.data)
                    phase='start';session.start(save_results=True)
                if session.flow_units!=('CFS','GPM','MGD','CMS','LPS','MLD').index(captured.units.flow_units):
                    reject('run.engine_units','Loaded native units differ from the captured Model')
                if archive is not None:
                    phase='checkpoint_restore';emit('restoring',force=True)
                    restored=session.restore_checkpoint(archive.state)
                    restored_outputs=restored.outputs
                    from ._checkpoint_directory_outputs import roots as output_directory_roots
                    allowed_directories={workspace.path/name for name in output_directory_roots(snapshot)}
                    if any(getattr(v,'directory',None) is not None and v.directory not in allowed_directories for v in restored_outputs):
                        reject('run.checkpoint_output','Restored stream claims an undeclared output directory')
                    restored_stamps={v.destination:self._checkpoint_output_stamp(v,workspace.path,cancellation.check) for v in restored_outputs}
                    issues.extend(Diagnostic(code='run.checkpoint_cleanup',severity=Severity.WARNING,message=str(v)) for v in restored.cleanup)
                    emit('resumed',force=True)
                phase='step'
                while True:
                    cancellation.check()
                    result=session.step(max_steps=config.step_batch_size);steps+=result.steps
                    elapsed=duration if result.finished else result.elapsed_days*86400
                    emit('running',force=steps==result.steps or result.finished)
                    if result.finished:break
                    if checkpoints is not None and elapsed>=next_checkpoint:
                        phase='checkpoint_save'
                        context=RunnerContext(asset_directory=asset_name,diagnostics=ValidationReport(diagnostics=tuple(issues)),
                            consumed_caches=tuple(consumed),cache_contexts=tuple(cache_contexts.values()),
                            backend_artifacts=backend_products,steps=steps,continuations=continuations)
                        saved=capture_checkpoint(session,context,checkpoint_directory/('checkpoint-'+attempt_id+f'-{steps:012d}'),
                                                 checkpoint=cancellation.check)
                        next_checkpoint=(int(elapsed/checkpoints.interval.total_seconds())+1)*checkpoints.interval.total_seconds()
                        if checkpoints.on_saved is not None:
                            phase='checkpoint_callback';checkpoints.on_saved(saved)
                        cancellation.check();phase='step'
                phase='end';balance=session.end()
                with checkpoint_scope(cancellation.check):
                    result_context=context_from_model(captured,info,input_sha256=snapshot.input_sha256).with_fact('swmm:completed','yes')
                    balance=balance.with_context(result_context)
                    if execution_plan is not None:backend_results=JsonDocument.from_data(session.execution_results())
                if not captured.effective_report.settings.disabled:
                    phase='report';session.report()
                phase='close'
            native_completed=True
            if restored_outputs:
                phase='checkpoint_outputs'
                self._adopt_checkpoint_outputs(restored_outputs,restored_stamps,workspace.path,cancellation.check)
                outputs_adopted=True
            phase='verify'
            with checkpoint_scope(cancellation.check):
                verify_resources(resources,workspace.path,checkpoint=cancellation.check,diagnostics=resource_diagnostics)
                # Bind final resource content before report extensions or finalizing callbacks.
                from ._runner_directories import collect, enabled
                if enabled(resources):
                    directory_artifacts,directory_group_artifacts=collect(resources,workspace.path,adapters=self.directory_adapters,limits=self.directory_limits,complete=True,checkpoint=cancellation.check)
                if not input_matches(source_files['input'],input_bytes):reject('run.input_changed','Executed INP was modified during the session')
                observed={path:digest_file(path,checkpoint=cancellation.check) for path in checkpointed(source_files.values())}
                observed.update({workspace.path/relative:digest_file(workspace.path/relative,checkpoint=cancellation.check)
                                 for _,_,relative in checkpointed(outputs) if (workspace.path/relative).is_file()})
                observed.update({workspace.path/product.relative_path:digest_file(workspace.path/product.relative_path,
                    checkpoint=cancellation.check) for product in checkpointed(backend_products)})
                from ..io.output_metadata import OutputMetadata
                from ..io.report_document import ReportDocument
                output_metadata=OutputMetadata.read(source_files['output'])
                output_metadata=replace(output_metadata,semantics=info.output_semantics,
                    producer=info.key,averages=captured.effective_report.settings.averages,result_context=result_context)
                if output_metadata.flow_units!=captured.units.flow_units or output_metadata.engine_version!=info.engine_version:
                    reject('run.output_profile','OUT profile differs from the actual native session')
                for name in checkpointed(('subcatchments','nodes','links')):
                    wanted={ref.canonical.key for ref in checkpointed(getattr(captured.effective_report,name))}
                    expected_names=tuple(canonical_key(name) for name in checkpointed(engine_objects.names('swmm:'+name)) if canonical_key(name) in wanted)
                    if tuple(canonical_key(name) for name in checkpointed(output_metadata.names('swmm:'+name)))!=expected_names:
                        reject('run.output_identity',f'Actual OUT {name} differs from the captured REPORT selection')
                pollutant_names=() if captured.effective_options.values.ignore_quality else tuple(line.values[0] for line in checkpointed(document.records('POLLUTANTS')))
                if tuple(canonical_key(name) for name in checkpointed(output_metadata.names('swmm:pollutants')))!=tuple(canonical_key(name) for name in checkpointed(pollutant_names)):
                    reject('run.output_pollutants','Actual OUT pollutants differ from the captured input')
                if output_metadata.pollutant_units!=tuple(captured.pollutants[name].units for name in checkpointed(pollutant_names)):
                    reject('run.output_pollutant_units','Actual OUT concentration units differ from the Model')
            phase='report_read'
            from ..io.report_document import SWMM_UTF8_REPORT
            from ..io.report import ReportContext, read_report_tables
            from ..results import ResultSource
            report_profile=(SWMM_UTF8_REPORT if info.engine_version==52004 and
                isinstance(backend,(StandardBackend,FlexiblePondingBackend)) else None)
            with checkpoint_scope(cancellation.check):
                report_document=ReportDocument.read(source_files['report'],encoding=config.report_read.encoding,
                    on_decode_error=config.report_read.on_decode_error,profile=report_profile,
                    max_bytes=config.report_read.max_bytes,source=str(main_targets['report']),checkpoint=cancellation.check)
                issues.extend(report_document.diagnostics.diagnostics)
                report_tables=read_report_tables(report_document,config.report_read.tables,registry=self.report_tables,
                    source=ResultSource(format='swmm:rpt',path=str(main_targets['report']),
                        sha256=hashlib.sha256(report_document.raw).hexdigest(),run_id=run_id,
                        input_sha256=snapshot.input_sha256,backend_sha256=info.sha256,
                        engine_version=info.engine_version,encoding=report_document.encoding),
                    context=ReportContext(producer=info.key,numerical_policy=info.numerical_policy,
                        result_context=result_context,
                        report_start=snapshot.options.report_start,averages=output_metadata.averages,
                        accounting=dict(info.output_semantics).get('easysewer:ponding-accounting'),
                        flow_units=captured.units.flow_units,
                        rain_gage_sources=tuple((line.values[0],line.values[4].upper()) for line in checkpointed(document.records('RAINGAGES'))),
                        pollutants=tuple(line.values[0] for line in checkpointed(document.records('POLLUTANTS')))),
                    on_error=config.report_read.on_table_error,checkpoint=cancellation.check)
                for table in checkpointed(report_tables):issues.extend(table.diagnostics.diagnostics)
            phase='publication';emit('finalizing',force=True)
            with checkpoint_scope(cancellation.check):
                verify_resources(resources,workspace.path,checkpoint=cancellation.check,diagnostics=resource_diagnostics)
                for path,identity in checkpointed(observed.items()):
                    if digest_file(path,checkpoint=cancellation.check)!=identity:
                        reject('run.output_changed','Native input/output changed after the session completed: '+str(path))
                sources={main_targets[key]:path for key,path in checkpointed(source_files.items())}
                sources[output_directory/asset_name]=workspace.path/asset_name
                reserved_outputs={target.path for target in transaction.targets}
                for use,destination,relative in checkpointed(outputs):
                    path=workspace.path/relative
                    if (path.is_dir() if use.kind=='directory' else path.is_file()):
                        if destination is not None and destination.resolve() in reserved_outputs:sources[destination.resolve()]=path
                    else:issues.append(Diagnostic(code='run.output_not_created',severity=Severity.WARNING,
                        message=f'Native did not create declared {use.role}: {destination}'))
                # Build producer evidence before publication so a malformed cache
                # cannot turn a committed set of outputs into a failed transaction.
                cache_manifests=self._cache_manifests(captured,outputs,workspace.path,snapshot,engine_objects,config,issues,cancellation.check)
                records=self._execution_record(workspace.path,asset_name,snapshot,engine_objects,output_metadata,balance,cache_manifests,backend_results,report_document,report_tables,cache_contexts,consumed,continuations,checkpoint=cancellation.check)
                records+=tuple((product.role,Path(product.relative_path),None,()) for product in checkpointed(backend_products))
                for key,path in checkpointed(source_files.items()):
                    sha,size=digest_file(path,checkpoint=cancellation.check)
                    artifacts.append(FileArtifact(role='run:'+key,path=str(main_targets[key]),sha256=sha,size=size,complete=True))
                for record in checkpointed(resources):
                    if record.directory_group is not None:continue
                    path=workspace.path/record.relative_path
                    if record.kind=='file' and path.is_file():
                        sha,size=digest_file(path,checkpoint=cancellation.check)
                        artifact=FileArtifact(role=record.role if record.access=='write' else 'run:resource',
                            path=str(output_directory/record.relative_path),sha256=sha,size=size,complete=True,
                            owner=record.owner,field=record.field,declared_path=record.original_path)
                        artifacts.append(artifact)
                        if (record.owner,record.field) in cache_manifests:
                            kind,manifest=cache_manifests[(record.owner,record.field)]
                            produced.append(ProducedCache(kind=kind,artifact=artifact,manifest=manifest,producer=snapshot,
                                applicability=result_context.artifact(kind),reuse_evidence=CacheEvidence(cache_sha256=sha,context=cache_contexts[kind])))
                for role,relative,owner,field in checkpointed(records):
                    sha,size=digest_file(workspace.path/relative,checkpoint=cancellation.check)
                    artifacts.append(FileArtifact(role=role,path=str(output_directory/relative),sha256=sha,size=size,
                        complete=True,owner=owner,field=field))
                from ._runner_directories import rebase, publication_trees, enabled
                expected_sources=dict(observed)
                for path in checkpointed((workspace.path/asset_name).rglob('*')):
                    if path.is_file():expected_sources[path]=digest_file(path,checkpoint=cancellation.check)
                # Native outputs must still match the bytes observed when the
                # session ended, including outputs inside the asset directory.
                expected_sources.update(observed)
                # Original captured resource digests remain authoritative if a
                # file was modified between inventory and publication copying.
                expected_sources.update({workspace.path/record.relative_path:(record.sha256,record.size)
                                         for record in checkpointed(resources) if record.sha256 is not None and record.directory_group is None})
                publication_options={}
                if enabled(resources):
                    trees=publication_trees(sources,transaction,directory_artifacts,directory_group_artifacts,resources,workspace.path/asset_name,limits=self.directory_limits,checkpoint=cancellation.check)
                    publication_options['expected_trees']=trees
                    directory_artifacts,directory_group_artifacts=rebase(directory_artifacts,directory_group_artifacts,workspace.path,output_directory)
                    # Validate the complete success contract before committing outputs.
                    RunResult(run_id=run_id,status='succeeded',diagnostics=ValidationReport(diagnostics=tuple(issues)),
                        artifacts=tuple(artifacts),directory_artifacts=directory_artifacts,directory_group_artifacts=directory_group_artifacts,
                        snapshot=snapshot,backend=info,engine_objects=engine_objects,mass_balance=balance,native_completed=True,
                        produced_caches=tuple(produced),consumed_caches=tuple(consumed),output_metadata=output_metadata,
                        report_document=report_document,report_tables=report_tables,backend_results=backend_results,continuations=continuations)
            transaction.publish(sources,checkpoint=cancellation.check,expected_sources=expected_sources,**publication_options)
            report_document=replace(report_document,source=str(main_targets['report']))
            status='succeeded'
        except Exception as error:
            cause=error
            resource_report=getattr(error,'_easysewer_resource_diagnostics',None)
            if resource_report is not None:
                issues.extend(resource_report.diagnostics)
            if restored_outputs and not outputs_adopted:
                current={v.destination:v.path for v in restored_outputs}
                source_files={key:current.get(path,path) for key,path in source_files.items()}
            if getattr(error,'checkpoint_committed',None) is True:
                issues.append(Diagnostic(code='run.checkpoint_committed',message='Restoration committed before failure; this attempt cannot be retried on the same session'))
                if not restored_outputs:
                    # A malformed/fatal restore response may follow a native
                    # commit without returning a validated output mapping.
                    # Startup files are not evidence of the restored attempt.
                    restore_outputs_unknown=True
                    source_files={key:path for key,path in source_files.items() if key=='input'}
                    issues.append(Diagnostic(code='run.checkpoint_outputs_unavailable',severity=Severity.WARNING,
                        message='Committed restoration returned no verified output mapping; startup outputs are omitted'))
            if getattr(error,'runner_checkpoint_cleanup',None):
                issues.append(Diagnostic(code='run.checkpoint_cleanup',severity=Severity.WARNING,message=error.runner_checkpoint_cleanup))
            if isinstance(error,ValidationError):
                # Preflight records a report before raise_for_errors(). Do not
                # append those same objects again while handling its exception.
                # Separate diagnostics with equal values keep their own entries.
                recorded={id(issue) for issue in issues}
                issues.extend(issue for issue in error.report.diagnostics if id(issue) not in recorded)
                status='rejected' if not native_completed else 'failed'
            elif isinstance(error,_Interrupted):status=error.status
            elif isinstance(error,SessionCancelled):status=cancellation.status() or 'cancelled'
            elif isinstance(error,SessionTimeout):status='timed_out'
            failure=RunFailure(stage=phase,exception_type=type(error).__name__,message=str(error),
                native=error.failure if isinstance(error,SessionError) else None,
                cleanup=error.cleanup if isinstance(error,SessionError) else (),
                stderr=error.stderr if isinstance(error,SessionError) else '',
                worker_returncode=error.returncode if isinstance(error,SessionError) else None)
            if transaction is not None and transaction.journal is not None:
                transaction.journal.data['failure']=(phase,type(error).__name__,str(error))
                try:transaction.journal.sync(transaction,phase=phase)
                except Exception as journal_error:
                    transaction.issues.append(f'Cannot record primary failure in {transaction.journal.path}: {journal_error}')
            issues.append(Diagnostic(code='run.'+status,message=f'{phase}: {error}'))
            if (isinstance(error,SessionError) and error.failure.stage=='open' and error.failure.code==303
                    and 'input path cannot be resolved or exceeds native path buffer' in error.failure.message
                    and info is not None and info.platform=='Windows' and 'easysewer:path-io:1' in info.capabilities):
                issues.append(Diagnostic(code='run.native_path',severity=Severity.WARNING,
                    message='Windows native path resolution requires a working-directory path representable '
                    'by the process ANSI code page and within the 4095-byte native path limit. '
                    'If a verified existing ASCII short-name alias is unavailable, '
                    'for Runner.run set Model Options.temp_directory (TEMPDIR) to a shorter compatible writable '
                    'directory; output_directory can remain Unicode. Explicit TEMPDIR is never relocated.'))
            artifacts=[];produced=[]
            directory_artifacts=directory_group_artifacts=()
            output_metadata=report_document=None
            report_tables=()
            report_path=source_files.get('report')
            if report_path is not None:
                try:
                    from ..io.report_document import ReportCapture, SWMM_UTF8_REPORT
                    profile=(SWMM_UTF8_REPORT if info is not None and info.engine_version==52004 and
                        isinstance(backend,(StandardBackend,FlexiblePondingBackend)) else None)
                    # Interruption must not restart a potentially minutes-long
                    # parse of the same report. Keep a labelled byte prefix;
                    # full files follow the existing artifact retention policy.
                    capture_limit=(min(config.report_read.max_bytes,64*1024)
                        if status in ('cancelled','timed_out') else config.report_read.max_bytes)
                    failure_report=ReportCapture.read(report_path,max_bytes=capture_limit,
                        encoding=config.report_read.encoding,profile=profile)
                    issues.extend(failure_report.document.diagnostics.diagnostics)
                    if failure_report.truncated:
                        issues.append(Diagnostic(code='run.failure_report_truncated',severity=Severity.WARNING,
                            message=f'Failed-run report captured only up to {capture_limit} bytes; prefix hash is not the full file hash.'))
                except FileNotFoundError:pass  # Native may fail before creating any report.
                except Exception as capture_error:
                    issues.append(Diagnostic(code='run.failure_report_capture',severity=Severity.WARNING,message=str(capture_error)))
            if workspace is not None and config.keep_failed_artifacts:
                workspace.retained=True
                # Private failures remain in their owned location; no requested
                # destination or old successful output is replaced.
                try:
                    for key,path in source_files.items():
                        if path.is_file():
                            sha,size=digest_file(path)
                            artifacts.append(FileArtifact(role='run:'+key,path=str(path),sha256=sha,size=size,complete=False))
                    for record in resources:
                        if record.directory_group is not None:continue
                        if restore_outputs_unknown and record.access=='write':continue
                        path=workspace.path/record.relative_path
                        if restored_outputs and not outputs_adopted:path=current.get(path,path)
                        if record.kind=='file' and path.is_file():
                            sha,size=digest_file(path)
                            artifacts.append(FileArtifact(role=record.role if record.access=='write' else 'run:resource',
                                path=str(path),sha256=sha,size=size,complete=False,owner=record.owner,field=record.field,
                                declared_path=record.original_path))
                    for product in backend_products:
                        if restore_outputs_unknown:continue
                        path=workspace.path/product.relative_path
                        if restored_outputs and not outputs_adopted:path=current.get(path,path)
                        if path.is_file():
                            sha,size=digest_file(path)
                            artifacts.append(FileArtifact(role=product.role,path=str(path),sha256=sha,size=size,complete=False))
                except Exception as collection_error:
                    issues.append(Diagnostic(code='run.failed_artifact_inventory',severity=Severity.WARNING,message=str(collection_error)))
                from ._runner_directories import collect
                def directory_error(label,error):
                    issues.append(Diagnostic(code='run.failed_directory_artifact',severity=Severity.WARNING,message=f'{label}: {error}'))
                try:
                    if snapshot is not None:
                        directory_artifacts,directory_group_artifacts=collect(resources,workspace.path,adapters=self.directory_adapters,limits=self.directory_limits,complete=False,on_error=directory_error,omit_outputs=restore_outputs_unknown)
                    elif resources:
                        issues.append(Diagnostic(code='run.directory_snapshot_unavailable',severity=Severity.WARNING,message='Captured resource files remain in the retained workspace; execution snapshot was not completed.'))
                except Exception as collection_error:directory_error('directory inventory',collection_error)
        finally:
            if session is not None and session.cleanup_errors:
                issues.extend(Diagnostic(code='run.cleanup',severity=Severity.WARNING,message=str(error)) for error in session.cleanup_errors)
            if workspace is not None:
                try:
                    if not workspace.retained:
                        if transaction is not None:transaction.workspace_cleanup(workspace,requested=True)
                        if session_requested and (session_use is None or not session_use.stopped()):
                            if transaction is None or not transaction.revoke_execution(workspace):
                                raise RuntimeError('Execution shutdown was not confirmed; workspace retained')
                        if transaction is not None:transaction.workspace_cleanup(workspace)
                        workspace.close()
                        if transaction is not None:transaction.workspace_cleanup(workspace,completed=True)
                except Exception as error:
                    workspace.retained=True
                    message=f'{workspace.root}: {error}'
                    issues.append(Diagnostic(code='run.workspace_cleanup',severity=Severity.WARNING,message=message))
                    if transaction is not None:transaction.issues.append('Workspace cleanup pending: '+message)
            if transaction is not None:
                transaction.close()
                issues.extend(Diagnostic(code='run.publication_cleanup',severity=Severity.WARNING,message=message) for message in transaction.issues)
        result=RunResult(run_id=run_id,status=status,diagnostics=ValidationReport(diagnostics=tuple(issues)),
            artifacts=tuple(artifacts),snapshot=snapshot,backend=info,engine_objects=engine_objects,mass_balance=balance,
            failure=failure,native_completed=native_completed,produced_caches=tuple(produced),consumed_caches=tuple(consumed),
            output_metadata=output_metadata,report_document=report_document,report_tables=report_tables,failure_report=failure_report,
            retained_directory=str(workspace.path) if workspace is not None and workspace.retained else None,
            backend_results=backend_results,continuations=continuations,
            directory_artifacts=directory_artifacts,directory_group_artifacts=directory_group_artifacts)
        if raise_on_error and not result.succeeded:raise RunError(result) from cause
        return result

    @staticmethod
    def _checkpoint_output_stamp(item,root,checkpoint):
        if getattr(item,'directory',None) is None:
            return file_state(item.destination,checkpoint=checkpoint) if item.destination.exists() else None
        from ._checkpoint_directory_inputs import _parents
        from ._directory_tree import _node
        root=Path(root).resolve();directory=Path(item.directory);target=Path(item.destination);source=Path(item.path)
        if not directory.is_absolute() or directory==root or not directory.is_relative_to(root) or target==directory or not target.is_relative_to(directory):
            raise ValueError('Restored stream is outside its bound output directory')
        checkpoint();parents=_parents(root,target);_parents(root,source)
        info=_node(target,'file')[1];live=_node(source,'file')[1]
        if (info.st_dev,info.st_ino)!=(live.st_dev,live.st_ino):raise ValueError('Restored stream no longer matches its directory file')
        return ('directory-output',(info.st_dev,info.st_ino),parents)

    @staticmethod
    def _adopt_checkpoint_outputs(outputs, stamps, root, checkpoint):
        """Collect closed restored streams into this attempt's owned workspace.

        Startup destinations must still have their post-restore identities.
        Publication to requested destinations remains a separate transaction.
        Original restored streams stay available if collection is interrupted.
        """
        root=Path(root).resolve()
        def checked(path, *, required):
            path=Path(path)
            if not path.is_absolute() or not path.is_relative_to(root) or path==root:
                raise ValueError('Restored output escaped its execution workspace')
            for item in (path,*path.parents):
                if item==root:break
                if item.is_symlink():raise ValueError('Restored output contains a symbolic link')
            if path.resolve()!=path or required and not path.is_file() or path.exists() and not path.is_file():
                raise ValueError('Restored output requires a regular workspace file')
            return path
        seen=set();identities=set()
        for item in outputs:
            checkpoint()
            source=checked(item.path,required=True);target=checked(item.destination,required=False)
            identity=fingerprint(source)[:2]
            if source==target or target in seen or identity in identities:
                raise ValueError('Restored output aliases another stream')
            seen.add(target);identities.add(identity)
            if getattr(item,'directory',None) is not None:
                if target not in stamps or Runner._checkpoint_output_stamp(item,root,checkpoint)!=stamps[target]:
                    raise ValueError('Bound directory output identity changed after restoration')
                continue
            if source.exists() and target.exists() and source.samefile(target):
                raise ValueError('Restored output aliases another stream')
            if target not in stamps or (file_state(target, checkpoint=checkpoint) if target.exists() else None)!=stamps[target]:
                raise ValueError('Startup output changed after checkpoint restoration')
            temporary=target.with_name('.easysewer-restored-'+uuid.uuid4().hex)
            identity=[]
            try:
                copy_input(source,temporary,checkpoint=checkpoint,on_create=identity.append)
                checkpoint();checked(target,required=False)
                if (file_state(target, checkpoint=checkpoint) if target.exists() else None)!=stamps[target]:
                    raise ValueError('Startup output changed during checkpoint collection')
                os.replace(temporary,target)
            except BaseException as error:
                try:
                    if identity and temporary.exists() and not temporary.is_symlink() and fingerprint(temporary)[:2]==identity[0]:
                        temporary.unlink()
                except BaseException as cleanup:
                    error.runner_checkpoint_cleanup=f'{type(cleanup).__name__}: {cleanup}'
                raise

    @staticmethod
    def _execution_record(*args, checkpoint=None, **kwargs):
        with checkpoint_scope(checkpoint):
            return Runner._execution_record_data(*args, **kwargs)

    @staticmethod
    def _execution_record_data(root, asset_name, snapshot, objects, metadata, balance, manifests, backend_results, report_document, report_tables,cache_contexts,consumed,continuations=()):
        """Persist the observed execution, independent of publication success.

        This is inspectable provenance, not a signed producer attestation or a
        general RunResult deserializer. Typed cache imports remain explicit.
        """
        from ..io.json.run import config_types
        base=Path(asset_name)/'execution';(root/base).mkdir()
        files=[('run:model',base/'model.json',snapshot.model_json,None,()),
               ('run:config',base/'config.json',snapshot.config_json,None,()),
               ('run:executed-input',base/'input.inp',snapshot.input_bytes,None,())]
        for index,((owner,field),(kind,manifest)) in enumerate(checkpointed(manifests.items())):
            files.append(('run:cache-manifest',base/f'cache-{index}-{kind}.json',manifest.to_bytes(),owner,field))
            evidence=CacheEvidence(cache_sha256=manifest.sha256,context=cache_contexts[kind])
            files.append(('run:cache-evidence',base/f'cache-{index}-{kind}-conditions.json',evidence.to_bytes(),owner,field))
        for index,table in enumerate(checkpointed(report_tables)):
            files.append(('run:report-table',base/f'report-table-{index}.json',table.to_json_document().to_bytes(),None,(table.key,)))
        mixed_evidence=any(r.directory_group is not None and r.kind=='file' for r in snapshot.resources)
        grouped_evidence=any(r.directory_group is not None for r in checkpointed(snapshot.resources))
        linked_evidence = grouped_evidence or any(r.tree is not None and r.tree.has_hardlinks for r in checkpointed(snapshot.resources))
        absent_evidence = any(r.initial_relative_path is not None and r.tree is None for r in checkpointed(snapshot.resources))
        mutable_evidence = any(resource.initial_relative_path is not None for resource in checkpointed(snapshot.resources))
        directory_evidence = any(resource.tree is not None or resource.initial_relative_path is not None for resource in checkpointed(snapshot.resources))
        resource_records = []
        for resource in checkpointed(snapshot.resources):
            row = record_asdict(resource)
            if not grouped_evidence:row.pop('directory_group')
            elif not mixed_evidence and row['directory_group'] is not None:
                for view in row['directory_group']['state']['layout']['views']:view.pop('kind')
            # Keep the established file-only schema and exact wire fields.
            if not directory_evidence:
                row.pop('tree')
            if not mutable_evidence:
                row.pop('initial_relative_path')
            if not linked_evidence and row.get('tree') is not None:
                for entry in checkpointed(row['tree']['entries']):entry.pop('hardlink_to')
            resource_records.append(row)
        record=dict(kind='easysewer:execution-record',schema_version='1.7' if mixed_evidence else '1.6' if grouped_evidence else '1.5' if linked_evidence else '1.4' if absent_evidence else '1.3' if mutable_evidence else '1.2' if directory_evidence else '1.1',run_id=snapshot.run_id,
            continuations=[dict(attempt_id=row.attempt_id,execution_run_id=row.execution_run_id,
                checkpoint_sha256=row.checkpoint_sha256,state_sha256=row.state_sha256,
                execution_sha256=row.execution_sha256,started_at=row.started_at.isoformat(),
                simulation_seconds=row.simulation_seconds,steps=row.steps,config=json.loads(row.config_json))
                for row in checkpointed(continuations)],
            result_context=metadata.result_context.to_data(),
            cache_contexts={kind:context.to_data() for kind,context in cache_contexts.items()},
            consumed_caches=[dict(kind=row.kind,sha256=row.sha256,verification=row.verification,reuse=row.reuse.to_data()) for row in consumed],
            cache_applicability=[dict(kind=kind,applicability=metadata.result_context.artifact(kind).to_data())
                                 for kind,manifest in manifests.values()],
            created_at=snapshot.created_at.isoformat(),native_completed=True,
            model_sha256=snapshot.model_sha256,input_sha256=snapshot.input_sha256,
            config_sha256=hashlib.sha256(snapshot.config_json).hexdigest(),
            profile=dict(key=snapshot.profile.key,engine_version=snapshot.profile.engine_version,
                unit_rules=snapshot.profile.unit_rules.key if snapshot.profile.unit_rules else None),
            backend=record_asdict(snapshot.backend),effective_options=config_types().encode(snapshot.options.values),
            backend_settings=json.loads(snapshot.backend_settings) if snapshot.backend_settings else None,
            backend_results=backend_results.data if backend_results else None,
            time=dict(start=snapshot.options.start.isoformat(),end=snapshot.options.end.isoformat(),
                report_start=snapshot.options.report_start.isoformat(),duration_seconds=snapshot.options.duration.total_seconds()),
            resources=resource_records,engine_objects=record_asdict(objects),
            output=dict(engine_version=metadata.engine_version,flow_units=metadata.flow_units,groups=metadata.groups,
                pollutant_units=metadata.pollutant_units,variable_codes=metadata.variable_codes,periods=metadata.periods,
                report_step_seconds=metadata.report_step.total_seconds(),saved_start=metadata.saved_start.isoformat(),
                first_time=metadata.first_time.isoformat() if metadata.first_time else None,
                last_time=metadata.last_time.isoformat() if metadata.last_time else None,
                output_offset=metadata.output_offset,period_bytes=metadata.period_bytes,semantics=metadata.semantics,
                averages=metadata.averages,producer=metadata.producer,input_properties=metadata.input_properties,
                identifier_encoding=metadata.identifier_encoding,result_context=metadata.result_context.to_data()),mass_balance=balance.to_data(),
            report=dict(sha256=hashlib.sha256(report_document.raw).hexdigest(),size=len(report_document.raw),
                requested_encoding=report_document.requested_encoding,encoding=report_document.encoding,
                profile=report_document.profile,decoding_strategy=report_document.decoding_strategy,
                repaired_byte_offsets=report_document.repaired_byte_offsets,
                tables=[dict(key=table.key,status=table.status,reason=table.reason,applicability=record_asdict(table.applicability),artifact=f'report-table-{index}.json')
                        for index,table in enumerate(checkpointed(report_tables))]))
        files.append(('run:execution-record',base/'execution.json',
            (record_dumps(record)+'\n').encode('utf-8'),None,()))
        for _,relative,data,_,_ in checkpointed(files):write_record_bytes(root/relative,data)
        return tuple((role,relative,owner,field) for role,relative,_,owner,field in files)

    @staticmethod
    def _cache_manifests(model, outputs, root, snapshot, objects, config, issues, checkpoint):
        with checkpoint_scope(checkpoint):
            from ..io.hotstart import HotstartLayout
            from ..io.hotstart_manifest import HotstartManifest
            from ..io.runoff_cache import RunoffLayout, RdiiLayout
            from ..io.cache_manifest import CacheManifest
            result={}
            for use,_,relative in checkpointed(outputs):
                if use.role not in ('swmm:interface.hotstart','swmm:interface.runoff','swmm:interface.rdii'):
                    continue
                path=root/relative
                if not path.is_file():continue
                checkpoint();kind=use.role.rsplit('.',1)[1].upper()
                layout={'HOTSTART':HotstartLayout,'RUNOFF':RunoffLayout,'RDII':RdiiLayout}[kind].from_model(model,normalize=config.normalize_inp)
                if kind=='HOTSTART':
                    for name in checkpointed(('nodes','links','subcatchments')):
                        if tuple(canonical_key(v.id) for v in checkpointed(getattr(layout,name)))!=tuple(canonical_key(v) for v in checkpointed(objects.names('swmm:'+name))):
                            reject('run.cache_native_order','Produced hotstart layout differs from actual native identity order')
                elif kind=='RUNOFF' and tuple(canonical_key(v) for v in checkpointed(layout.subcatchments))!=tuple(canonical_key(v) for v in checkpointed(objects.names('swmm:subcatchments'))):
                    reject('run.cache_native_order','Produced runoff layout differs from actual native identity order')
                elif kind=='RDII' and tuple(canonical_key(v) for v in checkpointed(layout.nodes))!=tuple(canonical_key(v) for v in checkpointed(objects.names('swmm:nodes'))):
                    reject('run.cache_native_order','Produced RDII layout differs from actual native identity order')
                sha,size=digest_file(path,checkpoint=checkpoint)
                cls=HotstartManifest if kind=='HOTSTART' else CacheManifest
                manifest=cls(sha256=sha,layout=layout,producer_input_sha256=snapshot.input_sha256,engine_sha256=snapshot.backend.sha256)
                if size<=config.file_inspection_limit:manifest.verify(read_record_bytes(path),layout=layout)
                else:issues.append(Diagnostic(code='run.cache_inspection_limit',severity=Severity.WARNING,
                    message=f'{kind} producer provenance was recorded, but payload inspection exceeds the byte budget'))
                result[(use.owner,use.path)]=(kind,manifest)
            return result
