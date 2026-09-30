"""Capture declared Model file consumers into a private, replayable run tree."""

from dataclasses import dataclass, replace
from contextlib import contextmanager
import os
from pathlib import Path, PureWindowsPath

from ._workspace import copy_input, digest_file
from .results import ResourceSnapshot
from .directory_resources import DirectoryAdapter, directory_adapters as validated_directory_adapters
from ._directory_tree import DirectoryLimits, capture_tree, inspect_tree, verify_tree, _node
from ..model.file_resources import file_references, file_subject, replace_path
from ..model.identity import Ref
from ..model.values import FileReference
from ..validation import Diagnostic, DiagnosticSubject, Severity, ValidationError, ValidationReport
from ..validation._cooperative import checkpointed


def reject(code, message, *, subject=None, related=(), diagnostics=None):
    report = ValidationReport(diagnostics=(Diagnostic(code=code, message=message, subject=subject, related=related),))
    raise ValidationError(diagnostics(report) if diagnostics is not None else report)


def resource_failure(error, code, *, subject, related=(), diagnostics=None):
    """Add evidence without changing the operational exception or its errno."""
    report = ValidationReport(diagnostics=(Diagnostic(code=code, message=str(error), subject=subject, related=related),))
    error._easysewer_resource_diagnostics = diagnostics(report) if diagnostics is not None else report


@contextmanager
def resource_errors(model, use, code):
    try:
        yield
    except ValidationError:
        raise
    except (OSError, ValueError) as error:
        resource_failure(error, code, subject=file_subject(use), diagnostics=model._resolve_diagnostics)
        raise


def host_path(reference, *, relative_to=None):
    path = reference.resolve(relative_to=relative_to)
    if isinstance(path, PureWindowsPath) != (os.name == 'nt'):
        raise ValueError('A foreign path flavor requires an explicit host mapping')
    return Path(path).absolute()


@dataclass(frozen=True, kw_only=True)
class ResourcePlan:
    use: object
    original: Path | None
    directory_adapter: DirectoryAdapter | None = None


def inventory(model, *, input_directory, working_directory, _captured_directories=None, directory_adapters=None):
    if model._store.opaque_constraints:
        reject('run.opaque_consumers', 'Unstructured Model content needs its file/identity codec before isolated execution')
    adapters = validated_directory_adapters(directory_adapters)
    if adapters and _captured_directories is not None:
        raise ValueError('Captured directory declarations and live adapters are separate inventory modes')
    directory_claims = None
    if _captured_directories is not None:
        if type(_captured_directories) is not tuple:
            raise TypeError('Captured directory declarations must be immutable')
        directory_claims = {}
        for record in checkpointed(_captured_directories):
            if type(record) is not ResourceSnapshot or record.kind != 'directory' or record.role == 'swmm:temporary_directory':
                raise TypeError('Expected a captured non-scratch directory declaration')
            key = (record.owner.canonical, record.field)
            if key in directory_claims:
                reject('run.directory_identity', 'Duplicate captured directory owner/field')
            directory_claims[key] = record
    uses = tuple(model.file_uses())
    declared = set()
    for use in checkpointed(uses):
        address = (use.owner.canonical, use.path)
        if address in declared:
            reject('run.duplicate_consumer', 'A file field must have exactly one file-consumer declaration',
                subject=file_subject(use), diagnostics=model._resolve_diagnostics)
        declared.add(address)
    for specification in checkpointed(model._store.specifications):
        for key, row in checkpointed(model.collection(specification.key).items()):
            owner = Ref(collection=specification.key, key=key)
            for path, _ in checkpointed(file_references(row)):
                if (owner.canonical, path) not in declared:
                    reject('run.undeclared_consumer', f'{owner}/{path} contains a FileReference without a file-consumer declaration',
                        subject=DiagnosticSubject(collection=owner.collection, key=owner.key, path=(*path, 'path')),
                        diagnostics=model._resolve_diagnostics)
    result = []
    for use in checkpointed(uses):
        try:
            base = working_directory if use.base == 'working_directory' else input_directory
            path = host_path(use.file, relative_to=base)
        except (OSError, ValueError) as error:
            if use.active and use.required:
                resource_failure(error, 'run.resource_path', subject=file_subject(use), diagnostics=model._resolve_diagnostics)
                raise
            if not isinstance(error, ValueError):
                raise
            path = None
        adapter = None
        if use.kind == 'directory' and use.role != 'swmm:temporary_directory':
            if directory_claims is None:
                adapter = adapters.get(use.format)
                if use.active and adapter is None:
                    reject('run.directory_adapter', f'Directory consumer {use.role} requires a runtime staging adapter',
                        subject=file_subject(use), diagnostics=model._resolve_diagnostics)
            else:
                record = directory_claims.pop((use.owner.canonical, use.path), None)
                if (record is None or any(getattr(record, name) != getattr(use, name)
                        for name in ('role', 'format', 'kind', 'access', 'active', 'required')) or
                        use.file.path != record.relative_path or use.file.base_directory is not None):
                    reject('run.directory_declaration', 'Executed directory adapter declaration differs from its captured resource',
                        subject=file_subject(use), diagnostics=model._resolve_diagnostics)
        result.append(ResourcePlan(use=use, original=path, directory_adapter=adapter))
    if directory_claims:
        reject('run.directory_declaration', 'Captured directory adapter resource is absent from the executed input')
    return tuple(result)


