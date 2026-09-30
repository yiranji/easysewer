"""Native admission exposes why Python-only calendar numbers are rejected."""
import ctypes,hashlib,os,tempfile,unittest
from pathlib import Path
from easysewer import get_native_capabilities
from easysewer.model import Model
from easysewer.validation import ValidationError
from test_calendar_syntax_v2 import BAD_DATES,GOOD_DATES,BASE,document
from test_native_v2_regulator_fields import FAMILIES,library
from test_native_v2_title_report_gates import solve

EVIDENCE=[]


@unittest.skipUnless(get_native_capabilities()['swmm_solver'] and get_native_capabilities()['flexible_ponding'],
                     'Both packaged engines required')
class NativeCalendarSyntaxTests(unittest.TestCase):
    def test_python_only_date_numbers_do_not_silently_change_native_meaning(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for family,name,symbol in FAMILIES:
                lib,_=library(name,symbol)
                lib.swmm_getValue.argtypes=[ctypes.c_int,ctypes.c_int];lib.swmm_getValue.restype=ctypes.c_double
                for token in BAD_DATES[:6]:
                    with self.subTest(family=family,token=token):
                        source=BASE+'[OPTIONS]\nSTART_DATE '+token+'\n'
                        inp=root/'model.inp';inp.write_text(source,encoding='utf-8')
                        try:
                            code=lib.swmm_open(os.fsencode(inp),os.fsencode(root/'model.rpt'),os.fsencode(root/'model.out'))
                            if token=='1/1/2_020':
                                self.assertEqual(code,0)
                                self.assertEqual(lib.swmm_getValue(0,0),-693228.0)  # 0002-01-01
                            else:self.assertNotEqual(code,0)
                        finally:self.assertEqual(lib.swmm_close(),0)
                        with self.assertRaises(ValidationError):Model.from_document(document(source),strict=True)
                        model=Model.from_document(document(source),strict=False)
                        self.assertEqual(model.document.text,source)
                        with self.assertRaises(ValidationError):model.to_document(normalize=True)
                        EVIDENCE.append(dict(kind='native-calendar-boundary',family=family,token=token,
                            native_code=code,native_start=-693228.0 if code==0 else None,python_rejected=True))

    def test_supported_ascii_spellings_keep_full_native_results(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for family,name,symbol in FAMILIES:
                lib,_=library(name,symbol)
                baseline=solve(self,lib,root,BASE)
                for token in GOOD_DATES:
                    with self.subTest(family=family,token=token):
                        source=BASE+'[OPTIONS]\nSTART_DATE '+token+'\n'
                        actual=solve(self,lib,root,source)
                        self.assertEqual(actual,baseline)
                        model=Model.from_document(document(source),strict=True)
                        restored=Model.from_json_document(model.to_json_document(),strict=True)
                        self.assertEqual(solve(self,lib,root,restored.to_document(normalize=True).text),actual)
                        EVIDENCE.append(dict(kind='native-calendar-spelling',family=family,token=token,
                            out_sha256=hashlib.sha256(actual['out']).hexdigest(),report_sha256=hashlib.sha256(actual['report']).hexdigest()))


if __name__=='__main__':unittest.main()
