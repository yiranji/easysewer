"""Interface artifacts produced and consumed by the fixed SWMM 5.2.4 engine."""

from dataclasses import replace
from datetime import datetime, timedelta
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest

import easysewer
from easysewer import get_native_capabilities
from easysewer.io.inp import InpDocument
from easysewer.io.interface_inspection import inspect_interface
from easysewer.io.routing import RoutingInterface
from easysewer.model import Model, FileReference
from easysewer.model.hydrology import FileRainfall
from easysewer.model.report import ReportSelection
from easysewer.runtime import check_files
from test_files_v2 import bind, routing
from test_hydrology_v2 import hydrology_model
import test_native_v2_project as native_project
from test_options_v2 import network
from test_scenario_v2 import portable


def selected(model):
    model.update_options(report_step=timedelta(seconds=60))
    model.update_report(nodes=ReportSelection(mode='ALL'), links=ReportSelection(mode='ALL'),
                        subcatchments=ReportSelection(mode='ALL'))
    return model


@unittest.skipUnless(get_native_capabilities()['swmm_solver'] and get_native_capabilities()['swmm_output'], 'Native solver/output unavailable')
class NativeFilesTests(unittest.TestCase):
    def solve(self, directory, name, source, **kwargs):
        # Native hydrology report literals contain a single-byte superscript 3.
        return native_project.NativeProjectTests.solve(self, directory, name, source, report_encoding='cp1252', **kwargs)

    def test_hotstart_dual_bindings_reuse_actual_state_and_header_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); state = root/'state.hsf'; final = root/'final.hsf'
            model = selected(network())
            self.solve(root, 'producer', model.to_document().text + f'[FILES]\nSAVE HOTSTART "{state}"\n')
            self.assertTrue(state.read_bytes().startswith(b'SWMM5-HOTSTART4'))
            expected = self.solve(root, 'literal', model.to_document().text + f'[FILES]\nUSE HOTSTART "{state}"\nSAVE HOTSTART "{final}"\n')
            final.unlink()
            bind(model, 'HOTSTART', 'USE', state); bind(model, 'HOTSTART', 'SAVE', final)
            preflight = check_files(model)
            self.assertTrue(preflight.report.is_valid, preflight.report)
            self.assertFalse(preflight.complete)
            self.assertEqual(preflight.checks[0].inspection.status, 'state_checked')
            self.assertEqual(expected, self.solve(root, 'rebuilt', portable(model).to_document().text))
            self.assertTrue(final.is_file())
            cold = selected(network())
            self.assertNotEqual(expected['series'], self.solve(root, 'cold', cold.to_document().text)['series'])
            bad = bytearray(state.read_bytes()); struct.pack_into('<i', bad, len(b'SWMM5-HOTSTART4')+8, 999)
            self.assertFalse(inspect_interface(bad, 'HOTSTART', model).report.is_valid)

    def test_runoff_native_cache_reuse_and_complete_frame_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); cache = root/'runoff.bin'
            model = selected(hydrology_model())
            model.update_options(routing_step=timedelta(seconds=60))
            produced = self.solve(root, 'producer', model.to_document().text + f'[FILES]\nSAVE RUNOFF "{cache}"\n')
            expected = self.solve(root, 'literal', model.to_document().text + f'[FILES]\nUSE RUNOFF "{cache}"\n')
            bind(model, 'RUNOFF', 'USE', cache)
            actual = self.solve(root, 'rebuilt', portable(model).to_document().text)
            self.assertEqual(expected, actual)
            self.assertGreater(max(produced['series'][0][0][4]), 0)
            inspected = inspect_interface(cache.read_bytes(), 'RUNOFF', model)
            self.assertTrue(inspected.report.is_valid, inspected.report)
            self.assertEqual(inspected.status, 'state_checked')
            self.assertIn('files.positional_identity', {d.code for d in inspected.report.diagnostics})
            self.assertFalse(inspect_interface(cache.read_bytes()[:-1], 'RUNOFF', model).report.is_valid)

    def test_rainfall_native_station_header_and_cache_skips_original_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); raw = root/'rain.txt'; cache = root/'rain.bin'
            model = selected(hydrology_model())
            model.update_options(routing_step=timedelta(seconds=60))
            lines = []
            for hour in range(73):
                stamp = datetime(2020,1,30)+timedelta(hours=hour)
                lines.append(f'Station {stamp:%Y %m %d %H %M} {0.6 if 2 <= hour < 8 or 48 <= hour < 52 else 0}')
            raw.write_text('\n'.join(lines)+'\n', encoding='utf-8')
            model.raingages.update('R', source=FileRainfall(file=FileReference(path=str(raw)), station='Station', units='IN'))
            produced = self.solve(root, 'producer', model.to_document().text + f'[FILES]\nSAVE RAINFALL "{cache}"\n')
            raw.unlink()
            expected = self.solve(root, 'literal', model.to_document().text + f'[FILES]\nUSE RAINFALL "{cache}"\n')
            bind(model, 'RAINFALL', 'USE', cache)
            self.assertEqual(expected, self.solve(root, 'rebuilt', portable(model).to_document().text))
            self.assertEqual(expected['series'], produced['series'])
            preflight = check_files(model)
            self.assertTrue(preflight.complete, preflight.report)
            inspection = next(c.inspection for c in preflight.checks if c.inspection)
            self.assertEqual(dict(inspection.facts)['stations'], ('Station',))
            self.assertFalse(inspect_interface(cache.read_bytes()[:30], 'RAINFALL', model).report.is_valid)

    def test_native_outflows_read_edit_write_and_inflows_consume_same_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); raw = root/'out.ifc'; rewritten = root/'rewritten.ifc'
            producer = selected(network()); bind(producer, 'OUTFLOWS', 'SAVE', raw)
            self.solve(root, 'producer', portable(producer).to_document().text)
            document = RoutingInterface.read(raw)
            self.assertEqual(document.nodes, ('O',))
            self.assertEqual(document.to_bytes(), raw.read_bytes())
            replace(document, title='Canonical routing interface').write(rewritten)
            consumer = selected(network())
            consumer.nodes.rename('O', 'End'); consumer.nodes.rename('J', 'O')
            expected = self.solve(root, 'literal', consumer.to_document().text + f'[FILES]\nUSE INFLOWS "{raw}"\n')
            bind(consumer, 'INFLOWS', 'USE', rewritten)
            self.assertTrue(check_files(consumer).complete)
            self.assertEqual(expected, self.solve(root, 'rebuilt', portable(consumer).to_document().text))

    def test_rdii_text_uses_process_directory_and_can_be_explicitly_relocated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); work = root/'work'; project = root/'project'
            work.mkdir(); project.mkdir()
            routing().write(work/'rdii.txt')
            (project/'rdii.txt').write_text('Not an interface\n', encoding='utf-8')
            model = selected(network())
            inp = project/'model.inp'
            inp.write_text(model.to_document().text + '[FILES]\nUSE RDII rdii.txt\n', encoding='utf-8')
            # Isolated process proves the C engine's path base without changing
            # this test process's global working directory or native lifecycle.
            code = '''import os,sys
from pathlib import Path
package = Path(sys.argv.pop(1)).resolve()
sys.path.insert(0, str(package))
import easysewer
assert Path(easysewer.__file__).resolve().is_relative_to(package)
from easysewer.runtime._solver_api import SWMMSolverAPI
s=SWMMSolverAPI()
assert s.get_version()==52004
started=False
try:
    error=s.open(*sys.argv[1:])
    if not error:
        error=s.start(1); started=not error
finally:
    if started:s.end()
    s.close()
sys.exit(error)
'''
            env = dict(os.environ, PYTHONPATH=str(Path(easysewer.__file__).resolve().parent.parent), PYTHONDONTWRITEBYTECODE='1')
            run = subprocess.run([sys.executable,'-B','-c',code,str(Path(easysewer.__file__).resolve().parent.parent),str(inp),str(project/'model.rpt'),str(project/'model.out')],
                                 cwd=work, env=env, capture_output=True, text=True, timeout=30)
            details = run.stderr
            if (project/'model.rpt').exists():
                details += (project/'model.rpt').read_text(errors='replace')
            self.assertEqual(run.returncode, 0, details)
            model = Model.from_inp(inp, strict=True)
            self.assertTrue(check_files(model, working_directory=str(work)).complete)
            relocated = model.resolve_files(working_directory=str(work))
            relocated.to_inp(root/'relocated.inp')
            expected = self.solve(root, 'literal', selected(network()).to_document().text + f'[FILES]\nUSE RDII "{work / "rdii.txt"}"\n')
            self.assertEqual(expected, self.solve(root, 'rebuilt', portable(relocated).to_document().text))

    def test_rdii_binary_cache_checks_indices_before_native_reuse(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); cache = root/'rdii.bin'
            base = selected(hydrology_model()); base.update_options(routing_step=timedelta(seconds=60))
            # Unit hydrographs and nodal RDII now have structured codecs.
            source = base.to_document().text + '[HYDROGRAPHS]\nUH R\nUH ALL SHORT .15 1 2 0 0 0\n[RDII]\nJ UH 2\n'
            self.solve(root, 'producer', source + f'[FILES]\nSAVE RDII "{cache}"\n')
            literal = source + f'[FILES]\nUSE RDII "{cache}"\n'
            model = Model.from_document(InpDocument.from_text(literal), strict=True)
            # Preserve the original source as well as the structured records.
            model = Model.from_json_document(model.to_json_document())
            self.assertEqual(self.solve(root, 'literal', literal), self.solve(root, 'rebuilt', model.to_document(normalize=True).text))
            data = cache.read_bytes(); inspected = inspect_interface(data, 'RDII', model)
            self.assertTrue(inspected.report.is_valid, inspected.report)
            self.assertEqual(inspected.status, 'state_checked')
            bad = bytearray(data); struct.pack_into('<i', bad, len(b'SWMM5-RDII')+8, 999)
            self.assertFalse(inspect_interface(bad, 'RDII', model).report.is_valid)

    def test_bundled_hotstart_quality_payload_matches_reader_byte_layout(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for pollutants in (1,2):
                with self.subTest(pollutants=pollutants):
                    model=selected(hydrology_model());model.update_options(routing_step=timedelta(seconds=60))
                    rows=''.join(f'Q{i} MG/L 0 0 0 0 NO * 0 0 0\n' for i in range(pollutants))
                    source=model.to_document().text+'[POLLUTANTS]\n'+rows+'[LANDUSES]\nLand 0 0 0\n[COVERAGES]\nS Land 100\n'
                    state=root/f'quality-{pollutants}.hsf'
                    self.solve(root,'producer',source+f'[FILES]\nSAVE HOTSTART "{state}"\n')
                    data=state.read_bytes()
                    self.assertEqual(struct.unpack_from('<6i',data,len(b'SWMM5-HOTSTART4')),(1,1,2,1,pollutants,0))
                    # Independent fixed 5.2.4 readRunoff/readRouting layout:
                    # one ordinary catchment, one land use, one storage node,
                    # one outfall and one link; no snow or groundwater state.
                    expected=len(b'SWMM5-HOTSTART4')+6*4
                    expected+=(4+6+2*pollutants+pollutants+1)*8
                    expected+=(2*(2+pollutants)+1+(3+pollutants))*4
                    # The official tag's saveRunoff source writes too many
                    # buildup doubles for np > 1. The bundled binary does not
                    # reproduce that size defect; version alone is not provenance.
                    self.assertEqual(len(data),expected)
                    parsed=Model.from_document(InpDocument.from_text(source),strict=True)
                    self.assertEqual(inspect_interface(data,'HOTSTART',parsed).status,'state_checked')
                    # Typed quality now establishes layout, but still not origin.


if __name__ == '__main__':
    unittest.main()
