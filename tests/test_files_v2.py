"""Interface bindings, external identity guards, path and preflight contracts."""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import struct
import unittest
from unittest.mock import patch

from easysewer.io.inp import InpDocument
from easysewer.io.routing import RoutingInterface, InterfaceConstituent, InterfaceFrame
from easysewer.model import Model, FileReference
from easysewer.model.files import InterfaceFile
from easysewer.model.hydrology import FileRainfall
from easysewer.model.climate import ClimateFile
from easysewer.model.resources import FileTimeSeries
from easysewer.runtime import check_files
from easysewer.validation import ValidationError
from easysewer.io.interface_inspection import InterfaceInspection, inspect_interface
from test_hydrology_v2 import hydrology_model
from test_options_v2 import network
from test_scenario_v2 import portable


def bind(model, kind, mode, path, **kwargs):
    value = InterfaceFile(kind=kind, mode=mode, file=FileReference(path=str(path), direction='input' if mode == 'USE' else 'output', **kwargs))
    model.files.add(value)
    return value


def routing(nodes=('J',), *, quality=False):
    constituents = (InterfaceConstituent(name='FLOW', units='CFS'),)
    if quality:
        constituents += (InterfaceConstituent(name='TSS', units='MG/L'), InterfaceConstituent(name='Lead', units='UG/L'))
    return RoutingInterface(step=timedelta(seconds=60), nodes=nodes, constituents=constituents, title='Interface example',
        frames=tuple(InterfaceFrame(time=datetime(2020,1,1)+timedelta(minutes=i), values=tuple(tuple((i+1)*(j+1)*.1 for j in range(len(constituents))) for _ in nodes)) for i in range(11)))


