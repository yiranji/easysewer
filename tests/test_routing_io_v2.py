"""Routing format validity and explicit execution capability requirements."""

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from easysewer.io.interface_inspection import InterfaceInspection, inspect_interface
from easysewer.model import FileReference
from easysewer.model.identity import namespace_key
from easysewer.runtime import check_files, RunConfig
from test_files_v2 import routing, bind
from test_options_v2 import network


class RoutingExecutionRequirementsTests(unittest.TestCase):
    def test_format_validity_is_separate_from_backend_proof(self):
        model=network();data=routing(quality=True).to_bytes()
        inspected=inspect_interface(data,'INFLOWS',model)
        self.assertTrue(inspected.report.is_valid)
        self.assertEqual(inspected.status,'validated')
        self.assertEqual(inspected.required_capabilities,('easysewer:routing-io:1',))
        with tempfile.TemporaryDirectory() as directory:
            file=Path(directory)/'routing.ifc';file.write_bytes(data)
            bind(model,'INFLOWS','USE',file)
            with patch('ctypes.CDLL',side_effect=AssertionError('Preflight must not load native code')):
                pending=check_files(model)
                self.assertTrue(pending.report.is_valid)
                self.assertEqual(pending.checks[0].inspection.status,'pending')
                self.assertFalse(pending.complete)
                missing=check_files(model,backend_capabilities=())
                self.assertFalse(missing.report.is_valid)
                self.assertEqual(missing.checks[0].inspection.status,'unsupported')
                supported=check_files(model,backend_capabilities=('easysewer:routing-io:1',))
                self.assertTrue(supported.report.is_valid)
                self.assertEqual(supported.checks[0].inspection.status,'validated')
                self.assertIn('files.unmatched_interface_pollutant',{d.code for d in supported.report.diagnostics})

    def test_custom_inspectors_share_the_requirement_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            file=Path(directory)/'routing.ifc';file.write_bytes(b'plugin bytes')
            model=network();bind(model,'INFLOWS','USE',file)
            use=model.file_uses()[0]
            inspector=lambda *a,**kw:InterfaceInspection(format=use.format,status='validated',
                required_capabilities=('plugin:reader:2',))
            for capabilities,status in ((None,'pending'),((),'unsupported'),(('plugin:reader:2',),'validated')):
                result=check_files(model,inspectors={use.format:inspector},backend_capabilities=capabilities)
                self.assertEqual(result.checks[0].inspection.status,status)
            with self.assertRaises(TypeError):check_files(model,backend_capabilities='plugin:reader:2')

    def test_versioned_capabilities_roundtrip_without_relaxing_collection_ids(self):
        config=RunConfig(output_directory=FileReference(path='out',direction='output'),
            required_capabilities=('easysewer:routing-io:1','runtime:cancellation'))
        restored=RunConfig.from_json_document(config.to_json_document())
        self.assertEqual(restored.required_capabilities,config.required_capabilities)
        self.assertEqual(restored.to_json_document().data,config.to_json_document().data)
        self.assertTrue(config.validate_support(backends=('swmm:standard',),capabilities=config.required_capabilities).is_valid)
        self.assertFalse(config.validate_support(backends=('swmm:standard',),capabilities=('easysewer:routing-io:2',)).is_valid)
        with self.assertRaises(ValueError):namespace_key('swmm:nodes:1')
        for value in ('plain','Name:reader:1','x:reader:','x:reader:1 2','x::1'):
            with self.subTest(value=value),self.assertRaises(ValueError):replace(config,required_capabilities=(value,))
        with self.assertRaises(TypeError):InterfaceInspection(format='x',status='validated',required_capabilities=['x:reader'])
        with self.assertRaises(ValueError):InterfaceInspection(format='x',status='validated',required_capabilities=('x:reader','x:reader'))

    def test_native_consumption_range_does_not_reject_ignored_columns(self):
        model=network();document=routing(quality=True)
        frames=tuple(replace(frame,values=((.5,1e300,-1e300),)) for frame in document.frames)
        data=replace(document,frames=frames).to_bytes()
        self.assertTrue(inspect_interface(data,'INFLOWS',model).report.is_valid)
        huge=replace(document,frames=tuple(replace(frame,values=((1e300,0.,0.),)) for frame in document.frames))
        self.assertFalse(inspect_interface(huge.to_bytes(),'INFLOWS',model).report.is_valid)
        self.assertTrue(inspect_interface(replace(huge,nodes=('Foreign',)).to_bytes(),'INFLOWS',model).report.is_valid)
