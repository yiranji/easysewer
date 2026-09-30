from dataclasses import dataclass, replace
import inspect
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from easysewer import get_native_capabilities
from easysewer.model import Model,Ref,FileReference
from easysewer.runtime import Runner,RunConfig
from easysewer.model.node_rules import validate_nodes
from easysewer.validation._cooperative import checkpoint_scope
from easysewer.schema import network_fields
from test_network_fields_v2 import load,source,NODE
def config(path, **options):
    return RunConfig(output_directory=FileReference(path=str(path),direction="output"),**options)


def depth_issue(model):
    return next(d for d in model.validate(for_run=True).errors if d.code=='node.initial_depth')


class CandidateTests(unittest.TestCase):
    def test_source_rename_json_and_rollback(self):
        model=load(source(tail=' 1 2 0'))
        issue=depth_issue(model)
        self.assertEqual(issue.subject.path,('initial_depth',))
        self.assertEqual(issue.locations[0].status,'current')
        self.assertTrue(issue.locations[0].spans)
        self.assertEqual(depth_issue(Model.from_json_document(model.to_json_document())),issue)
        model.nodes.rename('J','Renamed')
        renamed=depth_issue(model)
        self.assertEqual(renamed.subject.key,'Renamed')
        self.assertEqual(renamed.locations[0].original.key,'J')
        before=model.to_json_document()
        with self.assertRaisesRegex(RuntimeError,'abort'):
            with model.transaction():
                model.nodes.update('Renamed',initial_depth=.5)
                self.assertTrue(model.validate(for_run=True).is_valid)
                raise RuntimeError('abort')
        self.assertEqual(model.to_json_document(),before)
        self.assertEqual(depth_issue(model),renamed)
        model.nodes.update('Renamed',initial_depth=.5)
        self.assertTrue(model.validate(for_run=True).is_valid)

    @unittest.skipUnless(get_native_capabilities()['swmm_solver'], 'Real native backend required')
    def test_runner_stops_before_project_open_and_preserves_previous_outputs(self):
        from easysewer.runtime._process_session import ProcessSession
        model=load(source(tail=' 1 2 0'));before=model.to_json_document()
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            previous={root/('model'+suffix):('previous'+suffix).encode() for suffix in ('.inp','.rpt','.out')}
            for path,data in previous.items():path.write_bytes(data)
            with patch.object(ProcessSession,'open',side_effect=AssertionError('Native project must not open')) as opened:
                result=Runner().run(model,config(root,overwrite=True))
            self.assertFalse(opened.called)
            self.assertEqual(result.status,'rejected')
            self.assertFalse(result.native_completed)
            self.assertIn('node.initial_depth',{d.code for d in result.diagnostics.errors})
            for path,data in previous.items():self.assertEqual(path.read_bytes(),data)
            self.assertEqual(model.to_json_document(),before)

    def test_sparse_graph_adjacent_visits_are_linear(self):
        model=load(source(tail=' 0 .5 0'))
        node=model.nodes['J'];link=model.links['P']
        for index in range(1,1024):
            name='J'+str(index)
            model.nodes.add(replace(node,id=name))
            model.links.add(replace(link,id='P'+str(index),inlet=Ref(collection='swmm:nodes',key=name)))
        visits=[];original=network_fields.node_depth
        def measured(context,mode,**kwargs):
            self.assertIsNotNone(kwargs.get('links'))
            visits.append(len(kwargs['links']))
            return original(context,mode,**kwargs)
        with patch.object(network_fields,'node_depth',measured):
            issues=tuple(validate_nodes(model._store,model.profile,for_run=True))
        self.assertEqual(len(visits),1024)
        self.assertEqual(sum(visits),1024)
        self.assertFalse(issues)

    def test_adjacency_build_can_cancel_without_mutating_model(self):
        model=load(source(tail=' 0 .5 0'));link=model.links['P']
        for index in range(1024):model.links.add(replace(link,id='P'+str(index)))
        before=model.to_json_document();interrupted=OSError('cancel adjacency')
        def cancel():
            frame=inspect.currentframe()
            while frame:
                if frame.f_code.co_name=='validate_nodes' and len(frame.f_locals.get('adjacent',{})):
                    raise interrupted
                frame=frame.f_back
        with self.assertRaises(OSError) as caught:
            with checkpoint_scope(cancel):tuple(validate_nodes(model._store,model.profile,for_run=True))
        self.assertIs(caught.exception,interrupted)
        self.assertEqual(model.to_json_document(),before)
        self.assertFalse(tuple(validate_nodes(model._store,model.profile,for_run=True)))


    def test_unknown_extension_does_not_become_a_known_zero_crown(self):
        from easysewer.model import geometry as g
        @dataclass(frozen=True,kw_only=True)
        class Extension(g.Geometry):
            kind = 'EXTRA'
            depth: float
        model=load(source(tail=' 0 3 0'))
        model.links.update('P',section=g.CrossSection(geometry=Extension(depth=4)))
        self.assertEqual(model.inspect_field(NODE,'initial_depth').semantics.effective.status,'unknown')
        issues=tuple(validate_nodes(model._store,model.profile,for_run=True))
        self.assertFalse([d for d in issues if d.code=='node.initial_depth'])

    def test_invalid_neighbor_does_not_become_an_initial_depth_diagnostic(self):
        model=load(source(tail=' 0 3 0'))
        model.links.update('P',section=None)
        self.assertEqual(model.inspect_field(NODE,'initial_depth').semantics.effective.status,'invalid')
        issues=tuple(validate_nodes(model._store,model.profile,for_run=True))
        self.assertFalse([d for d in issues if d.code=='node.initial_depth'])
        self.assertFalse(model.validate(for_run=True).is_valid)

    def test_zero_initial_depth_does_not_compute_crowns(self):
        model=load();node=model.nodes['J'];link=model.links['P']
        for index in range(1,1024):
            name='J'+str(index)
            model.nodes.add(replace(node,id=name,initial_depth=0.))
            model.links.add(replace(link,id='P'+str(index),inlet=Ref(collection='swmm:nodes',key=name)))
        with patch.object(network_fields,'node_depth',side_effect=AssertionError('Zero depth requires no crown calculation')):
            self.assertFalse(tuple(validate_nodes(model._store,model.profile,for_run=True)))