class FilesTests(unittest.TestCase):
    def test_all_documented_modes_source_json_and_model_roundtrip(self):
        for kind in ('RAINFALL','RUNOFF','HOTSTART','RDII','INFLOWS','OUTFLOWS'):
            for mode in (('USE',) if kind == 'INFLOWS' else ('SAVE',) if kind == 'OUTFLOWS' else ('USE','SAVE')):
                with self.subTest(kind=kind,mode=mode):
                    m = network(); bind(m,kind,mode,'a file.dat')
                    restored = Model.from_document(m.to_document(),strict=True)
                    self.assertEqual(tuple(restored.files.values()),tuple(m.files.values()))
                    self.assertEqual(tuple(portable(restored).files.values()),tuple(m.files.values()))
                    self.assertEqual(restored.to_document().to_bytes(),m.to_document().to_bytes())

    def test_last_assignment_and_hotstart_dual_slots(self):
        source='[FILES]\nUSE RUNOFF old.dat\n[FILES]\nSAVE RUNOFF new.dat extra\nUSE HOTSTART in.hsf\nSAVE HOTSTART out.hsf\n'
        m=Model.from_document(InpDocument.from_text(source),strict=True)
        self.assertEqual(tuple(m.files),(('RUNOFF','SAVE'),('HOTSTART','USE'),('HOTSTART','SAVE')))
        self.assertEqual(m.to_document().text,source)
        self.assertEqual(len(m.to_document(normalize=True).records('FILES')),3)
        m.files.remove(('HOTSTART','USE'))
        self.assertNotIn('in.hsf',m.to_document().text)
        self.assertEqual(tuple(portable(m).files),tuple(m.files))

    def test_invalid_modes_opaque_assignments_and_missing_file_noop(self):
        for row in ('SAVE INFLOWS x','USE OUTFLOWS x','NO INFLOWS x','SCRATCH OUTFLOWS x','USE','USE RUNOFF "bad'):
            m=Model.from_document(InpDocument.from_text('[FILES]\n'+row+'\n'))
            self.assertFalse(m.validate().is_valid)
            self.assertEqual(m.document.records('FILES')[0].content,row)
        source='[FILES]\nUSE RUNOFF x\nNO RUNOFF unused\nUSE HOTSTART\nFUTURE OTHER x\n'
        m=Model.from_document(InpDocument.from_text(source),strict=True)
        self.assertFalse(m.files)
        self.assertEqual(m.to_document().text,source)
        self.assertTrue(m.support.opaque_records)
        m=Model();bind(m,'RUNOFF','USE','a');bind(m,'RUNOFF','SAVE','b')
        self.assertIn('files.conflicting_modes',{d.code for d in m.validate().errors})

    def test_runoff_reports_replay_and_partial_hotstart_state(self):
        m=hydrology_model();bind(m,'RUNOFF','USE','runoff.bin')
        codes={d.code for d in m.validate(for_run=True).diagnostics}
        self.assertIn('files.runoff_replay_scope',codes)
        self.assertNotIn('files.runoff_hotstart_partial',codes)
        bind(m,'HOTSTART','SAVE','state.hsf')
        self.assertIn('files.runoff_hotstart_partial',{d.code for d in m.validate(for_run=True).diagnostics})
        m.files.remove(('RUNOFF','USE'))
        self.assertNotIn('files.runoff_replay_scope',{d.code for d in m.validate(for_run=True).diagnostics})

    def test_external_guards_survive_copies_json_and_rollback(self):
        m=network();bind(m,'HOTSTART','USE','state.hsf')
        for current in (m,m.copy(),portable(m)):
            for action in (lambda:current.nodes.rename('J','Moved'),lambda:current.nodes.move('O',before='J'),
                           lambda:current.nodes.add(replace(current.nodes['J'],id='Extra')),
                           lambda:current.links.remove('P'),lambda:current.convert_units('CMS'),lambda:current.reinterpret_units('CMS')):
                before=current.to_json_document().to_bytes()
                with self.assertRaises(ValidationError):action()
                self.assertEqual(current.to_json_document().to_bytes(),before)
            current.links.update('P',roughness=.014)
            current.files.remove(('HOTSTART','USE'))
            current.nodes.rename('J','Moved')
        saved=network();bind(saved,'HOTSTART','SAVE','state.hsf');saved.nodes.rename('J','Moved')
        self.assertEqual(saved.links['P'].inlet.key,'Moved')

    def test_consumer_inventory_distinguishes_active_optional_and_working_paths(self):
        m=hydrology_model()
        m.raingages.update('R',source=FileRainfall(file=FileReference(path='rain.txt'),station='Station',units='IN'))
        m.timeseries.add(FileTimeSeries(id='Boundary',file=FileReference(path='series.txt')))
        m.update_climate(file=ClimateFile(file=FileReference(path='weather.txt')))
        m.update_backdrop(file=FileReference(path='image.png'))
        m.update_options(temp_directory=FileReference(path='scratch',direction='output'))
        bind(m,'RAINFALL','USE','rain.ifc');bind(m,'RDII','USE','rdii.ifc')
        uses={u.role:u for u in m.file_uses()}
        self.assertEqual(len(uses),7)
        self.assertFalse(uses['swmm:rainfall'].active)
        self.assertFalse(uses['swmm:backdrop'].required)
        self.assertEqual(uses['swmm:interface.rdii'].base,'working_directory')
        self.assertEqual(uses['swmm:interface.rainfall'].access,'read')
        self.assertNotIn('core:undeclared',uses)
        bind(m,'HOTSTART','USE','state.hsf');bind(m,'RUNOFF','USE','runoff.ifc')
        uses={u.role:u for u in m.file_uses()}
        self.assertEqual(uses['swmm:interface.hotstart'].access,'read')
        self.assertEqual(uses['swmm:interface.runoff'].access,'read')

    def test_rdii_relative_origin_and_explicit_resolution_are_not_inp_rebasing(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            m=Model.from_document(InpDocument.from_text('[FILES]\nUSE RDII data.ifc\n',source=str(root/'project'/'a.inp')),strict=True)
            self.assertIsNone(m.files[('RDII','USE')].file.base_directory)
            with self.assertRaises(ValidationError):m.to_inp(root/'elsewhere'/'b.inp')
            bound=m.resolve_files(working_directory=str(root/'work'))
            bound.to_inp(root/'elsewhere'/'b.inp')
            reread=Model.from_inp(root/'elsewhere'/'b.inp',strict=True)
            self.assertEqual(reread.files[('RDII','USE')].file.resolve(),root/'work'/'data.ifc')
            self.assertIsNone(m.files[('RDII','USE')].file.base_directory)

    def test_ordinary_file_rebase_with_attached_reuse_guards(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            m=hydrology_model();m.raingages.update('R',source=FileRainfall(file=FileReference(path='rain.txt',base_directory=str(root)),station='Station',units='IN'))
            bind(m,'RAINFALL','USE','cache.ifc',base_directory=str(root))
            m.to_inp(root/'new'/'model.inp')
            n=Model.from_inp(root/'new'/'model.inp',strict=True)
            self.assertEqual(n.raingages['R'].source.file.resolve(),root/'rain.txt')
            self.assertEqual(n.files[('RAINFALL','USE')].file.resolve(),root/'cache.ifc')

    def test_read_only_preflight_missing_collisions_and_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);file=root/'in.ifc';routing().write(file)
            m=network();bind(m,'INFLOWS','USE',file)
            original=file.read_bytes()
            result=check_files(m)
            self.assertTrue(result.report.is_valid)
            self.assertTrue(result.complete)
            self.assertEqual(result.checks[0].inspection.status,'validated')
            self.assertEqual(file.read_bytes(),original)
            limited=check_files(m,max_bytes=10)
            self.assertTrue(limited.report.is_valid);self.assertFalse(limited.complete)
            bind(m,'OUTFLOWS','SAVE',file)
            self.assertIn('files.path_collision',{d.code for d in check_files(m,overwrite=True).report.errors})
            m.files.remove(('OUTFLOWS','SAVE'))
            m.files.update(('INFLOWS','USE'),file=FileReference(path=str(root/'missing.ifc')))
            self.assertFalse(check_files(m).report.is_valid)
            m.files.remove(('INFLOWS','USE'));bind(m,'OUTFLOWS','SAVE',root/'new'/'out.ifc')
            self.assertTrue(check_files(m).report.is_valid)
            self.assertFalse((root/'new').exists())

    def test_preflight_protects_inactive_inputs_and_existing_file_aliases(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); raw=root/'rain.txt'; raw.write_bytes(b'original')
            m=hydrology_model();m.update_options(ignore_rainfall=True)
            m.raingages.update('R',source=FileRainfall(file=FileReference(path=str(raw)),station='Station',units='IN'))
            bind(m,'HOTSTART','SAVE',raw)
            self.assertIn('files.path_collision',{d.code for d in check_files(m,overwrite=True).report.errors})
            self.assertEqual(raw.read_bytes(),b'original')
            m.files.remove(('HOTSTART','SAVE'));m.update_options(ignore_rainfall=False)
            alias=root/'alias.bin'
            try:alias.hardlink_to(raw)
            except OSError as error:self.skipTest(f'Hardlinks unavailable: {error}')
            bind(m,'HOTSTART','SAVE',alias)
            self.assertIn('files.path_collision',{d.code for d in check_files(m,overwrite=True).report.errors})

    def test_extension_consumers_inspectors_and_mutation_guards(self):
        from easysewer.io.inp.network import default_schema
        from easysewer.model import CollectionSpec, Ref
        from easysewer.model.file_resources import FileUse
        from easysewer.schema import DecodedFeature, FeatureDescriptor, RegistryError
        from easysewer.schema.structured import FeatureData
        from easysewer.validation import Diagnostic
        @dataclass(frozen=True,kw_only=True)
        class Data:
            id: str
            file: FileReference
        def guard(store, action, targets, **kwargs):
            if action=='rename' and any(r.collection=='test:data' for r in targets):
                yield Diagnostic(code='test:external_id',message='External manifest contains this ID')
        class Codec:
            collections=(CollectionSpec(key='test:data',record_type=Data,key_of=lambda v:v.id,identity_field='id'),)
            mutation_guards=(guard,)
            def decode(self,document,profile):return DecodedFeature(value=FeatureData())
            def encode(self,store,profile):return ()
            def validate(self,store,profile):return ()
        class Declared(Codec):
            def file_uses(self,store,profile):
                for row in store.collection('test:data').values():
                    yield FileUse(owner=Ref(collection='test:data',key=row.id),path=('file',),file=row.file,
                                  role='test:reader',format='test:data')
        class Directory(Declared):
            def file_uses(self,store,profile):
                for use in super().file_uses(store,profile):yield replace(use,kind='directory')
        class Broken(Declared):
            def file_uses(self,store,profile):
                for use in super().file_uses(store,profile):yield replace(use,path=('missing',))
        def model(codec,file):
            schema=default_schema();schema.register(FeatureDescriptor(key='test:data',sections={'DATA'}),codec)
            m=Model(schema=schema);m.collection('test:data').add(Data(id='R',file=FileReference(path=str(file))))
            return m
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);path=root/'data.txt';path.write_bytes(b'custom format')
            m=model(Declared(),path)
            self.assertFalse(check_files(m).complete)
            self.assertTrue(check_files(m,inspectors={'test:data':lambda data,**kw:InterfaceInspection(format=kw['use'].format,
                status='validated' if data==b'custom format' else 'invalid')}).complete)
            self.assertEqual(model(Codec(),path).file_uses()[0].role,'core:undeclared')
            with self.assertRaises(RegistryError):model(Broken(),path).file_uses()
            self.assertFalse(check_files(model(Directory(),root)).complete)
            self.assertIn('files.directory_adapter_missing',{d.code for d in check_files(model(Directory(),root)).report.diagnostics})
            self.assertFalse(check_files(model(Directory(),root/'missing')).report.is_valid)
            clone=m.copy()
            with self.assertRaises(ValidationError):clone.collection('test:data').rename('R','Changed')
            self.assertEqual(tuple(clone.collection('test:data')),('R',))

    def test_binary_and_quality_inspection_boundaries_are_explicit(self):
        m=network()
        for kind, data in (('HOTSTART',b'SWMM5-HOTSTART4'),('RUNOFF',b'SWMM5-RUNOFF'),
                           ('RAINFALL',b'SWMM5-RAIN'),('RDII',b'SWMM5-RDII')):
            with self.subTest(kind=kind):
                self.assertFalse(inspect_interface(data,kind,m).report.is_valid)
        inspected=inspect_interface(routing(quality=True).to_bytes(),'INFLOWS',m)
        self.assertTrue(inspected.report.is_valid)
        self.assertEqual(inspected.required_capabilities,('easysewer:routing-io:1',))
        bad=b'SWMM5-RDII'+struct.pack('<iii',60,1,-1)+struct.pack('<df',43831.,.5)
        self.assertFalse(inspect_interface(bad,'RDII',m).report.is_valid)
        extra=routing(('Missing',)).to_bytes()
        self.assertFalse(inspect_interface(extra,'RDII',m).report.is_valid)
        self.assertIn('files.unmatched_interface_node',{d.code for d in inspect_interface(extra,'INFLOWS',m).report.diagnostics})


