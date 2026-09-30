"""Scenario isolation, JSON fidelity and declarative runtime boundaries."""

from dataclasses import dataclass, replace
from datetime import timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from easysewer.io.json import JsonDocument
from easysewer.io.json.scenario import OperationSpec, default_registry, scenario_schema
from easysewer.io.json.run import run_schema
from easysewer.model import FileReference, Model, Ref
from easysewer.model.resources import InlineTimeSeries, SeriesPoint
from easysewer.runtime import ReportReadOptions, RunConfig
from easysewer.scenario import (
    AddRecord, AssertRecord, ChangeContext, FieldChange, MoveRecord, OpaqueOperation,
    RemoveRecord, RenameRecord, ReplaceRecord, ScenarioPatch, SetFields,
)
from easysewer.validation import ValidationError
from test_hydrology_v2 import hydrology_model


def target(collection, key):
    return Ref(collection="swmm:" + collection, key=key)


def fields(collection, key, **values):
    return SetFields(target=target(collection, key), changes=tuple(FieldChange(name=n, value=v) for n, v in values.items()))


def portable(model):
    data = model.to_json_document().data
    data.pop("source", None)
    return Model.from_json_document(JsonDocument.from_data(data), strict=True)


class ScenarioTests(unittest.TestCase):
    def test_all_operations_actual_changes_order_and_input_isolation(self):
        original = hydrology_model()
        before = original.to_json_document().to_bytes()
        rain = original.timeseries["Rain"]
        extra = InlineTimeSeries(id="Extra", points=(SeriesPoint(time=timedelta(0), value=0),))
        p = ScenarioPatch(flow_units="CFS", operations=(
            AssertRecord(target=target("timeseries", "Extra")), AddRecord(target=target("timeseries", "Extra"), value=extra),
            ReplaceRecord(target=target("timeseries", "Extra"), value=replace(extra, points=(SeriesPoint(time=timedelta(0), value=1),))),
            MoveRecord(target=target("timeseries", "Extra"), before="Rain"),
            RenameRecord(target=target("timeseries", "Rain"), new_id="Storm"),
            AssertRecord(target=target("timeseries", "Storm"), value=replace(rain, id="Storm")),
            fields("options", "settings", rule_step=timedelta(0), routing_step=timedelta(seconds=.5)),
            RemoveRecord(target=target("timeseries", "Extra")),
        ))
        parsed = ScenarioPatch.from_json_document(p.to_json_document())
        result = parsed.apply(original)
        self.assertEqual(original.to_json_document().to_bytes(), before)
        self.assertEqual(result.model.raingages["R"].source.series.key, "Storm")
        self.assertEqual(result.model.options.rule_step, timedelta(0))
        self.assertEqual(result.model.options.routing_step, timedelta(seconds=.5))
        self.assertTrue(any(c.target == target("raingages", "R") and c.operation == 4 for c in result.changes))
        self.assertTrue(any(c.before_index != c.after_index for c in result.changes))
        self.assertEqual(portable(result.model).raingages["R"], result.model.raingages["R"])

    def test_failure_at_end_and_precondition_failure_leave_input_byte_exact(self):
        m = hydrology_model()
        before = m.to_json_document().to_bytes()
        for op in (fields("raingages", "R", interval=timedelta(seconds=-1)),
                   AssertRecord(target=target("nodes", "J")), RemoveRecord(target=target("nodes", "J")),
                   fields("subcatchments", "S", outlet=target("nodes", "Missing"))):
            with self.subTest(operation=op), self.assertRaises((ValidationError, ValueError)):
                ScenarioPatch(operations=(fields("options", "settings", rule_step=timedelta(seconds=30)), op)).apply(m)
            self.assertEqual(m.to_json_document().to_bytes(), before)
        with self.assertRaises(ValidationError):
            ScenarioPatch(operations=(AssertRecord(target=target("timeseries", "Rain"), value=replace(m.timeseries["Rain"], points=())),)).apply(m)
        with self.assertRaises(ValidationError):
            ScenarioPatch(operations=(AddRecord(target=target("nodes", "Invalid"), value=1),)).apply(m)

    def test_graph_validation_waits_for_complete_patch(self):
        m = hydrology_model()
        series = InlineTimeSeries(id="Future", points=m.timeseries["Rain"].points)
        from easysewer.model.hydrology import SeriesRainfall
        p = ScenarioPatch(operations=(fields("raingages", "R", source=SeriesRainfall(series=target("timeseries", "Future"))),
                                     AddRecord(target=target("timeseries", "Future"), value=series)))
        self.assertEqual(p.apply(m).model.raingages["R"].source.series.key, "Future")

    def test_context_changes_and_numeric_preconditions(self):
        m = hydrology_model()
        with self.assertRaises(ValidationError):
            ScenarioPatch(flow_units="CMS").apply(m)
        with self.assertRaises(ValidationError):
            ScenarioPatch(profile="other:profile").apply(m)
        with self.assertRaises(ValidationError):
            ScenarioPatch(operations=(fields("options", "settings", flow_units="CMS"),)).apply(m)
        p = ScenarioPatch(flow_units="CFS", operations=(ChangeContext(context="flow_units", value="CMS"),))
        converted = ScenarioPatch.from_json_document(p.to_json_document()).apply(m).model
        expected = m.copy(); expected.convert_units("CMS")
        self.assertEqual(converted.to_json_document().data, expected.to_json_document().data)
        interpreted = ScenarioPatch(operations=(ChangeContext(context="flow_units", value="CMS", mode="reinterpret"),)).apply(m).model
        self.assertEqual(interpreted.timeseries["Rain"], m.timeseries["Rain"])
        for changes in ({"routing_step": True}, {"id": "other"}, {"nonsense": 4}):
            with self.assertRaises((ValidationError, ValueError)):
                ScenarioPatch(operations=(fields("options", "settings", **changes),)).apply(m)

    def test_unknown_operations_and_fields_preserved_and_execution_blocked(self):
        m = hydrology_model()
        base = ScenarioPatch(operations=(fields("options", "settings", rule_step=timedelta(0)),)).to_json_document().data
        variants = []
        for path in ("operation", "change", "duration", "root"):
            data = json.loads(json.dumps(base))
            obj = {"operation": data["operations"][0], "change": data["operations"][0]["changes"][0],
                   "duration": data["operations"][0]["changes"][0]["value"], "root": data}[path]
            obj["future"] = {"reference": "P"}
            variants.append(data)
        data = json.loads(json.dumps(base)); data["operations"][0]["type"] = "plugin:future"; variants.append(data)
        data = json.loads(json.dumps(base)); data["required_capabilities"] = ["plugin:needed"]; variants.append(data)
        for data in variants:
            document = JsonDocument.from_bytes(b"\xef\xbb\xbf" + json.dumps(data).encode())
            loaded = ScenarioPatch.from_json_document(document)
            self.assertEqual(loaded.to_json_document().to_bytes(), document.to_bytes())
            with self.assertRaises(ValidationError):
                loaded.apply(m)
        data = json.loads(json.dumps(base)); data["schema_version"] = "1.5"; data["extensions"] = {"user:labels": {"name": "雨"}}
        loaded = ScenarioPatch.from_json_document(JsonDocument.from_data(data))
        self.assertIn("scenario.newer_minor", {d.code for d in loaded.apply(m).report.diagnostics})
        data["schema_version"] = "2.0"
        with self.assertRaises(ValidationError):
            ScenarioPatch.from_json_document(JsonDocument.from_data(data))

    def test_omitted_defaults_original_bytes_and_document_kinds(self):
        d = JsonDocument.from_text('{"kind":"easysewer:scenario","schema_version":"1.0","profile":"epa-swmm:5.2.4","operations":[{"type":"scenario:remove","target":{"type":"core:ref","collection":"swmm:nodes","key":"J"}}]}')
        self.assertEqual(ScenarioPatch.from_json_document(d).to_json_document().to_bytes(), d.to_bytes())
        for data in ({"rain": {}}, {"kind": "easysewer:model"}, {"kind": "easysewer:run"}):
            with self.assertRaises(ValidationError):
                ScenarioPatch.from_json_document(JsonDocument.from_data(data))
        data = d.data; data["operations"][0]["cascade"] = 1
        with self.assertRaises(ValidationError):
            ScenarioPatch.from_json_document(JsonDocument.from_data(data))
        with self.assertRaises(ValueError):
            fields("options", "settings", **{})

    def test_trusted_operation_registration_promotes_unknown_without_core_changes(self):
        @dataclass(frozen=True, kw_only=True)
        class SetInterval:
            seconds: int
        def encode(value, types):
            return {"type": "example:interval", "seconds": value.seconds}
        def decode(data, types):
            if set(data) != {"type", "seconds"} or type(data["seconds"]) is not int:
                raise ValueError("Invalid interval payload")
            return SetInterval(seconds=data["seconds"])
        spec = OperationSpec(key="example:interval", operation_type=SetInterval, encode=encode, decode=decode,
            apply=lambda op, model, types: model.update_options(rule_step=timedelta(seconds=op.seconds)),
            json_schema=JsonDocument.from_data({"type": "object", "required": ["type", "seconds"],
                "properties": {"type": {"const": "example:interval"}, "seconds": {"type": "integer"}}}))
        registry = default_registry(); snapshot = registry.snapshot(); registry.register(spec)
        p = ScenarioPatch(operations=(SetInterval(seconds=9),), required_capabilities=("example:interval",))
        document = p.to_json_document(registry=registry)
        unknown = ScenarioPatch.from_json_document(document)
        self.assertIsInstance(unknown.operations[0], OpaqueOperation)
        with self.assertRaises(ValidationError):
            unknown.apply(hydrology_model(), registry=registry)
        promoted = ScenarioPatch.from_json_document(document, registry=registry)
        self.assertEqual(promoted.apply(hydrology_model(), registry=registry).model.options.rule_step, timedelta(seconds=9))
        self.assertNotIn(spec.key, snapshot.capabilities)
        with self.assertRaises(ValueError):
            registry.register(spec)




