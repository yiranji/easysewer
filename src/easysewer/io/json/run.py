"""Explicit RunConfig schema 1.0; unknown execution settings remain unsupported."""

from dataclasses import replace
from datetime import timedelta
import json

from ...runtime.config import ReportReadOptions, RunConfig
from .catalog import builtin_types
from .document import JsonDocument, fail
from .scenario import check_version
from .types import JsonField, JsonType, JsonTypes, UnknownValue

KIND = "easysewer:run"


def config_types():
    def f(name, shape):
        return JsonField(name=name, attribute=name, shape=shape)
    return JsonTypes((*builtin_types(),
        JsonType(key="run:report_read", value_type=ReportReadOptions, fields=(
            f("encoding", ("union", ("null",), ("string",))),
            f("on_decode_error", ("literal", "preserve", "raise")), f("tables", ("array", ("string",))),
            f("on_table_error", ("literal", "preserve", "raise")), f("max_bytes", ("integer",))),
            defaults=(("encoding", None), ("on_decode_error", "preserve"), ("tables", ()),
                      ("on_table_error", "preserve"), ("max_bytes", 64*1024*1024))),
        JsonType(key="run:config", value_type=RunConfig, fields=(
            f("output_directory", ("object", "core:file")),
            f("input_directory", ("union", ("null",), ("object", "core:file"))),
            f("backend", ("string",)), f("artifact_stem", ("string",)), f("overwrite", ("boolean",)),
            f("keep_failed_artifacts", ("boolean",)), f("progress_interval", ("object", "core:duration")),
            f("cancellation_poll_interval", ("object", "core:duration")),
            f("wall_time_limit", ("union", ("null",), ("object", "core:duration"))),
            f("native_call_timeout", ("object", "core:duration")), f("step_batch_size", ("integer",)),
            f("file_inspection_limit", ("integer",)), f("normalize_inp", ("boolean",)),
            f("report_read", ("object", "run:report_read")), f("required_capabilities", ("array", ("string",))),
            f("cache_reuse", ("array", ("array", ("string",))))),
            defaults=(("input_directory", None), ("backend", "swmm:standard"), ("artifact_stem", "model"),
                      ("overwrite", False), ("keep_failed_artifacts", True), ("progress_interval", timedelta(seconds=1)),
                      ("cancellation_poll_interval", timedelta(seconds=1)), ("wall_time_limit", None),
                      ("native_call_timeout", timedelta(seconds=30)), ("step_batch_size", 100),
                      ("file_inspection_limit", 64*1024*1024), ("normalize_inp", False),
                      ("report_read", ReportReadOptions()), ("required_capabilities", ()), ("cache_reuse", ())))))


def read_config(document):
    data = document.data
    if type(data) is not dict or data.get("kind") != KIND:
        fail("run.kind", "Expected an easysewer:run document")
    if not {"kind", "schema_version", "settings"} <= data.keys():
        fail("run.envelope", "Run configuration requires kind, schema_version and settings")
    check_version(data["schema_version"])
    extras = []
    try:
        value = config_types().decode(data["settings"], extras=extras)
    except UnknownValue as error:
        # Whole unknown settings cannot masquerade as a valid RunConfig.
        fail("run.unknown_variant", str(error))
    if type(value) is not RunConfig:
        fail("run.settings_type", "Settings must be a run:config value")
    return replace(value, schema_version=data["schema_version"], extensions=JsonDocument.from_data(data.get("extensions", {})),
        _source=document, _unknown_fields=tuple(sorted(data.keys() - {"kind", "schema_version", "settings", "extensions"}))
            + tuple(str(path + (name,)) for path, names in extras for name in names))


def write_config(config):
    from .model import _merge_extras
    types = config_types()
    settings = types.encode(config)
    data = dict(config._source.data) if config._source else {}
    if config._source:
        extras = []
        baseline = types.decode(config._source.data["settings"], extras=extras)
        # Keep unknown fields only where they cannot be reattached to another value.
        baseline_data = types.encode(baseline)
        if json.dumps(settings, sort_keys=True) == json.dumps(baseline_data, sort_keys=True):
            settings = config._source.data["settings"]
        else:
            settings = _merge_extras(settings, config._source.data["settings"], baseline_data, extras)
    data.update(kind=KIND, schema_version=config.schema_version, settings=settings)
    if config.extensions and config.extensions.data or "extensions" in data:
        data["extensions"] = config.extensions.data if config.extensions else {}
    if config._source and json.dumps(data, sort_keys=True) == json.dumps(config._source.data, sort_keys=True):
        return config._source
    return JsonDocument.from_data(data)


def run_schema():
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", "$defs": config_types().schema(),
        "type": "object", "required": ["kind", "schema_version", "settings"], "properties": {
            "kind": {"const": KIND}, "schema_version": {"type": "string", "pattern": r"^1\.(0|[1-9][0-9]*)$"},
            "settings": {"$ref": "#/$defs/run:config"}, "extensions": {"type": "object"}}}