class RoutingInterfaceTests(unittest.TestCase):
    def test_case_insensitive_row_identity_preserves_original_bytes(self):
        raw=routing().to_bytes().replace(b'J 2020',b'j 2020')
        parsed=RoutingInterface.from_bytes(raw)
        self.assertEqual(parsed,routing())
        self.assertEqual(parsed.to_bytes(),raw)

    def test_flow_two_pollutants_source_preservation_and_editing(self):
        model=routing(('J','Other'),quality=True)
        raw=model.to_bytes().replace(b'\n',b'\r\n')
        reread=RoutingInterface.from_bytes(raw)
        self.assertEqual(reread,model)
        self.assertEqual(reread.to_bytes(),raw)
        edited=replace(reread,title='Edited')
        self.assertEqual(RoutingInterface.from_bytes(edited.to_bytes()),edited)
        self.assertNotIn(b'\r\n',edited.to_bytes())

    def test_frame_order_labels_time_and_nonfinite_values_are_checked(self):
        source=routing(('J','K')).to_bytes()
        variants=(source.replace(b'J 2020',b'K 2020',1),source.replace(b'K 2020 01 01 00 00 00',b'K 2020 01 01 00 00 01',1),
                  source.replace(b'0.1',b'nan',1),b'not swmm\n',source[:-6],source.replace(b'FLOW CFS',b'FLOW UNKNOWN'))
        for data in variants:
            with self.assertRaises((ValidationError,ValueError)):RoutingInterface.from_bytes(data)
        with self.assertRaises(ValueError):replace(routing(),frames=tuple(reversed(routing().frames)))

    def test_atomic_writer_failure_leaves_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'routing.ifc';path.write_bytes(b'old')
            with patch('easysewer.io._atomic.os.replace',side_effect=OSError('injected')):
                with self.assertRaises(OSError):routing().write(path)
            self.assertEqual(path.read_bytes(),b'old')
            self.assertEqual(list(Path(directory).iterdir()),[path])

    def test_native_text_encoding_requires_ascii_compatible_bytes_without_bom(self):
        with self.assertRaises(ValueError):routing().to_bytes(encoding='utf-8-sig')
        with self.assertRaises(ValueError):routing().to_bytes(encoding='utf-16-le')
        with self.assertRaises(ValueError):RoutingInterface.from_bytes(b'\xef\xbb\xbf'+routing().to_bytes(),encoding='utf-8-sig')
        with self.assertRaises(ValueError):RoutingInterface.from_bytes(routing().to_bytes().decode().encode('utf-16-le'),encoding='utf-16-le')
        with self.assertRaises(TypeError):RoutingInterface.from_bytes(bytearray(routing().to_bytes()))
        with self.assertRaises(ValidationError):RoutingInterface.from_bytes(routing().to_bytes().replace(b'\n',b'\r'))
        special=replace(routing(),title='An embedded\vseparator')
        self.assertEqual(RoutingInterface.from_bytes(special.to_bytes()),special)
        with self.assertRaises(TypeError):replace(routing(),constituents=('FLOW',))


if __name__=='__main__':unittest.main()
