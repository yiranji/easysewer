"""Active process fixtures and ordered option histories for OPTIONS gates."""
from datetime import date, time, timedelta
from dataclasses import replace
import unittest

from easysewer.io.inp import InpDocument
from easysewer.model import Model, Ref
from easysewer.model import climate as c
from easysewer.model.report import ReportSelection
from test_hydrology_v2 import hydrology_model, snowpack
from test_groundwater_v2 import groundwater_model
from test_rdii_v2 import rdii_model
from test_quality_v2 import quality_model
from test_climate_v2 import add_series
from test_native_v2_checkpoint_dynwave import fixture as ponding_source
from test_native_v2_network import NETWORK, SETTINGS

OWNER = Ref(collection='swmm:options', key='settings')
UNITS = ('CFS', 'GPM', 'MGD', 'CMS', 'LPS', 'MLD')
SWITCHES = ('ignore_rainfall', 'ignore_snowmelt', 'ignore_groundwater', 'ignore_rdii',
            'ignore_routing', 'ignore_quality', 'allow_ponding', 'skip_steady_state')


def load(text):
    return Model.from_document(InpDocument.from_text(text, source='active-options.inp'), strict=True)


def fixture(field, units='CFS'):
    if field == 'allow_ponding':
        m = load(ponding_source(ponding=True, fixed=True))
    elif field == 'skip_steady_state':
        m = load(NETWORK.format(shape='CIRCULAR 2 0 0 0')+SETTINGS)
        m.reinterpret_units('CFS')
    else:
        m = {'ignore_groundwater': groundwater_model, 'ignore_rdii': rdii_model,
             'ignore_quality': quality_model}.get(field, hydrology_model)()
        m.update_options(end_date=date(2020, 1, 30), end_time=time(8),
            report_step=timedelta(minutes=15), routing_step=timedelta(seconds=60))
        if field == 'ignore_snowmelt':
            m.snowpacks.add(snowpack(initial=3))
            m.subcatchments.update('S', snowpack=Ref(collection='swmm:snowpacks', key='Snow'))
            air = add_series(m, 'Air', ((0, 45), (8, 45)))
            m.update_climate(temperature=c.SeriesTemperature(series=air))
    m.update_options(**{field: None})
    m.update_report(nodes=ReportSelection(mode='ALL'), links=ReportSelection(mode='ALL'),
                    subcatchments=ReportSelection(mode='ALL'))
    m.convert_units(units)
    return m


def infiltration_fixture(method, units='CFS'):
    m = hydrology_model(method)
    m.update_options(infiltration=method, end_date=date(2020, 1, 30), end_time=time(8),
        report_step=timedelta(minutes=15), routing_step=timedelta(seconds=60))
    # Low-intensity prewetting distinguishes Green-Ampt from its modified form.
    m.timeseries.update('Rain', points=tuple(replace(p, value=.1) if p.time < timedelta(hours=2) else p
                                            for p in m.timeseries['Rain'].points))
    # The global OPTIONS value must actually control the method.
    m.subcatchments.update('S', infiltration=replace(m.subcatchments['S'].infiltration, method=None))
    m.update_report(nodes=ReportSelection(mode='ALL'), links=ReportSelection(mode='ALL'),
                    subcatchments=ReportSelection(mode='ALL'))
    m.convert_units(units)
    return m


class OptionEffectTests(unittest.TestCase):
    def test_active_fixture_switches_keep_ordered_sources_edit_clear_and_rollback(self):
        for field in SWITCHES:
            for units in UNITS:
                with self.subTest(field=field, units=units):
                    base = fixture(field, units)
                    self.assertTrue(base.validate(for_run=True).is_valid)
                    keyword = field.upper()
                    text = base.to_document().text+'[OPTIONS]\n'+keyword+' YES ; first\n'+keyword+' NO ; last\n'
                    m = load(text)
                    self.assertIs(getattr(m.options, field), False)
                    self.assertEqual([d.contributes for d in m.field_provenance(OWNER, field).declarations], [False, True])
                    before = m.to_json_document().to_bytes()
                    with self.assertRaises(RuntimeError):
                        with m.transaction():
                            m.update_options(**{field: True})
                            raise RuntimeError('rollback')
                    self.assertEqual(m.to_json_document().to_bytes(), before)
                    m.update_options(**{field: True})
                    parsed = Model.from_json_document(m.to_json_document(), strict=True)
                    self.assertIs(getattr(parsed.options, field), True)
                    self.assertIs(getattr(load(parsed.to_document(normalize=True).text).options, field), True)
                    m.update_options(**{field: None})
                    self.assertIsNone(getattr(m.options, field))
                    self.assertIs(getattr(m.effective_options.values, field), False)


if __name__ == '__main__': unittest.main()
