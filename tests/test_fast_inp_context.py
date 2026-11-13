"""Lightweight INP context survives files, execution preparation and JSON."""

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from easysewer.io.inp import InpDocument
from easysewer.io.json import JsonDocument
from easysewer.model import FileReference, Model, Ref
from easysewer.model.resources import FileTimeSeries
from easysewer.runtime import RunConfig, Runner, check_files
from easysewer.runtime.backend import BackendInfo
from easysewer.runtime._preparation import inventory
from easysewer.runtime._workspace import OutputTransaction
from easysewer.validation import ValidationError
from test_options_v2 import network


class FastInputContextTests(unittest.TestCase):
    def load(self, root, suffix=""):
        source = root / "original.inp"
        source.write_text(network().to_document().text + suffix, encoding="utf-8")
        return Model.from_inp(source), source

    def test_path_copy_and_json_do_not_invent_original_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            model, source = self.load(Path(directory))
            for restored in (model, model.copy(), Model.from_json_document(model.to_json_document())):
                self.assertEqual(restored.source_path, str(source.resolve()))
                self.assertIsNone(restored.document)
                self.assertEqual(restored.provenance(Ref(collection="swmm:nodes", key="J")).kind, "untracked")
                self.assertEqual(tuple(restored.nodes.values()), tuple(model.nodes.values()))
            wire = model.to_json_document().data
            self.assertNotIn("source", wire)
            self.assertEqual(wire["input_context"]["format"], "easysewer:inp-context")
            self.assertEqual(wire["input_context"]["passthrough"], [])
            self.assertIsNone(Model().source_path)
            checked = Model.from_document(InpDocument.read(source))
            self.assertEqual(checked.source_path, str(source.resolve()))

    def test_file_resolution_uses_input_directory_without_rich_document(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model, source = self.load(root)
            target = root / "series data.dat"
            target.write_text("01/01/2020 00:00 1\n", encoding="utf-8")
            model.timeseries.add(FileTimeSeries(id="Flow", file=FileReference(path=target.name)))
            resolved = model.resolve_files()
            self.assertEqual(Path(resolved.timeseries["Flow"].file.path), target.resolve())
            self.assertEqual(model.timeseries["Flow"].file.path, target.name)
            checks = check_files(model, inspect_data=False)
            self.assertEqual(len(checks.checks), 1)
            self.assertEqual(Path(checks.checks[0].resolved_path), target.resolve())
            self.assertTrue(checks.checks[0].available)

    def test_runner_protects_original_input_and_infers_its_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model, source = self.load(root)
            original = source.read_bytes()
            info = BackendInfo(key="swmm:standard", available=True, reason=None,
                library=None, sha256=None, engine_version=52004, platform="fixture",
                architecture="fixture", abi="fixture", profiles=(model.profile.key,),
                capabilities=(), isolation="fixture")
            backend = SimpleNamespace(probe=lambda **kwargs: info)
            observed = {}

            def inspect_inventory(captured, **kwargs):
                observed["input_directory"] = kwargs["input_directory"]
                return inventory(captured, **kwargs)

            def transaction(*args, **kwargs):
                observed["protected"] = kwargs["protected"]
                return OutputTransaction(*args, **kwargs)

            config = RunConfig(output_directory=FileReference(path=str(root), direction="output"),
                artifact_stem="original", overwrite=True)
            with patch("easysewer.runtime.runner.inventory", side_effect=inspect_inventory), \
                    patch("easysewer.runtime.runner.OutputTransaction", side_effect=transaction):
                result = Runner(backends={"swmm:standard": backend}).run(model, config)
            self.assertEqual(observed["input_directory"], root.resolve())
            self.assertIn(source.resolve(), observed["protected"])
            self.assertFalse(result.native_completed)
            self.assertNotEqual(result.status, "succeeded")
            self.assertEqual(source.read_bytes(), original)
            self.assertFalse((root / "original.out").exists())

    def test_json_preserves_passthrough_and_its_identity_guard(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model, source = self.load(root, "[FILES]\nNO RUNOFF\n[FUTURE]\nJ custom-value\n")
            wire = model.to_json_document()
            restored = Model.from_json_document(wire)
            self.assertEqual(restored._input_context, model._input_context)
            self.assertEqual(restored.to_json_document().data, wire.data)
            output = root / "export.inp"
            restored.to_inp(output)
            text = output.read_text(encoding="utf-8")
            self.assertIn("NO RUNOFF", text)
            self.assertIn("J custom-value", text)
            with self.assertRaises(ValidationError):
                restored.nodes.rename("J", "New")

    def test_unknown_input_context_is_preserved_and_blocks_parameter_export(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model, source = self.load(root)
            for changed in ({"schema_version": "2.0"}, {"future": {"retain": True}}):
                with self.subTest(changed=changed):
                    data = deepcopy(model.to_json_document().data)
                    data["input_context"].update(changed)
                    restored = Model.from_json_document(JsonDocument.from_data(data))
                    self.assertIsNone(restored.document)
                    self.assertEqual(restored.to_json_document().data, data)
                    with self.assertRaises(ValidationError):
                        restored.to_inp(root / "blocked.inp")
                    self.assertFalse((root / "blocked.inp").exists())

    def test_unknown_json_content_cannot_bypass_guards_through_known_context(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model, source = self.load(root)
            data = deepcopy(model.to_json_document().data)
            data["future"] = {"unknown_parameters": True}
            restored = Model.from_json_document(JsonDocument.from_data(data))
            self.assertIsNotNone(restored._input_context)
            with self.assertRaises(ValidationError):
                restored.to_document()
            with self.assertRaises(ValidationError):
                restored.to_inp(root / "blocked.inp")

    def test_invalid_passthrough_wire_cannot_introduce_physical_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            model, source = self.load(Path(directory), "[FUTURE]\nJ custom-value\n")
            data = deepcopy(model.to_json_document().data)
            data["input_context"]["passthrough"][0]["content"] = "J value\n[OPTIONS]"
            with self.assertRaises(ValidationError) as caught:
                Model.from_json_document(JsonDocument.from_data(data))
            self.assertEqual(caught.exception.report.errors[0].code, "json.input_context")

    def test_large_passthrough_context_obeys_json_cancellation(self):
        from easysewer.io.json import input_context
        from easysewer.validation._cooperative import checkpoint_scope

        with tempfile.TemporaryDirectory() as directory:
            model, source = self.load(Path(directory), "[FUTURE]\n" +
                "".join(f"J retained-{index}\n" for index in range(600)))
            # Unanchored trailing rows isolate the row-batch polling from the
            # separate checkpoints used while copying an anchor's key.
            model._input_context = replace(model._input_context, passthrough=tuple(
                replace(row, after=None, before=None) for row in model._input_context.passthrough))
            before = model.to_json_document()
            context = model._input_context
            for operation in ("write_context", "read_context"):
                with self.subTest(operation=operation):
                    entered = False
                    calls = 0
                    error = ValueError("cancel input context")
                    implementation = getattr(input_context, operation)

                    def observed(value):
                        nonlocal entered
                        entered = True
                        try:
                            return implementation(value)
                        finally:
                            entered = False

                    def checkpoint():
                        nonlocal calls
                        if entered:
                            calls += 1
                            if calls == 2:
                                raise error

                    with patch.object(input_context, operation, side_effect=observed):
                        with self.assertRaises(ValueError) as caught:
                            if operation == "write_context":
                                model.to_json_document(checkpoint=checkpoint)
                            else:
                                with checkpoint_scope(checkpoint):
                                    Model.from_json_document(before)
                    self.assertIs(caught.exception, error)
                    self.assertEqual(calls, 2)
                    self.assertIs(model._input_context, context)
                    self.assertEqual(model.to_json_document().to_bytes(), before.to_bytes())


if __name__ == "__main__":
    unittest.main()
