from dataclasses import dataclass, fields
from datetime import date, time, timedelta
from pathlib import Path
import tempfile
import unittest

from easysewer.io.inp import InpDocument
from easysewer.model import Model, Ref
from easysewer.model.inflows import FLOW, FlowInflow, DryWeatherFlow, ExternalInflow, inflow_key
from easysewer.model.fields import validate_fields
from easysewer.model.network import Junction, Outfall, FreeBoundary, Conduit, Storage, FunctionalStorage, Divider, OverflowDivider
from easysewer.model.geometry import CrossSection, Circular
from easysewer.model.resources import InlineTimeSeries, FileTimeSeries, SeriesPoint, Pattern
from easysewer.model.values import FileReference
from easysewer.validation import ValidationError


def ref(namespace, name):
    return Ref(collection="swmm:" + namespace, key=name)


def inflow_model():
    model = Model()
    model.update_options(flow_units="CFS", flow_routing="DYNWAVE", start_date=date(2020, 1, 30),
        end_date=date(2020, 2, 3), end_time=time(), report_step=timedelta(hours=1),
        wet_step=timedelta(minutes=5), routing_step=timedelta(minutes=5), variable_step=0)
    model.nodes.add(Junction(id="J", elevation=10, max_depth=5))
    model.nodes.add(Outfall(id="O", elevation=9, boundary=FreeBoundary()))
    model.links.add(Conduit(id="P", inlet=ref("nodes", "J"), outlet=ref("nodes", "O"), length=100,
        roughness=.013, inlet_offset=0, outlet_offset=0, section=CrossSection(geometry=Circular(diameter=2))))
    return model


def add_series(model, name="Q"):
    model.timeseries.add(InlineTimeSeries(id=name, points=tuple(SeriesPoint(time=timedelta(hours=hour), value=value)
        for hour, value in ((0, .1), (24, .3), (48, .2), (96, .1)))))
    return ref("timeseries", name)


def rebuild(model):
    result = Model()
    result.update_options(**{item.name: getattr(model.options, item.name) for item in fields(model.options)})
    for name in ("nodes", "links", "curves", "timeseries", "patterns", "inflows", "dwf"):
        for row in getattr(model, name).values():
            getattr(result, name).add(row)
    return result


