"""Independent byte fixtures for RAIN metadata, spans and time/value checks."""

from datetime import date, time, timedelta
import struct
import unittest
from unittest.mock import patch

from easysewer.io.interface_inspection import inspect_interface
from easysewer.model import FileReference
from easysewer.model.hydrology import FileRainfall
from test_hydrology_v2 import hydrology_model


def rain_model(path, station='Station'):
    model = hydrology_model()
    model.update_options(end_date=date(2020, 1, 30), end_time=time(0, 20),
        routing_step=timedelta(seconds=60), report_step=timedelta(minutes=1))
    model.raingages.update('R', form='VOLUME', interval=timedelta(minutes=5),
        source=FileRainfall(file=FileReference(path=str(path)), station=station, units='IN'))
    return model


def rain_bytes(stations=None):
    if stations is None:
        stations = [('Station', 300, ((43860., 0.), (43860.+300/86400, .2), (43860.+600/86400, .4)))]
    header = bytearray(b'SWMM5-RAIN'+struct.pack('<i', len(stations)))
    payload = bytearray()
    offset = 14 + 1037*len(stations)
    for name, interval, readings in stations:
        values = b''.join(struct.pack('<df', *row) for row in readings)
        header.extend(name.encode()+b'\0'*(1025-len(name.encode())))
        header.extend(struct.pack('<iii', interval, offset+len(payload), offset+len(payload)+len(values)))
        payload.extend(values)
    return bytes(header+payload)


def malformed_rain():
    good = rain_bytes()
    cases = [(f'prefix-{n}', good[:n], 320) for n in (0, 1, 9, 10, 13, 14, 1050)]
    cases += [('signature', b'BAD'+good[3:], 320)]
    for value in (-1, 2147483647):
        cases.append((f'count-{value}', b'SWMM5-RAIN'+struct.pack('<i', value), 320))
    for offset, values in ((1039, (0, -1)), (1043, (-1, 1050, 1088)),
                           (1047, (-1, 1050, 1086, 1088, 2147483647))):
        for value in values:
            damaged = bytearray(good); struct.pack_into('<i', damaged, offset, value)
            cases.append((f'header-{offset}-{value}', bytes(damaged), 320))
    cases += [('unterminated', good[:14]+b'X'*1025+good[1039:], 320),
              ('empty-name', good[:14]+b'\0'*1025+good[1039:], 320)]
    for fmt, offset, values in (('<d',1051,(float('nan'),float('inf'),-float('inf'),1e100,-693594.,2958466.)),
                                 ('<f',1059,(float('nan'),float('inf'),-float('inf'))),
                                 ('<d',1063,(43860.,43859.))):
        for index, value in enumerate(values):
            damaged=bytearray(good);struct.pack_into(fmt,damaged,offset,value)
            cases.append((f'sample-{offset}-{index}',bytes(damaged),320))
    cases += [('empty-station',rain_bytes([('Station',300,())]),321),
              ('missing-station',rain_bytes([('Different',300,((43860.,.2),))]),321),
              ('case-sensitive',rain_bytes([('station',300,((43860.,.2),))]),321),
              ('zero-stations',b'SWMM5-RAIN'+struct.pack('<i',0),321),
              ('unused-invalid',rain_bytes([('Station',300,((43860.,.2),)),
                  ('Unused',300,((43860.,float('nan')),))]),320)]
    return cases


class RainInterfaceTests(unittest.TestCase):
    def test_malformed_headers_offsets_and_payloads_are_rejected(self):
        model=rain_model('/unused.txt')
        for name, data, code in malformed_rain():
            with self.subTest(case=name):
                self.assertFalse(inspect_interface(data,'RAINFALL',model).report.is_valid)

    def test_duplicate_station_ids_keep_first_case_sensitive_assignment(self):
        data=rain_bytes([('Station',300,((43860.,.2),)),('Station',600,())])
        model=rain_model('/unused.txt')
        self.assertTrue(inspect_interface(data,'RAINFALL',model).report.is_valid)
        reversed_data=rain_bytes([('Station',600,()),('Station',300,((43860.,.2),))])
        self.assertFalse(inspect_interface(reversed_data,'RAINFALL',model).report.is_valid)

    def test_adjacent_station_calendars_can_restart_and_sparse_gaps_are_allowed(self):
        data=rain_bytes([('Station',300,((43860.,0.),(43870.,.2))),('Other',300,((43860.,.2),))])
        self.assertTrue(inspect_interface(data,'RAINFALL',rain_model('/unused.txt')).report.is_valid)

    def test_shared_ranges_do_not_multiply_payload_validation_work(self):
        count=1000; start=14+1037*count
        record=b'Station'+b'\0'*1018+struct.pack('<iii',300,start,start+12*1000)
        data=b'SWMM5-RAIN'+struct.pack('<i',count)+record*count
        data+=b''.join(struct.pack('<df',43860+i/288,.2) for i in range(1000))
        original=struct.unpack_from
        with patch('easysewer.io.interface_inspection.struct.unpack_from',wraps=original) as unpack:
            self.assertTrue(inspect_interface(data,'RAINFALL',rain_model('/unused.txt')).report.is_valid)
        self.assertLess(unpack.call_count,5000)


if __name__=='__main__':
    unittest.main()
