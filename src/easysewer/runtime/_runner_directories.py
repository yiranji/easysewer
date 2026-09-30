"""Runner collection and publication of declared regular-directory resources."""
from dataclasses import replace
from pathlib import Path
from .results import DirectoryArtifact, DirectoryGroupArtifact
from ._directory_tree import DirectoryLimits, inspect_tree
from ._directory_graph import resource_groups, view_manifest
from ..validation._cooperative import checkpoint_scope, checkpointed


def enabled(resources):
    return any(r.active and r.kind=='directory' and r.role!='swmm:temporary_directory' for r in resources)


def _limits(record,adapters,limit):
    declared=adapters[record.format].limits if record.format in adapters else limit
    return DirectoryLimits(total_bytes=min(declared.total_bytes,limit.total_bytes),
        entries=min(declared.entries,limit.entries),depth=min(declared.depth,limit.depth))


def collect(resources,workspace,*,adapters,limits,complete,checkpoint=None,on_error=None,omit_outputs=False):
    """Capture each independently verifiable artifact; retain primary failures."""
    with checkpoint_scope(checkpoint):
        workspace=Path(workspace);directories=[];groups=[]
        def capture(label,operation,target):
            try:target.append(operation())
            except Exception as error:
                if on_error is None:raise
                on_error(label,error)
        for group in checkpointed(resource_groups(resources).values()):
            for current in ((False,True) if group.initial_relative_path is not None else (False,)):
                path=group.relative_path if current else group.initial_relative_path or group.relative_path
                capture(path,lambda group=group,path=path,current=current:DirectoryGroupArtifact.from_path(
                    group,workspace/path,current=current,complete=complete),groups)
        for record in checkpointed(resources):
            if not record.active or record.kind!='directory' or record.role=='swmm:temporary_directory' or record.directory_group is not None:continue
            if record.access=='write' and omit_outputs:continue
            roles=[(record.role if record.access=='write' else 'run:resource',record.initial_relative_path or record.relative_path,False)]
            if record.initial_relative_path is not None:roles.append(('run:resource_state',record.relative_path,True))
            for role,path,current in roles:
                def operation(record=record,role=role,path=path,current=current):
                    artifact=DirectoryArtifact.from_path(workspace/path,role=role,owner=record.owner,field=record.field,
                        complete=complete,declared_path=record.original_path,limits=_limits(record,adapters,limits),allow_absent=True)
                    if not current and record.access!='write' and artifact.manifest!=record.tree:
                        raise ValueError('Initial directory artifact differs from its resource snapshot')
                    return artifact
                capture(path,operation,directories)
        return tuple(directories),tuple(groups)


def rebase(directories,groups,workspace,published):
    """Bind already captured evidence to its exact post-transaction locations."""
    workspace,published=Path(workspace).absolute(),Path(published).absolute()
    def artifact(value):
        source=Path(value.path).absolute();relative=source.relative_to(workspace)
        if relative==Path('.'):raise ValueError('Resource artifact cannot own the workspace root')
        target=published/relative
        return replace(value,path=str(target),files=tuple((name,replace(member,path=str(target/name))) for name,member in value.files))
    return tuple(artifact(v) for v in directories),tuple(replace(v,artifact=artifact(v.artifact)) for v in groups)


def _initial_isolation(tree,resources,asset):
    roots={g.initial_relative_path for g in resource_groups(resources).values() if g.initial_relative_path is not None}
    roots.update(r.initial_relative_path for r in resources if r.directory_group is None and r.initial_relative_path is not None)
    roots=tuple(Path(p).relative_to(asset).as_posix() for p in roots)
    def owner(path):
        return next((p for p in roots if path==p or path.startswith(p+'/')),None)
    for entry in checkpointed(tree.entries):
        if entry.hardlink_to is not None:
            a,b=owner(entry.path),owner(entry.hardlink_to)
            if a!=b and (a is not None or b is not None):raise ValueError('Initial resource evidence aliases another execution tree')


def admission(resources,asset,*,limits,checkpoint=None):
    if not enabled(resources):return None
    with checkpoint_scope(checkpoint):
        asset=Path(asset);tree=inspect_tree(asset,limits=limits)
        _initial_isolation(tree,resources,Path(asset.name))
        return tree


def publication_trees(sources,transaction,directories,groups,resources,asset,*,limits,checkpoint=None):
    with checkpoint_scope(checkpoint):
        asset=Path(asset).absolute();tree=admission(resources,asset,limits=limits)
        # Compare against the earlier artifact observations before a commit.
        for artifact in checkpointed((*directories,*(v.artifact for v in groups))):
            path=Path(artifact.path).absolute().relative_to(asset).as_posix()
            if view_manifest(tree,path)!=artifact.manifest:raise ValueError('Directory artifact changed before publication')
        result={}
        for target in checkpointed(transaction.targets):
            if target.directory and target.path in sources:
                source=Path(sources[target.path]).absolute()
                result[source]=tree if source==asset else inspect_tree(source,limits=limits)
        return result