def stage(model, plans, workspace, asset_name, *, checkpoint, max_bytes=None):
    """Only the private Model copy is edited. Original resources are read-only."""
    if max_bytes is not None and (type(max_bytes) is not int or max_bytes <= 0):
        raise ValueError('Directory capture byte budget must be positive')
    private_root = Path(workspace).resolve()
    for plan in checkpointed(plans):
        if plan.use.kind == 'directory' and plan.use.role != 'swmm:temporary_directory' and plan.use.access != 'write' and plan.original is not None:
            origin = plan.original.resolve()
            if private_root.is_relative_to(origin) or origin.is_relative_to(private_root):
                reject('run.directory_workspace_overlap', 'Private workspace overlaps a declared input directory',
                    subject=file_subject(plan.use), diagnostics=model._resolve_diagnostics)
    records, outputs, issues, copied = [], [], [], {}
    copied_trees = {}
    writable_sources = {p.original.resolve() for p in checkpointed(plans)
        if p.use.kind == 'directory' and p.use.active and p.use.access == 'read_write'
        and p.original is not None}
    from ._directory_outputs import layout as output_layout
    planned_outputs=output_layout(plans,asset_name)
    asset = workspace/asset_name
    for name in checkpointed(('inputs', 'outputs', 'inactive', 'tmp')):
        (asset/name).mkdir(parents=True, exist_ok=True)
    shared,graph_plans=_prepare_shared_graphs(model,plans,workspace,asset_name,checkpoint=checkpoint,max_bytes=max_bytes)
    indexed_records={}
    order=tuple((i,p) for i,p in enumerate(plans) if i not in planned_outputs.mutable)+tuple((i,p) for i,p in enumerate(plans) if i in planned_outputs.mutable)
    with model._store._permit_context_change('file_rebase'):
        for index, plan in checkpointed(order):
            checkpoint()
            use, original = plan.use, plan.original
            sha = size = tree = initial = group = None
            with resource_errors(model, use, 'run.resource_capture'):
                if use.kind == 'directory' and use.role == 'swmm:temporary_directory':
                    relative = Path(asset_name)/'tmp'
                elif not use.active:
                    relative = Path(asset_name)/'inactive'/(f'r{index}.dir' if use.kind == 'directory' else f'r{index}.dat')
                elif index in planned_outputs.mutable:
                    parent_index=planned_outputs.mutable[index];parent=indexed_records[parent_index]
                    suffix=original.resolve().relative_to(plans[parent_index].original.resolve())
                    relative=Path(parent.relative_path)/suffix
                    # A private mutable member is published only with its asset tree.
                    outputs.append((use,None,relative))
                elif (use.owner.canonical,use.path) in shared:
                    from ._directory_graph import consumer_key
                    group=shared[(use.owner.canonical,use.path)];view=group.state.layout.view(consumer_key(use.owner,use.path))
                    relative=Path(group.relative_path)/view.path;evidence=group.state.view(view.key)
                    if use.kind=='directory':tree=evidence
                    elif evidence is not None:sha,size=evidence.sha256,evidence.size
                    initial=Path(group.initial_relative_path)/view.path if group.initial_relative_path is not None else None
                    if evidence is None:
                        issues.extend(model._resolve_diagnostics(ValidationReport(diagnostics=(Diagnostic(
                            code='run.optional_resource_missing',severity=Severity.WARNING,subject=file_subject(use),
                            message=f'Optional {use.role} directory is not captured: {original or use.file.path}'),))).diagnostics)
                elif use.kind == 'directory':
                    adapter = plan.directory_adapter
                    if type(adapter) is not DirectoryAdapter:
                        reject('run.directory_adapter', 'Active directory capture requires its explicit runtime staging adapter',
                            subject=file_subject(use), diagnostics=model._resolve_diagnostics)
                    limits = adapter.limits if max_bytes is None else replace(adapter.limits,
                        total_bytes=min(adapter.limits.total_bytes, max_bytes))
                    if use.access == 'write':
                        relative = planned_outputs.relative.get(index,Path(asset_name)/'outputs'/f'r{index}.dir')
                        (workspace/relative).mkdir(parents=True,exist_ok=True)
                        outputs.append((use, original, relative))
                    elif original is None or not (original.exists() or original.is_symlink()):
                        if original is None and use.access == 'read_write':
                            reject('run.resource_path', 'Optional writable directory requires a resolvable source path; unavailable is not absent',
                                subject=file_subject(use), diagnostics=model._resolve_diagnostics)
                        if use.required:
                            reject('run.missing_resource', f'Required {use.role} directory is missing: {original or use.file.path}',
                                subject=file_subject(use), diagnostics=model._resolve_diagnostics)
                        source = original.resolve() if original is not None else None
                        if source in copied_trees:
                            relative, tree, initial = copied_trees[source]
                            if tree is not None:
                                reject('run.resource_changed', 'Shared directory disappeared during capture',
                                    subject=file_subject(use), diagnostics=model._resolve_diagnostics)
                        elif source in writable_sources:
                            relative = Path(asset_name)/'inputs'/f'r{index}.dir'
                            initial = Path(asset_name)/'initial'/f'r{index}.dir'
                            (workspace/initial).parent.mkdir(parents=True, exist_ok=True)
                            copied_trees[source] = (relative, None, initial)
                        else:
                            relative = Path(asset_name)/'inactive'/f'r{index}.dir'
                        from ._directory_state import verify_absent
                        if original is not None:verify_absent(original)
                        issues.extend(model._resolve_diagnostics(ValidationReport(diagnostics=(Diagnostic(
                            code='run.optional_resource_missing', severity=Severity.WARNING, subject=file_subject(use),
                            message=f'Optional {use.role} directory is not captured: {original or use.file.path}'),))).diagnostics)
                    else:
                        # Do not resolve a link before inspecting its declared root.
                        _node(original, 'directory')
                        source = original.resolve()
                        if source in copied_trees:
                            relative, tree, initial = copied_trees[source]
                            if tree is None:
                                reject('run.resource_changed', 'Shared directory appeared during capture',
                                    subject=file_subject(use), diagnostics=model._resolve_diagnostics)
                            # Each consumer's own limits still apply to shared trees.
                            verify_tree(original, tree, limits=limits, checkpoint=checkpoint)
                            verify_tree(workspace/relative, tree, limits=limits, checkpoint=checkpoint)
                        else:
                            relative = Path(asset_name)/'inputs'/f'r{index}.dir'
                            if source in writable_sources:
                                initial = Path(asset_name)/'initial'/f'r{index}.dir'
                                (workspace/initial).parent.mkdir(parents=True, exist_ok=True)
                                tree = capture_tree(original, workspace/initial, limits=limits, checkpoint=checkpoint)
                                capture_tree(workspace/initial, workspace/relative, limits=limits, checkpoint=checkpoint)
                            else:
                                tree = capture_tree(original, workspace/relative, limits=limits, checkpoint=checkpoint)
                            copied_trees[source] = (relative, tree, initial)
                elif use.access == 'write':
                    relative = planned_outputs.relative.get(index,Path(asset_name)/'outputs'/f'r{index}.dat')
                    (workspace/relative).parent.mkdir(parents=True,exist_ok=True)
                    outputs.append((use, original, relative))
                elif original is None or not original.is_file():
                    if use.required:
                        reject('run.missing_resource', f'Required {use.role} input is missing: {original or use.file.path}',
                            subject=file_subject(use), diagnostics=model._resolve_diagnostics)
                    relative = Path(asset_name)/'inactive'/f'r{index}.dat'
                    issues.extend(model._resolve_diagnostics(ValidationReport(diagnostics=(Diagnostic(
                        code='run.optional_resource_missing', severity=Severity.WARNING, subject=file_subject(use),
                        message=f'Optional {use.role} input is not captured: {original or use.file.path}'),))).diagnostics)
                else:
                    source = original.resolve()
                    if source in copied:
                        relative, sha, size = copied[source]
                    else:
                        relative = Path(asset_name)/'inputs'/f'r{index}.dat'
                        sha, size = copy_input(source, workspace/relative, checkpoint=checkpoint)
                        copied[source] = (relative, sha, size)
            reference = replace(use.file, path=relative.as_posix(), base_directory=None, flavor='native')
            rows = model.collection(use.owner.collection)
            rows.replace(use.owner.key, replace_path(rows[use.owner.key], use.path, reference))
            indexed_records[index]=ResourceSnapshot(owner=use.owner, field=use.path, role=use.role, format=use.format,
                kind=use.kind, access=use.access, active=use.active, required=use.required,
                original_path=str(original) if original else None, relative_path=relative.as_posix(), sha256=sha, size=size, tree=tree,
                initial_relative_path=initial.as_posix() if initial else None,directory_group=group)
    records=tuple(indexed_records[i] for i in range(len(plans)))
    from ._mutable_outputs import prepare_startup
    prepare_startup(records,workspace)
    from ._directory_graph import verify_plan
    for graph_plan in checkpointed(graph_plans):verify_plan(graph_plan,checkpoint=checkpoint)
    # All native scratch files use an owned subdirectory, even when the Model
    # did not specify TEMPDIR. Its chosen parent is honored by Runner.
    model.update_options(temp_directory=FileReference(path=(Path(asset_name)/'tmp').as_posix(), direction='output'))
    return tuple(records), tuple(outputs), tuple(issues)