class RunConfigTests(unittest.TestCase):
    def test_explicit_version_migration_is_independent_for_scenario_and_run(self):
        from easysewer.io.json.migrations import MigrationOutput, MigrationRegistry
        sources = (ScenarioPatch(operations=(fields("options", "settings", rule_step=timedelta(seconds=7)),)).to_json_document(),
                   RunConfig(output_directory=FileReference(path="runs", direction="output")).to_json_document())
        for document, loader in zip(sources, (ScenarioPatch.from_json_document, RunConfig.from_json_document)):
            old = document.data; old["schema_version"] = "0.9"
            old = JsonDocument.from_data(old)
            with self.assertRaises(ValidationError):
                loader(old)
            registry = MigrationRegistry()
            def transform(source):
                data = source.data; data["schema_version"] = "1.0"
                return MigrationOutput(document=JsonDocument.from_data(data))
            registry.register("0.9", "1.0", transform)
            migrated = registry.upgrade(old, target="1.0")
            self.assertEqual(migrated.changes[0].path, "$/schema_version")
            self.assertEqual(loader(migrated.document).to_json_document().data, document.data)
            self.assertEqual(old.data["schema_version"], "0.9")

    def test_config_roundtrip_paths_support_and_no_filesystem_effects(self):
        config = RunConfig(output_directory=FileReference(path="runs", flavor="windows", base_directory="D:/tmp", direction="output"),
            input_directory=FileReference(path="inputs", flavor="windows", base_directory="C:/data"),
            progress_interval=timedelta(milliseconds=125), wall_time_limit=timedelta(minutes=5),
            required_capabilities=("runtime:cancel",), report_read=ReportReadOptions(encoding="gb18030", tables=("swmm:node_depth",)))
        d = config.to_json_document(); result = RunConfig.from_json_document(d)
        self.assertEqual(result.to_json_document().to_bytes(), d.to_bytes())
        self.assertEqual(result.resolved_paths().output.path, "D:\\tmp\\runs\\model.out")
        self.assertEqual(result.progress_interval, timedelta(milliseconds=125))
        self.assertFalse(result.validate_support().is_valid)
        self.assertTrue(result.validate_support(backends=("swmm:standard",), capabilities=("runtime:cancel",)).is_valid)
        with patch("pathlib.Path.mkdir", side_effect=AssertionError("No IO")):
            result.resolved_paths()

    def test_unknown_runtime_fields_retained_but_require_support(self):
        config = RunConfig(output_directory=FileReference(path="/tmp/runs", flavor="posix", direction="output"))
        data = config.to_json_document().data
        data["settings"]["report_read"]["future"] = {"x": 1}
        data["extensions"] = {"custom:solver": {"threshold": 5}}
        document = JsonDocument.from_bytes(b"\xef\xbb\xbf" + json.dumps(data).encode())
        result = RunConfig.from_json_document(document)
        self.assertEqual(result.to_json_document().to_bytes(), document.to_bytes())
        edited = replace(result, artifact_stem="case_1").to_json_document()
        self.assertEqual(edited.data["settings"]["report_read"]["future"], {"x": 1})
        self.assertFalse(result.validate_support(backends=("swmm:standard",), extensions=("custom:solver",)).is_valid)
        clean = replace(config, extensions=JsonDocument.from_data({"custom:solver": {"x": 1}}))
        self.assertTrue(clean.validate_support(backends=("swmm:standard",), extensions=("custom:solver",)).is_valid)

    def test_invalid_config_and_schema_and_atomic_failure(self):
        base = dict(output_directory=FileReference(path="runs", direction="output"))
        for changes in ({"artifact_stem": "../escape"}, {"artifact_stem": "CON"}, {"overwrite": 1},
                        {"progress_interval": timedelta(0)}, {"wall_time_limit": -1},
                        {"backend": "module.import"}, {"output_directory": FileReference(path="runs")}):
            with self.subTest(changes=changes), self.assertRaises((TypeError, ValueError)):
                RunConfig(**(base | changes))
        config = RunConfig(**base)
        with self.assertRaises(ValueError):
            config.resolved_paths()
        self.assertIn("run:config", run_schema()["$defs"])
        self.assertIn("scenario:set_fields", scenario_schema()["$defs"])
        for filename, expected in (("run-1.0.schema.json", run_schema()), ("scenario-1.0.schema.json", scenario_schema())):
            self.assertEqual(json.loads((Path(__file__).parents[1] / "docs" / filename).read_text(encoding="utf-8")), expected)
        with self.assertRaises(ValueError):
            ReportReadOptions(encoding="nonexistent-codec")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.json"; path.write_bytes(b"old")
            with patch("easysewer.io._atomic.os.replace", side_effect=OSError("injected")):
                with self.assertRaises(OSError):
                    config.to_json(path)
            self.assertEqual(path.read_bytes(), b"old")
            self.assertEqual(list(Path(directory).iterdir()), [path])


if __name__ == "__main__":
    unittest.main()
