"""Missing tuple references retain sources without inventing item identity."""
from dataclasses import dataclass
from pathlib import Path
import tempfile,unittest
from easysewer.model import Model,Ref
from easysewer.model.store import CollectionSpec,RecordStore
from easysewer.validation import DiagnosticSubject
from test_title_report_gates_v2 import fixture,load,KEYWORDS,selection

def missing(m):return tuple(d for d in m.validate().errors if d.code=='model.unresolved_reference')
def aggregate(issue,name):
    return next(v for v in issue.locations if v.subject==DiagnosticSubject(collection='swmm:report',key='settings',path=(name,'members')))

class ReferenceContainerDiagnosticTests(unittest.TestCase):
    def test_runner_records_returned_or_raised_reports_once_without_merging_distinct_entries(self):
        from dataclasses import replace
        from unittest.mock import patch
        from easysewer.runtime import Runner
        from easysewer.validation import Diagnostic,ValidationError,ValidationReport
        from test_runner_v2 import config
        from test_checkpoint_container_v2 import snapshot
        from unittest.mock import Mock
        first=Diagnostic(code='test:validation',message='Two distinct declarations may have equal diagnostics')
        second=replace(first);self.assertIsNot(first,second)
        report=ValidationReport(diagnostics=(first,second));original=Model.validate
        for raised in (False,True):
            def validate(model,*args,**kwargs):
                if kwargs.get('for_run'):
                    if raised:raise ValidationError(report)
                    return report
                return original(model,*args,**kwargs)
            with self.subTest(raised=raised),tempfile.TemporaryDirectory() as directory:
                model=fixture()
                backend=Mock(spec=('probe','session'))
                backend.probe.return_value=snapshot(Path(directory),model=model).backend
                with patch.object(Model,'validate',validate):
                    result=Runner(backends={'swmm:standard':backend}).run(model,config(Path(directory)/'run'))
                backend.probe.assert_called_once()
                backend.session.assert_not_called()
                self.assertEqual(result.status,'rejected')
                self.assertEqual(tuple(d for d in result.diagnostics.errors if d.code=='test:validation'),report.diagnostics)

    def test_repeated_report_members_locate_contributing_container_tokens(self):
        for name,keyword in KEYWORDS.items():
            with self.subTest(name=name):
                base=fixture();first=next(iter(base.collection('swmm:'+name)))
                text=base.to_document().text+'[REPORT]\n'+keyword+' '+first+' Missing\n[REPORT]\n'+keyword+' Missing Other\n'
                m=load(text,False);issues=missing(m);self.assertEqual(len(issues),2)
                for issue in issues:
                    self.assertEqual(issue.locations[0].status,'untracked');self.assertFalse(issue.locations[0].spans)
                    self.assertIsNone(issue.span);self.assertEqual(issue.locations[1].status,'absent')
                    context=aggregate(issue,name);self.assertEqual(context.status,'current')
                    self.assertEqual({s.line for s in context.spans},{len(text.splitlines())-2,len(text.splitlines())})
                    tokens=[text.splitlines()[s.line-1][s.column-1:s.end_column-1] for s in context.spans]
                    self.assertEqual(tokens,[first,'Missing','Missing','Other'])
                    self.assertTrue(context.source_sha256)
                self.assertEqual(missing(m.copy()),issues)
                self.assertEqual(missing(Model.from_json_document(m.to_json_document(),strict=False)),issues)

    def test_replaced_reordered_and_case_changed_members_retain_historical_context(self):
        for name,keyword in KEYWORDS.items():
            m=load(fixture().to_document().text+'[REPORT]\n'+keyword+' Missing Other\n',False)
            original=aggregate(missing(m)[0],name)
            for names in (('Other','Missing'),('New','Other'),('missing','Other')):
                with self.subTest(name=name,names=names):
                    m.update_report(**{name:selection(name,*names)})
                    for issue in missing(m):
                        self.assertIsNone(issue.span);self.assertEqual(issue.locations[0].status,'untracked')
                        context=aggregate(issue,name);self.assertEqual(context.status,'changed')
                        self.assertEqual(context.spans,original.spans)
                    self.assertEqual(missing(Model.from_json_document(m.to_json_document(),strict=False)),missing(m))

    def test_programmatic_sequences_have_no_fabricated_source(self):
        for name in KEYWORDS:
            m=fixture();m.update_report(**{name:selection(name,'Missing')})
            issue,=missing(m)
            self.assertEqual([v.status for v in issue.locations],['programmatic','absent','programmatic'])
            self.assertTrue(all(not v.spans for v in issue.locations));self.assertIsNone(issue.span)

    def test_nested_sequence_uses_first_nonindexed_container_and_scalar_is_unchanged(self):
        @dataclass(frozen=True)
        class Row:
            id:str
            groups:tuple
            scalar:Ref
        store=RecordStore((CollectionSpec(key='test:rows',record_type=Row,key_of=lambda v:v.id),))
        store.collection('test:rows').add(Row('row',((Ref(collection='test:rows',key='nested'),),),Ref(collection='test:rows',key='scalar')))
        issues=store.validate().errors;self.assertEqual(len(issues),2)
        nested=next(d for d in issues if len(d.subject.path)>1)
        self.assertEqual(nested.subject.path,('groups',0,0))
        self.assertEqual(nested.related[-1],DiagnosticSubject(collection='test:rows',key='row',path=('groups',)))
        scalar=next(d for d in issues if d.subject.path==('scalar',))
        self.assertEqual(scalar.related,(DiagnosticSubject(collection='test:rows',key='scalar'),))

    def test_runner_rejection_json_and_result_archive_preserve_container_locations(self):
        from easysewer.runtime import Runner,RunResult
        from test_runner_v2 import config
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for name,keyword in KEYWORDS.items():
                m=load(fixture().to_document().text+'[REPORT]\n'+keyword+' Missing\n',False)
                expected=tuple(d for d in m.validate(for_run=True).errors if d.code=='model.unresolved_reference')
                with patch('ctypes.CDLL',side_effect=AssertionError('Validation must reject before loading native library')):
                    result=Runner().run(m,config(root/name,keep_failed_artifacts=False))
                self.assertEqual(result.status,'rejected')
                self.assertEqual(tuple(d for d in result.diagnostics.errors if d.code=='model.unresolved_reference'),expected)
                result.save(root/(name+'-saved'));actual=RunResult.load(root/(name+'-saved'))
                self.assertEqual(actual.diagnostics,result.diagnostics)

if __name__=='__main__':unittest.main()