def verify_resources(records, workspace, *, checkpoint, diagnostics=None, initial_execution=False):
    from ._directory_state import initial_path, mutable_groups
    mutable = mutable_groups(records)
    from ._mutable_outputs import startup_tree
    def verify_current(path,tree,limits):
        expected=startup_tree(records,path,tree)
        if expected is None:
            from ._directory_state import verify_absent
            verify_absent(workspace/path,checkpoint=checkpoint)
        else:
            limits=replace(limits,entries=max(limits.entries,len(expected.entries)),depth=max(limits.depth,max((len(e.path.split('/')) for e in expected.entries),default=1)))
            verify_tree(workspace/path,expected,limits=limits,checkpoint=checkpoint)
    from ._directory_graph import resource_groups
    for group in checkpointed(resource_groups(records).values()):
        verify_tree(workspace/(group.initial_relative_path or group.relative_path),group.tree,limits=group.state.layout.limits,checkpoint=checkpoint)
        if initial_execution and group.initial_relative_path is not None:
            verify_current(group.relative_path,group.tree,group.state.layout.limits)
    groups = {}
    for record in checkpointed(records):
        if record.directory_group is not None:continue
        if record.sha256 is not None or record.tree is not None or record.initial_relative_path is not None or (record.kind=='directory' and record.active and record.access!='write' and record.role!='swmm:temporary_directory'):
            groups.setdefault(initial_path(record), []).append(record)
    for group in checkpointed(groups.values()):
        record = group[0]
        subjects = tuple(dict.fromkeys(DiagnosticSubject(collection=item.owner.collection,
            key=item.owner.key, path=(*item.field, 'path')) for item in checkpointed(group)))
        expected = record.tree if record.kind == 'directory' else (record.sha256, record.size)
        if any((item.tree if item.kind == 'directory' else (item.sha256, item.size)) != expected for item in checkpointed(group)):
            reject('run.resource_conflict', 'Shared captured resource records disagree',
                subject=subjects[0], related=subjects[1:], diagnostics=diagnostics)
        try:
            if record.kind=='directory' and record.tree is None:
                from ._directory_state import verify_absent
                checkpoint(); actual = verify_absent(workspace/initial_path(record),checkpoint=checkpoint)
                if initial_execution:verify_current(record.relative_path,None,DirectoryLimits())
            elif record.tree is not None:
                manifest = record.tree
                limits = DirectoryLimits(total_bytes=max(1, manifest.total_bytes), entries=max(1, len(manifest.entries)),
                    depth=max((len(entry.path.split('/')) for entry in checkpointed(manifest.entries)), default=1))
                actual = inspect_tree(workspace/initial_path(record), limits=limits, checkpoint=checkpoint)
                if initial_execution and record.relative_path in mutable:
                    verify_current(record.relative_path,manifest,limits)
            else:
                actual = digest_file(workspace/record.relative_path, checkpoint=checkpoint)
        except (OSError, ValueError) as error:
            resource_failure(error, 'run.resource_unavailable', subject=subjects[0], related=subjects[1:], diagnostics=diagnostics)
            raise
        if actual != expected:
            reject('run.resource_changed', f'Captured {record.role} bytes changed after preflight',
                subject=subjects[0], related=subjects[1:], diagnostics=diagnostics)


