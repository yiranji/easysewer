"""Bounded valid-series inspection preserves the editable document contract."""

from datetime import date
import gc
import random
import tracemalloc
import unittest
from unittest.mock import patch

from easysewer.io import data_inspection
from easysewer.io.timeseries import TimeSeriesData
from easysewer.model import FileReference
from easysewer.model.resources import FileTimeSeries
from test_hydrology_v2 import hydrology_model


def context():
    model=hydrology_model()
    model.timeseries.replace('Rain',FileTimeSeries(id='Rain',file=FileReference(path='rain.dat')))
    use=next(u for u in model.file_uses() if u.format=='swmm:timeseries.data')
    return model,use


class SeriesPreflightTests(unittest.TestCase):
    def compare(self, raw, *, model=None, encoding='utf-8'):
        original,use=context()
        model=original if model is None else model
        options=dict(use=use,model=model,encoding=encoding,source='rain.dat')
        observed=data_inspection.inspect_data(raw,**options)
        with patch.object(data_inspection,'_series',data_inspection._series_document):
            expected=data_inspection.inspect_data(raw,**options)
        self.assertEqual(observed,expected)
        return observed

    def test_all_time_bases_errors_and_warning_precedence(self):
        cases=[b'',b'; comment',b'0 1',b'-1 -2\n0 3',b'0 1\n0 2',
               b'2 1\n1 2\n0 3',b'0 1\n01/01/2020 1 2\n2 3',
               b'0 1\n01/01/1900 1 2',b'01/01/2020 25:00 1\n26:00 2',
               b'01/01/2020 0:00:01.9 1 ignored\n0:01 2',
               b'0 1\n0 2\nbad',b'0 1\n0 2\n\xff',b'0 nan',
               b'100000000 1\n100000001 2',b'0 1\r1 2',b'0 1\x1a',
               b'\xef\xbb\xbf0 1',b'01/01/20_20 0 1',b'2147483648:00 1',
               b';'+b'x'*1022+b'\n0 1',b'0 1\n'+b'x'*1023,
               b'; caf\xe9\r\n0 1\r\n1 2\r\n']
        for raw in cases:
            with self.subTest(raw=raw[:60]):
                self.compare(raw,encoding='cp1252' if b'caf' in raw else 'utf-8')
        for start in (date(1,1,1),date(9999,12,31)):
            model,_=context();model.update_options(start_date=start)
            for raw in (b'-24 1\n0 2',b'0 1\n48 2',b'0 1\n01/01/9999 0 2'):
                self.compare(raw,model=model)

    def test_seeded_relative_calendar_and_mixed_sequences(self):
        rng=random.Random(147)
        for index in range(120):
            rows=[];calendar=False
            for position in range(rng.randrange(1,15)):
                hour=rng.randrange(-2,25) if index%3==0 else position/10
                if not calendar and position and index%2:
                    calendar=True
                    head=f'01/01/2020 {hour}'
                else:
                    head=str(hour)
                rows.append(f'{head} {rng.randrange(-1,4)}')
            with self.subTest(index=index):
                self.compare(('\r\n'.join(rows)+('\n' if index%2 else '')).encode())

    def test_valid_inspection_does_not_construct_editable_point_collections(self):
        model,use=context()
        with patch.object(TimeSeriesData,'from_bytes',side_effect=AssertionError('materialized')):
            observed=data_inspection.inspect_data(b'0 1\n1 2',use=use,model=model)
        self.assertEqual(observed.status,'validated')
        self.assertEqual(dict(observed.facts)['points'],2)

    def test_working_memory_does_not_grow_by_one_object_per_point(self):
        raw=''.join(f'{index} 1\n' for index in range(50000)).encode()
        model,use=context();gc.collect();tracemalloc.start()
        try:
            observed=data_inspection.inspect_data(raw,use=use,model=model)
            _,peak=tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(observed.status,'validated')
        self.assertEqual(dict(observed.facts)['points'],50000)
        self.assertLess(peak,3*len(raw)+512*1024)


if __name__=='__main__':
    unittest.main()
