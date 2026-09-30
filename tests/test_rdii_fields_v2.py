"""RDII seasonal configuration, native time boundaries and ordered sources."""
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import date, datetime, time, timedelta
import unittest
from unittest.mock import patch

from easysewer.model import Model, Ref
from easysewer.model import rdii as r
from easysewer.model.resources import SeriesPoint
from easysewer.io.inp import InpDocument
from test_hydrology_fields_v2 import fixture as hydrology_fixture, UNITS
from test_rdii_v2 import response

GROUP = Ref(collection='swmm:hydrographs', key='UH')
INFLOW = Ref(collection='swmm:rdii', key='J')
def ref(name, key): return Ref(collection='swmm:' + name, key=key)
def load(text): return Model.from_document(InpDocument.from_text(text, source='rdii-fields.inp'), strict=True)


def fixture(units='CFS', mode='defaults', month=1):
    m = load(hydrology_fixture(units=units))
    when = date(2020, month, 1)
    m.update_options(start_date=when, end_date=when, report_start_date=when)
    ia = {} if mode == 'defaults' else dict(maximum_abstraction=.01, recovery_rate=.02, initial_abstraction=.002)
    rows = tuple(response(kind=kind, fraction=.1/(index+1), peak=.015 + .0111*index, k=1.3, **ia)
                 for index, kind in enumerate(r.RESPONSES))
    if mode in ('override', 'crossmonth'):
        rows += (response(month='FEB', fraction=.2, peak=.0077, k=1.1, **ia),)
    if mode == 'override':
        rows += (response(fraction=.15, peak=.0199, k=1.2),)
    m.hydrographs.add(r.UnitHydrograph(id='UH', rain_gage=ref('raingages','R'), responses=rows))
    m.rdii.add(r.RdiiInflow(node=ref('nodes','J'), hydrograph=GROUP, sewer_area=2))
    if mode == 'prior':
        m.raingages.add(replace(m.raingages['R'], id='Earlier'))
        m.hydrographs.update('UH', prior_rain_gages=(ref('raingages','Earlier'),))
    if mode == 'crossmonth':
        m.update_options(start_date=date(2020,1,31), start_time=time(23,50), end_date=date(2020,2,1),
            end_time=time(0,20), report_start_date=date(2020,1,31), report_start_time=time(23,50))
        start=datetime(2020,1,31,23,50)
        m.timeseries.update('Rain',points=tuple(SeriesPoint(time=start+timedelta(minutes=i),value=2 if 1<=i<15 else 0) for i in range(31)))
    source=m.to_document().text
    if mode == 'legacy':
        # Use the parser's real legacy shape, not three already expanded rows.
        source += '[HYDROGRAPHS]\nUH ALL .1 .015 1.3 .05 .0261 1.3 .03 .0372 1.3 .01 .02 .002\nUH FEB SHORT .2 .0077 1.1\n'
    return source


def queries(m):
    result=[]
    def walk(owner,value,path=()):
        if is_dataclass(value):
            for f in fields(value):
                p=path+(f.name,);result.extend((m.inspect_field(owner,p),m.field_provenance(owner,p)));walk(owner,getattr(value,f.name),p)
        elif isinstance(value,tuple):
            for index,value in enumerate(value):
                p=path+(index,);result.extend((m.inspect_field(owner,p),m.field_provenance(owner,p)));walk(owner,value,p)
    for namespace in ('hydrographs','rdii'):
        for key,row in m.collection('swmm:'+namespace).items():walk(ref(namespace,key),row)
    return tuple(result)


