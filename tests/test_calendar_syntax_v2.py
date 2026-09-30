"""Calendar tokens must not acquire Python-only numeric spellings."""
from datetime import date
import unittest
from easysewer.io.inp import InpDocument
from easysewer.model import Model
from easysewer.validation import ValidationError
from test_option_effects_v2 import OWNER
from test_option_calendar_gates_v2 import BASE

BAD_DATES=('1/1/2_020','1/1/２０２０','1/1/٢٠٢٠','1/1_0/2020','١/1/2020','0_1/1/2020','1/ 1/2020')
GOOD_DATES=('1/1/2020','01/01/2020','Jan-01-2020','jAn/1/2020','+1/+1/+2020','1-01/2020')
CONSUMERS=(
    ('TIMESERIES','[TIMESERIES]\nDates {date} 00:00 1\n'),
    ('CONTROLS','[CONTROLS]\nRULE DateRule\nIF SIMULATION DATE > {date}\nTHEN CONDUIT P STATUS = CLOSED\n'),
    ('EVENTS','[EVENTS]\n{date} 00:00 1/2/2020 00:00\n'),
    ('TEMPERATURE','[TEMPERATURE]\nFILE "weather.dat" {date} F\n'),
    ('RAINGAGES','[RAINGAGES]\nR INTENSITY 1 1 FILE "weather.dat" Station IN {date}\n'),
)
EVIDENCE=[]


def document(text):
    return InpDocument.from_text(text,source='C:/fixtures/calendar-syntax.inp')


class CalendarSyntaxTests(unittest.TestCase):
    def invalid_source(self,text,section):
        with self.assertRaises(ValidationError):Model.from_document(document(text),strict=True)
        model=Model.from_document(document(text),strict=False)
        self.assertEqual(model.document.to_bytes(),text.encode('utf-8'))
        issues=[d for d in model.validate().errors if d.section==section]
        self.assertTrue(issues,model.validate())
        for issue in issues:
            self.assertIsNotNone(issue.span)
            self.assertEqual(issue.span.source,'C:/fixtures/calendar-syntax.inp')
            self.assertGreater(issue.span.line,0)
        restored=Model.from_json_document(model.to_json_document(),strict=False)
        self.assertEqual(restored.document.to_bytes(),model.document.to_bytes())
        with self.assertRaises(ValidationError):restored.to_document(normalize=True)
        return model

    def test_options_dates_reject_python_numeric_extensions_and_keep_sources(self):
        for field in ('start_date','end_date','report_start_date'):
            for token in BAD_DATES:
                with self.subTest(field=field,token=token):
                    text='[OPTIONS]\n'+field.upper()+' '+token+' ; retained\n'
                    model=self.invalid_source(text,'OPTIONS')
                    self.assertIsNone(getattr(model.options,field))
                    self.assertEqual(model.field_provenance(OWNER,field).status,'unknown')
                    EVIDENCE.append(dict(kind='invalid-date-option',field=field,token=token))

    def test_sweep_month_day_rejects_non_ascii_and_underscores(self):
        for field in ('sweep_start','sweep_end'):
            for token in ('1/1_0','١/1','1/１','1_0/1'):
                with self.subTest(field=field,token=token):
                    self.invalid_source('[OPTIONS]\n'+field.upper()+' '+token+'\n','OPTIONS')
                    EVIDENCE.append(dict(kind='invalid-sweep-date',field=field,token=token))

    def test_shared_date_consumers_preserve_invalid_rows_and_json(self):
        for section,template in CONSUMERS:
            for token in BAD_DATES[:6]:
                with self.subTest(section=section,token=token):
                    self.invalid_source(BASE+template.format(date=token),section)
                    EVIDENCE.append(dict(kind='invalid-shared-date',section=section,token=token))

    def test_supported_ascii_month_names_signs_separators_and_leap_days(self):
        for token in GOOD_DATES:
            with self.subTest(token=token):
                source=BASE+'[OPTIONS]\nSTART_DATE '+token+'\n'
                model=Model.from_document(document(source),strict=True)
                self.assertEqual(model.options.start_date,date(2020,1,1))
                self.assertEqual(model.to_document().text,source)
                restored=Model.from_json_document(model.to_json_document(),strict=True)
                self.assertEqual(Model.from_document(restored.to_document(normalize=True),strict=True).options,model.options)
                for section,template in CONSUMERS:
                    shared=Model.from_document(document(BASE+template.format(date=token)),strict=True)
                    self.assertEqual(shared.to_document().text,BASE+template.format(date=token))
                EVIDENCE.append(dict(kind='valid-ascii-date',token=token))
        for token,valid in (('2/29/2020',True),('2/29/2019',False)):
            source='[OPTIONS]\nSTART_DATE '+token+'\n'
            if valid:self.assertEqual(Model.from_document(document(source),strict=True).options.start_date,date(2020,2,29))
            else:self.invalid_source(source,'OPTIONS')


if __name__=='__main__':unittest.main()