class InflowTests(unittest.TestCase):
    def test_optional_columns_and_empty_series_preserve_source(self):
        prefix = inflow_model().to_document().text + "[TIMESERIES]\nQ 0 .1 96 .1\n[PATTERNS]\nM MONTHLY 2\n"
        for tail in ('Q', 'Q FLOW', 'Q FLOW 1', 'Q FLOW 1 0', 'Q FLOW 1 0 0', 'Q FLOW 1 .5 .1 M', '"" FLOW 1 1 .2 M'):
            with self.subTest(tail=tail):
                source = prefix + "[INFLOWS]\nJ FLOW " + tail + " ; retained\n"
                model = Model.from_document(InpDocument.from_text(source), strict=True)
                self.assertEqual(model.to_document().text, source)
                self.assertFalse(model.support.opaque_records)
                self.assertIs(model.inflows[("j", "flow")].constituent, FLOW)
                fresh = rebuild(model)
                parsed = Model.from_document(fresh.to_document(), strict=True)
                self.assertEqual(list(parsed.inflows.values()), list(fresh.inflows.values()))

    def test_relations_support_all_node_kinds_and_rekey_on_node_rename(self):
        model = inflow_model()
        model.nodes.add(Storage(id="S", elevation=10, max_depth=5, initial_depth=0,
            shape=FunctionalStorage(coefficient=0, exponent=1, constant=50)))
        model.nodes.add(Divider(id="D", elevation=10, law=OverflowDivider()))
        for name in ("J", "O", "S", "D"):
            model.inflows.add(FlowInflow(node=ref("nodes", name), baseline=.2))
            model.dwf.add(DryWeatherFlow(node=ref("nodes", name), baseline=.1))
            model.nodes.rename(name, name + "2")
            self.assertIn((name+"2", "FLOW"), model.inflows)
            self.assertNotIn((name, "FLOW"), model.inflows)
            with self.assertRaises(ValidationError):
                model.nodes.remove(name+"2")
        parsed = Model.from_document(model.to_document(), strict=True)
        self.assertEqual(list(parsed.dwf.values()), list(model.dwf.values()))

    def test_shared_resources_rename_delete_and_copy(self):
        model = inflow_model()
        q = add_series(model)
        model.patterns.add(Pattern(id="M", kind="MONTHLY", factors=(2,)))
        for name in ("J", "O"):
            model.inflows.add(FlowInflow(node=ref("nodes", name), series=q, baseline=.1, pattern=ref("patterns", "M")))
            model.dwf.add(DryWeatherFlow(node=ref("nodes", name), baseline=.2, patterns=(None, ref("patterns", "M"))))
        copied = model.copy()
        model.timeseries.rename("Q", "Hydrograph")
        model.patterns.rename("M", "Monthly")
        self.assertEqual(len(model.resource_uses(ref("timeseries", "Hydrograph"))), 2)
        self.assertEqual(len(model.resource_uses(ref("patterns", "Monthly"))), 4)
        with self.assertRaises(ValidationError):
            model.patterns.remove("Monthly")
        with self.assertRaises(ValidationError):
            model.timeseries.remove("Hydrograph")
        self.assertEqual(copied.inflows[("J", "FLOW")].series.key, "Q")
        self.assertEqual(model.dwf[("J", "FLOW")].patterns[1].key, "Monthly")

    def test_distinct_constituent_extension_keys_do_not_overwrite_flow(self):
        # Storage/graph extension only; a pollutant codec and units are still
        # required before these extension records can be exported or run.
        @dataclass(frozen=True, kw_only=True)
        class TestConcentration(ExternalInflow):
            concentration: float
        model = inflow_model()
        model.inflows.add(FlowInflow(node=ref("nodes", "J"), baseline=.1))
        for pollutant in ("TSS", "BOD"):
            row = TestConcentration(node=ref("nodes", "J"), constituent=ref("pollutants", pollutant), concentration=10)
            model.inflows.add(row)
            self.assertEqual(inflow_key(row), ("J", "POLLUTANT:"+pollutant))
        self.assertEqual(len(model.inflows), 3)
        self.assertEqual(model.inflows[("J", "FLOW")].baseline, .1)

    def test_dwf_all_pattern_kinds_order_empty_slots_and_duplicates(self):
        model = inflow_model()
        for name, kind in (("M", "MONTHLY"), ("D", "DAILY"), ("H", "HOURLY"), ("W", "WEEKEND"), ("H2", "HOURLY")):
            model.patterns.add(Pattern(id=name, kind=kind, factors=(2,)))
        prefix = model.to_document().text
        for tail in ('', 'M', 'W H M D', '"" M "" H', 'H H2'):
            source = prefix + f"[DWF]\nJ FLOW .2 {tail}\n"
            parsed = Model.from_document(InpDocument.from_text(source), strict=True)
            self.assertEqual(parsed.to_document().text, source)
            self.assertEqual(list(Model.from_document(rebuild(parsed).to_document()).dwf.values()), list(parsed.dwf.values()))
            codes = {item.code for item in parsed.validate().diagnostics}
            self.assertEqual("inflow.repeated_pattern_kind" in codes, tail == "H H2")

    def test_flow_and_dwf_same_node_remain_independent(self):
        model = inflow_model()
        model.inflows.add(FlowInflow(node=ref("nodes", "J"), baseline=.1))
        model.dwf.add(DryWeatherFlow(node=ref("nodes", "J"), baseline=.2))
        model.dwf.update(("J", "FLOW"), baseline=.4)
        self.assertEqual(model.inflows[("J", "FLOW")].baseline, .1)
        model.inflows.remove(("J", "FLOW"))
        self.assertEqual(model.dwf[("J", "FLOW")].baseline, .4)

    def test_shared_flow_series_converts_once_and_scaling_does_not_scale_baseline(self):
        model = inflow_model()
        q = add_series(model)
        for node in ("J", "O"):
            model.inflows.add(FlowInflow(node=ref("nodes", node), series=q, scale_factor=3, baseline=.2))
            model.dwf.add(DryWeatherFlow(node=ref("nodes", node), baseline=-.05))
        model.convert_units("CMS")
        self.assertAlmostEqual(model.timeseries["Q"].points[0].value, .1*.02832)
        self.assertAlmostEqual(model.inflows[("J", "FLOW")].baseline, .2*.02832)
        self.assertEqual(model.inflows[("J", "FLOW")].scale_factor, 3)
        self.assertAlmostEqual(model.dwf[("J", "FLOW")].baseline, -.05*.02832)

    def test_conflicting_consumer_dimensions_reject_atomic_conversion(self):
        from easysewer.model.network import SeriesBoundary
        model = inflow_model()
        q = add_series(model)
        model.inflows.add(FlowInflow(node=ref("nodes", "J"), series=q))
        model.nodes.update("O", boundary=SeriesBoundary(series=q))
        self.assertIn("resource.conflicting_dimensions", {item.code for item in model.validate().errors})
        with self.assertRaises(ValidationError):
            model.convert_units("CMS")
        self.assertEqual(model.units.flow_units, "CFS")

    def test_external_series_rebases_but_conversion_requires_data_conversion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/"new").mkdir()
            source = root/"model.inp"
            source.write_text(inflow_model().to_document().text + '[TIMESERIES]\nQ FILE "missing flow.dat"\n[INFLOWS]\nJ FLOW Q\n', encoding="utf-8")
            model = Model.from_inp(source, strict=True)
            model.to_inp(root/"new"/"moved.inp")
            parsed = Model.from_inp(root/"new"/"moved.inp", strict=True)
            self.assertEqual(parsed.timeseries["Q"].file.resolve(), model.timeseries["Q"].file.resolve())
            with self.assertRaises(ValidationError):
                model.convert_units("GPM")
            self.assertEqual(model.units.flow_units, "CFS")

    def test_pollutant_rows_are_owned_alongside_editable_flow(self):
        source = inflow_model().to_document().text + '[POLLUTANTS]\nTSS MG/L 0 0 0 0\nBOD MG/L 0 0 0 0\n[TIMESERIES]\nQ 0 1\nB 0 2\n[INFLOWS]\nJ FLOW "" FLOW 1 1 .2\nJ TSS Q CONCEN\nJ BOD B MASS 126\n[DWF]\nJ FLOW .3\nJ TSS 2\n'
        model = Model.from_document(InpDocument.from_text(source), strict=True)
        self.assertEqual(len(model.inflows), 3)
        self.assertEqual(len(model.support.opaque_records), 0)
        model.inflows.update(("J", "FLOW"), baseline=.4)
        self.assertIn("J TSS Q CONCEN", model.to_document().text)
        self.assertIn("J BOD B MASS 126", model.to_document().text)
        model.nodes.rename("J", "Changed")
        model.convert_units("CMS")
        self.assertIn(('Changed','POLLUTANT:TSS'),model.inflows)
        self.assertEqual(model.timeseries['Q'].points[0].value,1)

    def test_duplicate_assignments_preserve_then_collapse_with_comments(self):
        source = inflow_model().to_document().text + '[INFLOWS]\nJ FLOW "" FLOW 1 1 .1 ; first\nJ FLOW "" FLOW 1 1 .2 ; last\n[DWF]\nJ FLOW .3\nJ FLOW .4\n'
        model = Model.from_document(InpDocument.from_text(source), strict=True)
        self.assertEqual(model.inflows[("J", "FLOW")].baseline, .2)
        self.assertEqual(model.dwf[("J", "FLOW")].baseline, .4)
        self.assertEqual(model.to_document().text, source)
        model.inflows.update(("J", "FLOW"), baseline=.5)
        output = model.to_document()
        self.assertEqual(len(output.records("INFLOWS")), 1)
        self.assertIn("; first", output.text)
        self.assertIn("; last", output.text)

    def test_invalid_later_row_does_not_leave_partial_relation(self):
        for section, rows in (("INFLOWS", 'J FLOW ""\nJ FLOW "" FLOW 1 NaN'), ("DWF", 'J FLOW .1\nJ FLOW wrong')):
            model = Model.from_document(InpDocument.from_text(inflow_model().to_document().text+f"[{section}]\n{rows}\n"))
            self.assertFalse(model.validate().is_valid)
            self.assertEqual(len(model.inflows)+len(model.dwf), 0)

    def test_invalid_drafts_and_namespace_are_diagnosed(self):
        for row in (FlowInflow(node=ref("nodes", "J"), baseline=float("nan")),
                    FlowInflow(node=ref("nodes", "J"), series=ref("patterns", "Q")),
                    DryWeatherFlow(node=ref("nodes", "J"), baseline=.1, patterns=(ref("nodes", "J"),)),
                    DryWeatherFlow(node=ref("nodes", "J"), baseline=.1, patterns=(None,)*5)):
            model = inflow_model()
            (model.inflows if isinstance(row, FlowInflow) else model.dwf).add(row)
            self.assertFalse(model.validate().is_valid)
            with self.assertRaises(ValidationError):
                model.to_document()

    def test_native_source_references_of_overridden_assignments_require_normalization(self):
        for section, rows in (("INFLOWS", 'J FLOW Missing\nJ FLOW "" FLOW 1 1 .2'),
                              ("DWF", 'J FLOW .1 Missing\nJ FLOW .2')):
            model = Model.from_document(InpDocument.from_text(inflow_model().to_document().text+f"[{section}]\n{rows}\n"), strict=True)
            self.assertIn("inflow.native_source_reference", {d.code for d in model.validate(for_run=True).errors})
            self.assertTrue(model.validate(for_run=True, normalize=True).is_valid)

    def test_ignored_flow_type_mass_factor_and_literal_star(self):
        source = inflow_model().to_document().text + '[INFLOWS]\nJ FLOW "" MASS nonsense 2 .1\n'
        model = Model.from_document(InpDocument.from_text(source), strict=True)
        self.assertEqual(model.to_document().text, source)
        self.assertIn("inflow.ignored_flow_slots", {d.code for d in model.validate().diagnostics})
        self.assertEqual(model.to_document(normalize=True).records("INFLOWS")[0].values, ("J", "FLOW", "", "FLOW", "1", "2.0", "0.1"))
        star = Model.from_document(InpDocument.from_text(source.replace('"" MASS nonsense', '* FLOW 1')))
        self.assertFalse(star.validate().is_valid)  # * is a reference, not the empty-series marker.


if __name__ == "__main__":
    unittest.main()
