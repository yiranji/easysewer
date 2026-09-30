"""Native inactive contexts and explicitly rejected permissive spellings."""
import hashlib,tempfile,unittest
from pathlib import Path

from easysewer import get_native_capabilities
from easysewer.io.inp import InpDocument
from easysewer.model import Model
from easysewer.validation import ValidationError
from test_options_v2 import network
from test_option_variants_v2 import CHOICES,BOOLEANS
from test_native_v2_regulator_fields import FAMILIES,library
from test_native_v2_title_report_gates import solve

EVIDENCE=[]
UNITS=('CFS','GPM','MGD','CMS','LPS','MLD')


def digest(result):
    return dict(out_sha256=hashlib.sha256(result['out']).hexdigest(),
                report_sha256=hashlib.sha256(result['report']).hexdigest())


@unittest.skipUnless(get_native_capabilities()['swmm_solver'] and get_native_capabilities()['flexible_ponding'],
                     'Both packaged engines required')
class NativeOptionGateBoundaryTests(unittest.TestCase):
    def test_process_switches_and_infiltration_are_inert_without_consumers(self):
        # This drains below crown with no subcatchments, snow, groundwater,
        # RDII or pollutants and no flooding: the listed consumers are absent.
        inactive=('IGNORE_RAINFALL','IGNORE_SNOWMELT','IGNORE_GROUNDWATER',
                  'IGNORE_RDII','IGNORE_QUALITY','ALLOW_PONDING','SLOPE_WEIGHTING')
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for family,name,symbol in FAMILIES:
                lib,_=library(name,symbol)
                for unit in UNITS:
                    m=network();m.convert_units(unit);source=m.to_document().text
                    baseline=solve(self,lib,root,source)
                    for keyword,tokens in [(k,('NO','YES')) for k in inactive]+[('INFILTRATION',CHOICES['INFILTRATION'])]:
                        for token in tokens:
                            with self.subTest(family=family,unit=unit,keyword=keyword,token=token):
                                literal=source+'[OPTIONS]\n'+keyword+' '+token+'\n'
                                expected=dict(baseline)
                                if keyword=='ALLOW_PONDING' and token=='YES':
                                    # The report declares the configured switch
                                    # even when no node floods. Only that exact
                                    # summary value changes; OUT and all other
                                    # report bytes must remain equal.
                                    summary=b'    Ponding Allowed ........ NO\n'
                                    self.assertEqual(baseline['report'].count(summary),1)
                                    expected['report']=baseline['report'].replace(summary,b'    Ponding Allowed ........ YES\n')
                                self.assertEqual(solve(self,lib,root,literal),expected)
                                typed=Model.from_document(InpDocument.from_text(literal),strict=True)
                                restored=Model.from_json_document(typed.to_json_document(),strict=True)
                                self.assertEqual(solve(self,lib,root,restored.to_document(normalize=True).text),expected)
                                EVIDENCE.append(dict(kind='inactive-option-context',family=family,unit=unit,
                                    keyword=keyword,token=token,ponding_summary_changed=keyword=='ALLOW_PONDING' and token=='YES',**digest(expected)))

    def test_native_suffix_acceptance_does_not_claim_unsupported_values(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=network().to_document().text
            for family,name,symbol in FAMILIES:
                lib,_=library(name,symbol)
                for keyword,tokens in {**CHOICES,**{k:('YES','NO') for k in BOOLEANS}}.items():
                    token=tokens[0]
                    with self.subTest(family=family,keyword=keyword):
                        baseline=solve(self,lib,root,source+'[OPTIONS]\n'+keyword+' '+token+'\n')
                        text=source+'[OPTIONS]\n'+keyword+' '+token+'_FUTURE\n'
                        self.assertEqual(solve(self,lib,root,text),baseline)
                        document=InpDocument.from_text(text,source='suffix.inp')
                        with self.assertRaises(ValidationError):Model.from_document(document,strict=True)
                        typed=Model.from_document(document,strict=False)
                        self.assertEqual(typed.document.text,text)
                        with self.assertRaises(ValidationError):typed.to_document(normalize=True)
                        EVIDENCE.append(dict(kind='native-permissive-suffix',family=family,keyword=keyword,
                            token=token+'_FUTURE',typed_rejected=True,**digest(baseline)))

    def test_integer_numeric_coercions_match_complete_native_results(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=network().to_document().text
            for family,name,symbol in FAMILIES:
                lib,_=library(name,symbol)
                for keyword in ('THREADS','MAX_TRIALS'):
                    for token,expected in (('2.9',2),('2e3',2),('+.5',0),('-0.9',0),('+02',2)):
                        with self.subTest(family=family,keyword=keyword,token=token):
                            raw=source+'[OPTIONS]\n'+keyword+' '+token+'\n'
                            canonical=source+'[OPTIONS]\n'+keyword+' '+str(expected)+'\n'
                            actual=solve(self,lib,root,raw)
                            self.assertEqual(actual,solve(self,lib,root,canonical))
                            typed=Model.from_document(InpDocument.from_text(raw),strict=True)
                            self.assertEqual(solve(self,lib,root,typed.to_document(normalize=True).text),actual)
                            EVIDENCE.append(dict(kind='native-integer-coercion',family=family,keyword=keyword,
                                token=token,integer=expected,**digest(actual)))


if __name__=='__main__':unittest.main()
