"""Hydrology/climate diagnostics identify actual owners and original evidence."""
from dataclasses import replace
from datetime import timedelta
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from easysewer.io.inp import InpDocument
from easysewer.model import Model, Ref
from easysewer.model import climate as c, hydrology as h
from easysewer.runtime._result_codec import Codec
from easysewer.validation import DiagnosticSubject, ValidationError

SOURCE = str(Path.cwd()/'original'/'水文 气候.inp')
BASE = '''[OPTIONS]
START_DATE 01/01/2020
END_DATE 01/02/2020
[RAINGAGES]
R INTENSITY 1:00 1 TIMESERIES Rain
[TIMESERIES]
Rain 0:00 0 1:00 1 2:00 0
[JUNCTIONS]
J 0
[SUBCATCHMENTS]
S R J 2 30 100 1 0
[SUBAREAS]
S .01 .2 .05 .1 25 OUTLET
[INFILTRATION]
S 3 .2 4 2 0 HORTON
'''
SNOW = '[SNOWPACKS]\nSnow PLOWABLE .001 .004 32 .1 1 0 .5\n'
TRANSFER = 'Snow REMOVAL 0 0 0 0 0 .5 Receiver\n'
EVIDENCE = []


def parse(text=BASE):
    return Model.from_document(InpDocument.from_text(text, source=SOURCE), strict=False)


def issue(model, code, *, for_run=True):
    return next(d for d in model.validate(for_run=for_run).diagnostics if d.code == code)


def tokens(model, location):
    lines = model.document.text.splitlines()
    return tuple(lines[s.line-1][s.column-1:s.end_column-1] for s in location.spans)


