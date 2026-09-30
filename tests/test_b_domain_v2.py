"""B acceptance: compound identity, shared resources and independent units."""
import unittest

from easysewer.io.inp import InpDocument
from easysewer.io.inp.inflows import InflowsCodec
from easysewer.model import Model
from easysewer.model.inflows import FlowInflow, ConcentrationInflow
from easysewer.scenario import ScenarioPatch, RemoveRecord
from easysewer.validation import ValidationError
from test_scenario_v2 import portable
from b_domain_fixture import ORACLE, model, imported, state, edited, edited_oracle, edit_patch, ref, schema


class BDomainTests(unittest.TestCase):
    def assert_relations(self, value, node='J', pollutants=('A', 'B')):
        keys = ((node, 'FLOW'), *((node, 'POLLUTANT:'+key) for key in pollutants))
        for namespace in ('inflows', 'dwf'):
            self.assertEqual(tuple(getattr(value, namespace)), keys)

    def test_six_relations_from_empty_edit_and_all_roundtrips(self):
        self.assertEqual(state(model()), state(imported()))
        for value in (model(), imported()):
            self.assert_relations(value)
            before = value.to_json_document().to_bytes()
            changed = edited(value)
            self.assertEqual(value.to_json_document().to_bytes(), before)
            self.assert_relations(changed, 'Tank', ('Solids', 'Tracer'))
            self.assertEqual(len(changed.resource_uses(ref('patterns', 'WorkDays'))), 6)
            self.assertEqual(changed.dwf[('Tank', 'POLLUTANT:Solids')].patterns[0], None)
            self.assertEqual(changed.inflows[('Tank', 'FLOW')].series, ref('timeseries', 'Hydrograph'))
            for restored in (portable(changed), Model.from_json_document(changed.to_json_document(), strict=True),
                Model.from_document(changed.to_document(), strict=True),
                Model.from_document(changed.to_document(normalize=True), strict=True),
                Model.from_document(InpDocument.from_text(edited_oracle()), strict=True)):
                self.assertEqual(state(restored), state(changed))
                self.assertTrue(restored.validate(for_run=True).is_valid)

    def test_independent_feature_registration_claims_each_relation_once(self):
        inactive = schema(False)
        value = Model.from_document(InpDocument.from_text(ORACLE), schema=inactive, strict=True)
        self.assertEqual(len(value.support.opaque_records), 6)
        self.assertEqual(value.to_document().text, ORACLE)
        with self.assertRaises(ValidationError):
            value.nodes.rename('J', 'Tank')
        inactive.register(InflowsCodec.descriptor, InflowsCodec())
        promoted = Model.from_json_document(value.to_json_document(), schema=inactive, strict=True)
        self.assert_relations(promoted)
        changed = edited(promoted)
        for section in ('INFLOWS', 'DWF'):
            self.assertEqual(len(changed.to_document().records(section)), 3)
        self.assertEqual(state(changed), state(edited()))

    def test_flow_and_pollutant_unit_conversion_touch_only_their_consumers(self):
        value = model()
        value.inflows.add(FlowInflow(node=ref('nodes', 'O'), series=ref('timeseries', 'Q'), baseline=.025))
        before = state(value)
        value.convert_units('CMS', basis='physical')
        self.assertAlmostEqual(value.timeseries['Q'].points[0].value, .1*.028316846592)
        self.assertAlmostEqual(value.inflows[('J', 'FLOW')].baseline, .05*.028316846592)
        self.assertAlmostEqual(value.dwf[('J', 'FLOW')].baseline, .1*.028316846592)
        self.assertEqual(value.timeseries['C'], dict(before[3])['C'])
        self.assertEqual(value.timeseries['M'], dict(before[3])['M'])
        value.convert_pollutant_units('A', 'UG/L')
        self.assertEqual(value.timeseries['C'].points[0].value, 500)
        self.assertEqual(value.inflows[('J', 'POLLUTANT:A')].baseline, 2000)
        self.assertEqual(value.dwf[('J', 'POLLUTANT:A')].baseline, 4000)
        value.convert_pollutant_units('B', 'MG/L')
        self.assertEqual(value.inflows[('J', 'POLLUTANT:B')].mass_factor, .126)
        self.assertEqual(value.dwf[('J', 'POLLUTANT:B')].baseline, .03)
        self.assertEqual(value.timeseries['M'], dict(before[3])['M'])
        self.assertEqual(tuple(value.patterns.items()), before[4])
        self.assertEqual(state(portable(value)), state(value))

    def test_shared_concentration_cannot_be_partially_reinterpreted(self):
        value = model()
        value.reinterpret_pollutant_units('B', 'MG/L')
        value.inflows.replace(('J', 'POLLUTANT:B'), ConcentrationInflow(node=ref('nodes', 'J'),
            constituent=ref('pollutants', 'B'), series=ref('timeseries', 'C'), baseline=3))
        before = value.to_json_document().to_bytes()
        with self.assertRaises(ValidationError):
            value.convert_pollutant_units('A', 'UG/L')
        self.assertEqual(value.to_json_document().to_bytes(), before)

    def test_failed_edits_deletion_and_provenance_keep_compound_identities(self):
        value = imported()
        before = value.to_json_document().to_bytes()
        for target in (ref('pollutants', 'Solids'), ref('patterns', 'WorkDays'), ref('nodes', 'Tank')):
            with self.assertRaises(ValidationError):
                ScenarioPatch(operations=edit_patch().operations+(RemoveRecord(target=target),)).apply(value)
            self.assertEqual(value.to_json_document().to_bytes(), before)
        changed = edited(value)
        for namespace in ('inflows', 'dwf'):
            owner = ref(namespace, ('Tank', 'POLLUTANT:Solids'))
            self.assertEqual(changed.provenance(owner).original, ref(namespace, ('J', 'POLLUTANT:A')))
            self.assertEqual(Model.from_json_document(changed.to_json_document()).provenance(owner), changed.provenance(owner))
        removed = changed.pollutants.remove('Solids', cascade=True)
        for namespace in ('inflows', 'dwf'):
            self.assertIn(ref(namespace, ('Tank', 'POLLUTANT:Solids')), removed)
            self.assertEqual(tuple(getattr(changed, namespace)), (('Tank', 'FLOW'), ('Tank', 'POLLUTANT:Tracer')))
        self.assertEqual(state(portable(changed)), state(changed))


if __name__ == '__main__':
    unittest.main()
