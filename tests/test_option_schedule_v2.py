"""Runoff scheduling fixtures and antecedent dry-period option lifecycles."""
from dataclasses import replace
from datetime import date,time,timedelta
import unittest
from easysewer.model import Model,Ref
from easysewer.model.resources import SeriesPoint
from easysewer.model.report import ReportSelection
from easysewer.model.quality import PowerBuildup,InitialLoading,NoWashoff
from test_hydrology_v2 import hydrology_model
from test_quality_v2 import quality_model
from test_option_effects_v2 import OWNER,UNITS,load


def schedule_fixture(units='CFS',wet=60,dry=300,*,raining=True):
    model=hydrology_model()
    model.update_options(end_date=date(2020,1,30),end_time=time(0,15),
        routing_step=timedelta(seconds=30),report_step=timedelta(seconds=30),
        wet_step=timedelta(seconds=wet),dry_step=timedelta(seconds=dry),variable_step=0)
    model.raingages.update('R',interval=timedelta(seconds=60))
    model.timeseries.update('Rain',points=tuple(SeriesPoint(time=timedelta(seconds=s),
        value=1.2 if raining and 180<=s<420 else 0) for s in range(0,901,60)))
    model.update_report(subcatchments=ReportSelection(mode='ALL'),nodes=ReportSelection(mode='ALL'),links=ReportSelection(mode='ALL'))
    model.convert_units(units)
    return model


def buildup_fixture(units='CFS',*,override=False,raining=False):
    model=quality_model()
    model.update_options(end_date=date(2020,1,30),end_time=time(0,10),
        routing_step=timedelta(seconds=30),report_step=timedelta(seconds=30),
        wet_step=timedelta(seconds=60),dry_step=timedelta(seconds=120),dry_days=None)
    model.raingages.update('R',interval=timedelta(seconds=60))
    model.timeseries.update('Rain',points=tuple(SeriesPoint(time=timedelta(seconds=s),value=1.2 if raining else 0)
                                              for s in range(0,601,60)))
    model.landuses.update('Land',sweep_interval=0)
    model.update_report(subcatchments=ReportSelection(mode='ALL'),nodes=ReportSelection(mode='ALL'),links=ReportSelection(mode='ALL'))
    model.convert_units(units)
    # Independent unit-local oracle: 2 acres or 2 hectares and 2 mass/area/day.
    model.subcatchments.update('S',area=2)
    for key in model.buildup:
        model.buildup.update(key,function=PowerBuildup(maximum=10,coefficient=2,exponent=1))
    if override:
        for pollutant in ('Q0','Q1'):
            model.loadings.add(InitialLoading(subcatchment=Ref(collection='swmm:subcatchments',key='S'),
                pollutant=Ref(collection='swmm:pollutants',key=pollutant),mass_per_area=4))
    return model


class OptionScheduleTests(unittest.TestCase):
    def test_runoff_schedules_and_dry_days_edit_clear_json_rollback(self):
        for units in UNITS:
            for field,values in (('wet_step',(timedelta(seconds=30),timedelta(seconds=120))),
                                 ('dry_step',(timedelta(seconds=120),timedelta(seconds=300))),
                                 ('dry_days',(0,.5,2,5))):
                with self.subTest(units=units,field=field):
                    model=schedule_fixture(units);model.update_options(**{field:None})
                    baseline=model.to_json_document().to_bytes()
                    with self.assertRaises(RuntimeError):
                        with model.transaction():
                            model.update_options(**{field:values[-1]})
                            raise RuntimeError('rollback')
                    self.assertEqual(model.to_json_document().to_bytes(),baseline)
                    for value in values:
                        model.update_options(**{field:value})
                        restored=Model.from_json_document(model.to_json_document(),strict=True)
                        self.assertEqual(load(restored.to_document(normalize=True).text).options,model.options)
                    model.update_options(**{field:None})
                    self.assertIn(field,model.effective_options.defaults_used)
                    self.assertIsNone(getattr(load(model.to_document().text).options,field))


if __name__=='__main__':unittest.main()
