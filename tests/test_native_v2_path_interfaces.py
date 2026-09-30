"""Preserve each file owner's path base and verify generated cache bytes."""
import re
import unittest

from test_native_v2_path_resources import ResourceChecks
from test_native_v2_path_io import PathCase
from test_native_v2_checkpoint_streams import rdii
from test_native_v2_checkpoint_writes import fixture
from test_native_v2_lid_report_io import fixture as lid_fixture


class InterfaceChecks(PathCase):
    sized = ResourceChecks.sized

    def test_rdii_relative_names_preserve_working_directory_base(self):
        for kind in ('save', 'binary', 'text'):
            with self.subTest(kind=kind):
                folder = self.root/kind
                folder.mkdir()
                body = rdii(folder, kind)
                path = folder/'rdii.bin'
                code, expected, _ = self.run_model(body)
                self.assertEqual(code, 0)
                data = path.read_bytes()
                relative = str(path.relative_to(self.root))
                decoy = self.case/relative
                decoy.parent.mkdir(parents=True)
                decoy.write_bytes(b'wrong-directory-guard')
                if kind == 'save':
                    path.unlink()
                code, actual, _ = self.run_model(body.replace(str(path), relative))
                self.assertEqual((code, actual), (0, expected))
                self.assertEqual(path.read_bytes(), data)
                self.assertEqual(decoy.read_bytes(), b'wrong-directory-guard')

    def test_lid_relative_report_preserves_working_directory_base(self):
        path = self.root/'lid.txt'
        code, expected, _ = self.run_model(lid_fixture(detail=str(path)))
        self.assertEqual(code, 0)
        data = path.read_bytes()
        path.unlink()
        decoy = self.case/path.name
        decoy.write_bytes(b'wrong-directory-guard')
        code, actual, _ = self.run_model(lid_fixture(detail=path.name))
        self.assertEqual((code, actual), (0, expected))
        self.assertEqual(path.read_bytes(), data)
        self.assertEqual(decoy.read_bytes(), b'wrong-directory-guard')

    def test_generated_interfaces_use_document_directory_and_long_paths(self):
        folder = self.case/'short'
        folder.mkdir()
        body = fixture(folder, 'all')
        code, expected, _ = self.run_model(body)
        self.assertEqual(code, 0)
        names = ('saved-runoff.bin', 'saved-outflows.txt', 'saved-hotstart.bin')
        saved = {name: (folder/name).read_bytes() for name in names}
        targets = {name: self.sized(600, name) for name in names}
        moved = body
        for name, target in targets.items():
            relative = target.relative_to(self.case)
            moved = moved.replace(str(folder/name), str(relative))
            decoy = self.root/relative
            decoy.parent.mkdir(parents=True, exist_ok=True)
            decoy.write_bytes(b'wrong-directory-guard')
        code, actual, _ = self.run_model(moved)
        self.assertEqual((code, actual), (0, expected))
        for name, target in targets.items():
            self.assertEqual(target.read_bytes(), saved[name], name)
            self.assertEqual((self.root/target.relative_to(self.case)).read_bytes(),
                             b'wrong-directory-guard')
        # Replaying a hotstart is a different initial state. Compare identical
        # input bytes through short absolute and long relative names.
        consumer = re.sub(r'(?m)^SAVE (?:RUNOFF|OUTFLOWS|HOTSTART) .*\n', '', body)
        for kind, name in (('RUNOFF', names[0]), ('HOTSTART', names[2])):
            short = consumer + f'[FILES]\nUSE {kind} "{folder/name}"\n'
            long = consumer + f'[FILES]\nUSE {kind} "{targets[name].relative_to(self.case)}"\n'
            code, reference, _ = self.run_model(short)
            self.assertEqual(code, 0)
            code, actual, _ = self.run_model(long)
            self.assertEqual((code, actual), (0, reference), kind)
            self.assertEqual((folder/name).read_bytes(), saved[name])
            self.assertEqual(targets[name].read_bytes(), saved[name])


class StandardInterfaces(InterfaceChecks, unittest.TestCase):
    family = 'standard'


class CustomInterfaces(InterfaceChecks, unittest.TestCase):
    family = 'custom'