class RdiiFieldTests(unittest.TestCase):
    def test_seasonal_variants_units_defaults_and_json(self):
        for units in UNITS:
            for mode in ('defaults','explicit','override','prior','legacy','crossmonth'):
                with self.subTest(units=units,mode=mode):
                    m=load(fixture(units,mode));before=queries(m)
                    for info in before[::2]:
                        self.assertIn(info.semantics.effective.status,('known','not_applicable'),info.path)
                        self.assertNotEqual(info.semantics.unit.status,'unknown',info.path)
                    self.assertEqual(queries(Model.from_json_document(m.to_json_document(),strict=True)),before)
                    self.assertEqual(m.inspect_field(INFLOW,'sewer_area').semantics.unit.value,'acre' if units in UNITS[:3] else 'ha')
                    if mode=='defaults':
                        for name in ('maximum_abstraction','recovery_rate','initial_abstraction'):
                            info=m.inspect_field(GROUP,('responses',0,name))
                            self.assertIsNone(info.value);self.assertEqual(info.semantics.effective.value,0)
                            self.assertEqual(m.field_provenance(GROUP,('responses',0,name)).status,'omitted')

    def test_all_months_and_types_with_optional_tails(self):
        for month in ('ALL',*r.MONTHS):
            for kind in r.RESPONSES:
                for tail in ('',' .2',' .2 .1',' .2 .1 .05'):
                    source=hydrology_fixture()+'[HYDROGRAPHS]\nUH R\nUH '+month+' '+kind+' .1 .00055 .99'+tail+'\n[RDII]\nJ UH 2\n'
                    m=load(source);item=m.hydrographs['UH'].responses[0]
                    for name in ('time_to_peak','recession_ratio'):
                        fact=m.inspect_field(GROUP,('responses',0,name)).semantics.effective
                        self.assertEqual(fact.value,getattr(item,name));self.assertIn('1/3 seconds',fact.reason)
                    self.assertEqual(m.field_provenance(GROUP,('responses',0,'month')).declarations[0].tokens[0].value,month)

    def test_ordered_overrides_keep_partially_active_all_and_equal_rows(self):
        m=load(fixture(mode='crossmonth'))
        fact=m.inspect_field(GROUP,('responses',0,'fraction')).semantics.effective
        self.assertEqual(fact.status,'known');self.assertNotIn('FEB',fact.reason);self.assertIn('JAN',fact.reason)
        m=load(fixture(mode='override'))
        self.assertEqual(m.inspect_field(GROUP,('responses',0)).semantics.effective.status,'not_applicable')
        self.assertEqual(m.inspect_field(GROUP,('responses',3,'fraction')).semantics.effective.status,'not_applicable')
        row=m.hydrographs['UH'].responses[-1]
        m.hydrographs.update('UH',responses=(row,row))
        self.assertEqual(m.inspect_field(GROUP,('responses',0)).semantics.effective.status,'not_applicable')
        self.assertEqual(m.inspect_field(GROUP,('responses',1)).semantics.effective.status,'known')

    def test_legacy_source_positions_and_prior_gages(self):
        m=load(fixture(mode='legacy'))
        for index in (3,4,5):
            self.assertEqual(m.field_provenance(GROUP,('responses',index,'response')).declarations[0].role,'derived')
            self.assertEqual(m.field_provenance(GROUP,('responses',index,'maximum_abstraction')).declarations[0].tokens[0].value,'.01')
        m=load(fixture(mode='prior'))
        self.assertEqual(m.inspect_field(GROUP,'prior_rain_gages').semantics.effective.value,(ref('raingages','Earlier'),))
        self.assertEqual(m.field_provenance(GROUP,('prior_rain_gages',0,'key')).declarations[0].tokens[0].value,'Earlier')
        self.assertEqual(m.field_provenance(GROUP,('rain_gage','key')).declarations[0].tokens[0].value,'R')
        self.assertEqual(queries(Model.from_json_document(m.to_json_document(),strict=True)),queries(m))

    def test_generation_limits_missing_references_and_extension(self):
        m=load(fixture());m.hydrographs.update('UH',rain_gage=None)
        self.assertEqual(m.inspect_field(INFLOW,'sewer_area').semantics.effective.status,'invalid')
        m.update_options(ignore_rdii=True)
        self.assertEqual(m.inspect_field(INFLOW,'sewer_area').semantics.effective.status,'known')
        m=load(fixture());m.hydrographs.update('UH',responses=(response(fraction=1.011),))
        self.assertEqual(m.inspect_field(GROUP,'responses').semantics.effective.status,'invalid')
        m.hydrographs.update('UH',responses=(response(fraction=1.005),))
        self.assertEqual(m.inspect_field(GROUP,'responses').semantics.effective.status,'known')
        self.assertIn('rdii.response_tolerance',{d.code for d in m.validate().diagnostics})
        m.hydrographs.update('UH',responses=(response(fraction=2,peak=0),))
        self.assertEqual(m.inspect_field(GROUP,'responses').semantics.effective.status,'known')
        m.hydrographs.update('UH',rain_gage=ref('raingages','Missing'))
        self.assertEqual(m.inspect_field(GROUP,'responses').semantics.effective.status,'invalid')
        @dataclass(frozen=True,kw_only=True)
        class Extension(r.HydrographResponse):pass
        m=load(fixture());m.hydrographs.update('UH',responses=(Extension(month='ALL',response='SHORT',fraction=.1,time_to_peak=1,recession_ratio=2),))
        self.assertEqual(m.inspect_field(GROUP,'responses').semantics.effective.status,'unknown')

    def test_initial_abstraction_not_clamped_and_identity_rollback(self):
        m=load(fixture());m.hydrographs.update('UH',responses=(response(maximum_abstraction=.1,initial_abstraction=.2),))
        self.assertEqual(m.inspect_field(GROUP,('responses',0,'initial_abstraction')).semantics.effective.value,.2)
        before=queries(m)
        with self.assertRaises(RuntimeError):
            with m.transaction():
                m.raingages.rename('R','Gauge');m.nodes.rename('J','Receiving');raise RuntimeError
        self.assertEqual(queries(m),before)
        m.hydrographs.rename('UH','Seasonal');m.nodes.rename('J','Receiving');m.raingages.rename('R','Gauge')
        m.convert_units('CMS')
        owner=ref('hydrographs','Seasonal')
        self.assertAlmostEqual(m.inspect_field(owner,('responses',0,'maximum_abstraction')).semantics.effective.value,2.54)
        self.assertEqual(m.field_provenance(owner,('rain_gage','key')).value.value,'R')
        self.assertEqual(queries(Model.from_json_document(m.to_json_document(),strict=True)),queries(m))

    def test_interface_and_ignore_generation_context_without_file_io(self):
        m=load(fixture());m.hydrographs.update('UH',rain_gage=None)
        m=load(m.to_document().text+'[FILES]\nUSE RDII "missing.ifc"\n')
        with patch('builtins.open',side_effect=AssertionError('Field query must not open the interface')):
            self.assertEqual(m.inspect_field(INFLOW,'sewer_area').semantics.effective.value,2)
        binding=m.files[('RDII','USE')]
        m.files.update(binding.key,file=replace(binding.file,direction='output'))
        self.assertEqual(m.inspect_field(INFLOW,'sewer_area').semantics.effective.status,'invalid')
        m.files.remove(binding.key)
        self.assertEqual(m.inspect_field(INFLOW,'sewer_area').semantics.effective.status,'invalid')
        m.update_options(ignore_rainfall=True)
        self.assertEqual(m.inspect_field(INFLOW,'sewer_area').semantics.effective.status,'known')

    def test_malformed_group_not_partially_claimed(self):
        source='[HYDROGRAPHS]\nUH R\nUH ALL SHORT .1 1 2\nUH ALL bad\n'
        m=Model.from_document(InpDocument.from_text(source))
        self.assertFalse(m.hydrographs);self.assertFalse(m.validate().is_valid)
        self.assertEqual(m.document.text,source)


if __name__=='__main__':unittest.main()