def input_matches(path, expected, *, checkpoint=None):
    """Verify exact private input bytes in bounded reads, including EOF."""
    from ..validation._cooperative import checkpoint_scope, checkpoint as work_checkpoint
    with checkpoint_scope(checkpoint):
        offset = 0
        with path.open('rb') as stream:
            while True:
                work_checkpoint()
                chunk = stream.read(64*1024)
                if not chunk:
                    return offset == len(expected)
                if chunk != expected[offset:offset+len(chunk)]:
                    return False
                offset += len(chunk)



def _prepare_shared_graphs(model,plans,workspace,asset_name,*,checkpoint,max_bytes):
    from ._directory_graph import DirectoryRequest,DirectoryGroupSnapshot,consumer_key,plan_graphs,capture_graph,execution_graphs
    requests=[];members={}
    if not any(p.use.active and p.use.kind=='directory' and p.use.access in ('read','read_write') and p.use.role!='swmm:temporary_directory' and p.original is not None for p in plans):return {},()
    for plan in checkpointed(plans):
        use=plan.use
        if not(use.active and use.kind in ('directory','file') and use.access in ('read','read_write') and use.role!='swmm:temporary_directory') or plan.original is None:continue
        if use.kind=='directory' and type(plan.directory_adapter) is not DirectoryAdapter:
            reject('run.directory_adapter','Active directory capture requires its explicit runtime staging adapter',subject=file_subject(use),diagnostics=model._resolve_diagnostics)
        if use.required and not (plan.original.exists() or plan.original.is_symlink()):
            reject('run.missing_resource',f'Required {use.role} directory is missing: {plan.original}',subject=file_subject(use),diagnostics=model._resolve_diagnostics)
        limits=plan.directory_adapter.limits if use.kind=='directory' else DirectoryLimits()
        if max_bytes is not None:limits=replace(limits,total_bytes=min(limits.total_bytes,max_bytes))
        key=consumer_key(use.owner,use.path);requests.append(DirectoryRequest(key=key,source=plan.original,access=use.access,required=use.required,limits=limits,kind=use.kind));members[key]=(use.owner.canonical,use.path)
    if not requests:return {},()
    # Consumer budgets bound each observed tree; allow their union plus forest
    # root entries without imposing a smaller implicit per-group default.
    limits=DirectoryLimits(total_bytes=sum(r.limits.total_bytes for r in requests),entries=sum(r.limits.entries for r in requests)+len(requests),depth=max(r.limits.depth for r in requests)+1)
    planned=execution_graphs(plan_graphs(tuple(requests),limits=limits,checkpoint=checkpoint),checkpoint=checkpoint);shared={}
    for index,plan in checkpointed(enumerate(planned)):
        if len({o.path.resolve() for o in plan.observations})<2:continue
        key='g'+str(index);relative=Path(asset_name)/'inputs'/(key+'.dir');initial=Path(asset_name)/'initial'/(key+'.dir') if plan.state.layout.mutable else None
        if initial is not None:
            (workspace/initial).parent.mkdir(parents=True,exist_ok=True);capture_graph(plan,workspace/initial,checkpoint=checkpoint)
            capture_tree(workspace/initial,workspace/relative,limits=plan.state.layout.limits,checkpoint=checkpoint)
        else:capture_graph(plan,workspace/relative,checkpoint=checkpoint)
        group=DirectoryGroupSnapshot(key=key,relative_path=relative.as_posix(),initial_relative_path=initial.as_posix() if initial else None,state=plan.state)
        for view in plan.state.layout.views:shared[members[view.key]]=group
    return shared,planned