class HydroClimateDiagnosticTests(unittest.TestCase):
    def check_roundtrip(self, model, diagnostic):
        self.assertIsNotNone(diagnostic.subject)
        self.assertEqual({v.subject for v in diagnostic.locations}, {diagnostic.subject, *diagnostic.related})
        for location in diagnostic.locations:
            if location.spans:
                self.assertEqual(location.source_sha256, hashlib.sha256(model.document.text.encode()).hexdigest())
                self.assertTrue(all(s.source == SOURCE for s in location.spans))
        restored = Model.from_json_document(model.to_json_document(), strict=False)
        self.assertEqual(restored.validate(), model.validate())
        self.assertEqual(restored.validate(for_run=True), model.validate(for_run=True))
        codec = Codec({}); encoded = codec.encode(diagnostic)
        self.assertEqual(codec.decode(encoded), diagnostic)
        EVIDENCE.append(dict(code=diagnostic.code, collection=diagnostic.subject.collection,
            path=diagnostic.subject.path, statuses=[v.status for v in diagnostic.locations]))

    def test_catchment_semantic_cases_and_inherited_method(self):
        cases = (
            ('subcatchment.impervious_clamp', BASE.replace('2 30 100', '2 120 100'), ('impervious_percent',), False, 'current'),
            ('subcatchment.missing_subareas', BASE.replace('S .01 .2 .05 .1 25 OUTLET\n',''), ('subareas',), True, 'omitted'),
            ('subcatchment.missing_infiltration', BASE.replace('S 3 .2 4 2 0 HORTON\n',''), ('infiltration',), True, 'omitted'),
            ('subcatchment.zero_storage_percent', BASE.replace('.1 25 OUTLET','.1 125 OUTLET'), ('subareas','zero_storage_percent'), True, 'current'),
            ('infiltration.curve_number_clamp', BASE.replace('3 .2 4 2 0 HORTON','101 0 2 CURVE_NUMBER'), ('infiltration','parameters','curve_number'), False, 'current'),
        )
        for code, text, path, running, status in cases:
            with self.subTest(code=code):
                m = parse(text); d = issue(m, code, for_run=running)
                self.assertEqual(d.subject, DiagnosticSubject(collection='swmm:subcatchments',key='S',path=path))
                self.assertEqual(d.locations[0].status,status); self.check_roundtrip(m,d)
        m = parse(); m.subcatchments.add(replace(m.subcatchments['S'], id='J'))
        d = issue(m, 'subcatchment.ambiguous_outlet')
        self.assertEqual(d.subject,DiagnosticSubject(collection='swmm:subcatchments',key='S',path=('outlet','key')))
        self.assertEqual([(v.collection,v.key) for v in d.related], [('swmm:nodes','J'),('swmm:subcatchments','J')])
        self.assertEqual([v.status for v in d.locations],['current','context','programmatic'])
        self.check_roundtrip(m,d)
        m = parse(BASE.replace(' 0 HORTON',' 0'))
        m.update_options(infiltration='GREEN_AMPT')
        d = issue(m,'infiltration.method_parameters')
        self.assertEqual(d.subject.path,('infiltration','parameters'))
        self.assertEqual(d.related[-1],DiagnosticSubject(collection='swmm:options',key='settings',path=('infiltration',)))
        self.assertEqual([v.status for v in d.locations],['current','omitted','changed'])
        self.check_roundtrip(m,d)

    def test_rain_series_current_related_edit_rename_and_no_io(self):
        m = parse(BASE.replace('1:00 1','1:00 -1'))
        with patch('builtins.open',side_effect=AssertionError('validation does not open input')), patch.object(Model,'to_document',side_effect=AssertionError('no rendering')):
            d = issue(m,'rainfall.negative_series')
        self.assertEqual(d.subject,DiagnosticSubject(collection='swmm:raingages',key='R',path=('source','series','key')))
        self.assertEqual(tokens(m,d.locations[0]),('Rain',))
        self.assertIn('-1',tokens(m,d.locations[1])); self.check_roundtrip(m,d)
        m.timeseries.rename('Rain','Storm')
        renamed = issue(m,'rainfall.negative_series')
        self.assertEqual([v.status for v in renamed.locations],['changed','changed'])
        self.assertEqual(renamed.locations[1].original.key,'RAIN'); self.assertIsNone(renamed.span)
        self.check_roundtrip(m,renamed)
        m = parse(BASE.replace('1:00 1','1:00 -1'))
        with self.assertRaises(ValidationError):
            with m.transaction(): m.raingages.update('R',interval=timedelta())
        self.assertEqual(issue(m,'rainfall.negative_series'),d)

    def test_rain_sharing_and_file_conflicts_retain_both_consumers(self):
        texts = {
            'rainfall.negative_snow_factor': BASE.replace('1:00 1 TIMESERIES','1:00 -1 TIMESERIES'),
            'rainfall.interval_exceeds_series': BASE.replace('INTENSITY 1:00','INTENSITY 2:00'),
            'rainfall.shared_gage_settings': BASE+'[RAINGAGES]\nOther VOLUME 1:00 1 TIMESERIES Rain\n[SUBCATCHMENTS]\nT Other J 0 100 1 1 0\n',
            'rainfall.exclusive_series': BASE+'[EVAPORATION]\nTIMESERIES Rain\n',
            'rainfall.station_file_conflict': BASE.replace('TIMESERIES Rain','FILE first.dat Station IN')+'[RAINGAGES]\nOther INTENSITY 1:00 1 FILE second.dat Station IN\n',
            'rainfall.shared_file_settings': BASE.replace('TIMESERIES Rain','FILE first.dat Station IN')+'[RAINGAGES]\nOther VOLUME 2:00 1 FILE first.dat Station MM\n',
        }
        for code,text in texts.items():
            with self.subTest(code=code):
                m=parse(text); d=issue(m,code)
                self.assertEqual(d.subject.collection,'swmm:raingages')
                self.assertEqual(d.locations[0].status,'current'); self.check_roundtrip(m,d)
                if 'shared_' in code or code.endswith('station_file_conflict'):
                    self.assertEqual(d.subject.key,'Other'); self.assertIn('R',{v.key for v in d.related})
                if code.endswith('exclusive_series'):
                    self.assertIn(DiagnosticSubject(collection='swmm:climate',key='settings',path=('evaporation','source','series')),d.related)
                if code.endswith('interval_exceeds_series'):
                    self.assertIn(DiagnosticSubject(collection='swmm:timeseries',key='Rain',path=('points',)),d.related)

    def test_snow_surface_and_removal_cases(self):
        cases = (
            ('snowpack.melt_order',SNOW.replace('.001 .004','.01 .004'),('plowable','minimum_melt'),True),
            ('snowpack.free_water_clamp',SNOW.replace('.1 1 0 .5','.1 1 .9 .5'),('plowable','initial_free_water'),False),
            ('snowpack.negative_parameter',SNOW.replace('.001 .004','-.001 .004'),('plowable',),True),
            ('snowpack.removal_fractions',SNOW+'Snow REMOVAL 0 .6 .6 0 0\n',('removal',),True),
            ('snowpack.missing_destination',SNOW+'Snow REMOVAL 0 0 0 0 0 .5\n',('removal','destination'),True),
        )
        for code,snow,path,running in cases:
            with self.subTest(code=code):
                m=parse(BASE+snow); d=issue(m,code,for_run=running)
                self.assertEqual(d.subject,DiagnosticSubject(collection='swmm:snowpacks',key='Snow',path=path))
                self.assertEqual(d.locations[0].status,'omitted' if code.endswith('missing_destination') else 'current')
                self.check_roundtrip(m,d)

    def test_snow_transfer_recipient_and_different_area_owners(self):
        base=BASE.replace('S R J 2 30 100 1 0','S R J 2 30 100 1 0 Snow')+SNOW+TRANSFER
        for suffix,code in (('Receiver R J 3 100 1 1 0','snowpack.ignored_transfer'),
                            ('Receiver R J 3 30 1 1 0 Snow','snowpack.native_transfer_area')):
            with self.subTest(code=code):
                m=parse(base+'[SUBCATCHMENTS]\n'+suffix+'\n');d=issue(m,code)
                self.assertEqual(d.subject.path,('removal','destination','key'))
                self.assertEqual(tokens(m,d.locations[0]),('Receiver',))
                self.assertTrue(all(v.collection=='swmm:subcatchments' for v in d.related))
                if code.endswith('native_transfer_area'):
                    self.assertEqual([(v.key,v.path) for v in d.related],[('Receiver',('area',)),('S',('area',))])
                self.check_roundtrip(m,d)

    def test_climate_variants_keep_display_id_but_bind_real_singleton(self):
        months=' '.join(['-1']+['1']*11)
        cases=(
            ('[EVAPORATION]\nCONSTANT -1\n','climate.negative_evaporation',('evaporation','source','rate'),True),
            ('[EVAPORATION]\nMONTHLY '+months+'\n','climate.negative_evaporation',('evaporation','source','values'),True),
            ('[EVAPORATION]\nTIMESERIES Evap\n[TIMESERIES]\nEvap 0 -1\n','climate.negative_evaporation',('evaporation','source','series','key'),True),
            ('[TEMPERATURE]\nWINDSPEED MONTHLY '+months+'\n','climate.negative_wind',('wind','values'),True),
            ('[TEMPERATURE]\nFILE missing.dat\n[EVAPORATION]\nFILE '+months+'\n','climate.negative_pan_coefficient',('evaporation','source','pan_coefficients','values'),True),
            ('[ADJUSTMENTS]\nCONDUCTIVITY '+months+'\n','climate.conductivity_default',('adjustments','conductivity','values'),False),
            ('[TEMPERATURE]\nFILE missing.dat\nTIMESERIES Rain\n[EVAPORATION]\nTEMPERATURE\n','climate.temperature_evap_source',('temperature','series','key'),True),
            ('[TEMPERATURE]\nWINDSPEED FILE\n[EVAPORATION]\nTEMPERATURE\n','climate.missing_file',('file',),True),
        )
        for text,code,path,running in cases:
            with self.subTest(code=code,path=path):
                m=parse(BASE+text);d=issue(m,code,for_run=running)
                self.assertEqual(d.object_id,'climate')
                self.assertEqual(d.subject,DiagnosticSubject(collection='swmm:climate',key='settings',path=path))
                self.assertEqual(d.locations[0].status,'omitted' if code.endswith('missing_file') else 'current')
                if code.endswith('missing_file'):
                    self.assertEqual({v.path for v in d.related},{('wind',),('evaporation','source')})
                self.check_roundtrip(m,d)
        m=parse(BASE+'[TEMPERATURE]\nFILE missing.dat\n');m.update_climate(file=None)
        for code in ('climate.missing_temperature_file','climate.missing_file'):
            d=issue(m,code)
            self.assertEqual(d.locations[0].status,'changed');self.assertIsNone(d.span)
            self.assertIn(DiagnosticSubject(collection='swmm:climate',key='settings',path=('temperature',)),d.related)
            self.check_roundtrip(m,d)

    def test_replaced_variant_duplicate_rows_and_programmatic_diagnostics(self):
        m=parse(BASE+'[EVAPORATION]\nCONSTANT -2\nCONSTANT -3\n')
        before=issue(m,'climate.negative_evaporation')
        self.assertEqual(tokens(m,before.locations[0]),('-3',))
        m.update_climate(evaporation=c.Evaporation(source=c.MonthlyEvaporation(values=(-1.0,)*12)))
        d=issue(m,'climate.negative_evaporation')
        self.assertEqual(d.locations[0].status,'untracked'); self.assertFalse(d.locations[0].spans)
        self.check_roundtrip(m,d)
        m=Model();m.update_climate(evaporation=c.Evaporation(source=c.ConstantEvaporation(rate=-1)))
        self.assertEqual(issue(m,'climate.negative_evaporation').locations[0].status,'programmatic')
        m=parse(BASE+SNOW+'[SNOWPACKS]\nSnow PLOWABLE .01 .004 32 .1 1 0 .5\n')
        d=issue(m,'snowpack.melt_order');self.assertEqual(tokens(m,d.locations[0]),('.01',))
        self.assertEqual(tokens(m,d.locations[1]),('.004',));self.check_roundtrip(m,d)

    def test_runner_rejection_retains_both_domains_without_native_loading(self):
        from easysewer.runtime import Runner, RunResult
        from test_runner_v2 import config
        m=parse(BASE.replace('1:00 1','1:00 -1')+'[EVAPORATION]\nCONSTANT -2\n')
        with tempfile.TemporaryDirectory() as directory, patch('ctypes.CDLL',side_effect=AssertionError('No solver load')):
            root=Path(directory);result=Runner().run(m,config(root/'run',keep_failed_artifacts=False))
            self.assertEqual(result.status,'rejected')
            for code in ('rainfall.negative_series','climate.negative_evaporation'):
                found=next(d for d in result.diagnostics.errors if d.code==code)
                self.assertEqual(found,issue(m,code));self.assertTrue(found.locations[0].spans)
            result.save(root/'saved');self.assertEqual(RunResult.load(root/'saved'),result)


if __name__=='__main__':unittest.main()
