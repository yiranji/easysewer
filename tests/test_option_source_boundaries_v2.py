"""Every OPTIONS keyword keeps invalid histories and exact diagnostic origins."""
from datetime import timedelta
import unittest
from easysewer.io.inp import InpDocument
from easysewer.model import Model
from easysewer.validation import ValidationError
from test_option_effects_v2 import OWNER
from test_option_lifecycle_gates_v2 import CASES

EVIDENCE=[]


class OptionSourceBoundaryTests(unittest.TestCase):
    def test_all_43_invalid_assignments_before_after_and_between_valid_sources(self):
        for keyword,field,token,_,_ in CASES:
            valid=keyword+' '+token
            bad=keyword+('' if keyword=='TEMPDIR' else ' FUTURE_INVALID_VALUE')
            for label,lines,bad_line in (('last',[valid,bad],4),('first',[bad,valid],2),
                                         ('middle',[valid,bad,valid],4)):
                with self.subTest(keyword=keyword,history=label):
                    text='[options]\r\n'+lines[0]+' ; first\r\n[OPTIONS]\r\n'+'\r\n'.join(lines[1:])+'\r\n'
                    # FileReference origins are absolute, even for a virtual input.
                    document=InpDocument.from_text(text,source='C:/fixtures/option-boundaries.inp')
                    with self.assertRaises(ValidationError):Model.from_document(document,strict=True)
                    model=Model.from_document(document,strict=False)
                    self.assertEqual(model.document.to_bytes(),text.encode())
                    issues=[d for d in model.validate().errors if d.code=='options.invalid_input']
                    self.assertEqual(len(issues),1)
                    issue=issues[0]
                    self.assertEqual((issue.field,issue.span.source,issue.span.line),(field,'C:/fixtures/option-boundaries.inp',bad_line))
                    self.assertIn(keyword,text.splitlines()[bad_line-1])
                    provenance=model.field_provenance(OWNER,field)
                    self.assertEqual(provenance.status,'unknown')
                    self.assertEqual(len(provenance.declarations),2 if label=='middle' else 1)
                    self.assertTrue(provenance.declarations[-1].contributes)
                    for declaration in provenance.declarations:
                        for item in declaration.tokens:
                            line=text.splitlines()[item.span.line-1]
                            self.assertEqual(line[item.span.column-1:item.span.end_column-1],item.raw)
                    restored=Model.from_json_document(model.to_json_document(),strict=False)
                    self.assertEqual(restored.document.to_bytes(),document.to_bytes())
                    self.assertEqual(restored.field_provenance(OWNER,field),provenance)
                    # Editing the semantic value cannot silently remove an invalid source row.
                    with self.assertRaises(ValidationError):restored.to_document(normalize=True)
                    EVIDENCE.append(dict(kind='invalid-option-history',keyword=keyword,history=label,
                                         bad_line=bad_line,valid_declarations=len(provenance.declarations)))

    def test_unknown_keywords_survive_edits_clear_json_and_block_conversion(self):
        text='[OPTIONS]\nFUTURE_OPTION "opaque data" ; keep\nWET_STEP 00:01\n'
        model=Model.from_document(InpDocument.from_text(text,source='future.inp'),strict=True)
        self.assertEqual(model.to_document().text,text)
        self.assertEqual(model.field_provenance(OWNER,'wet_step').status,'explicit')
        warnings=[d for d in model.validate().diagnostics if d.code=='options.unknown_option']
        self.assertEqual([(d.span.source,d.span.line) for d in warnings],[('future.inp',2)])
        model.update_options(wet_step=timedelta(seconds=30))
        model.update_options(wet_step=None)
        restored=Model.from_json_document(model.to_json_document(),strict=True)
        output=restored.to_document().text
        self.assertIn('FUTURE_OPTION "opaque data" ; keep',output)
        self.assertNotIn('WET_STEP',output)
        before=restored.to_json_document().to_bytes()
        with self.assertRaises(ValidationError):restored.convert_units('CMS')
        self.assertEqual(restored.to_json_document().to_bytes(),before)
        EVIDENCE.append(dict(kind='unknown-option-protection',source_line=2))


if __name__=='__main__':unittest.main()
