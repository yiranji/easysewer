"""D joint groundwater/treatment model contract and conservative syntax handling."""
import unittest

from easysewer.io.inp import InpDocument
from easysewer.model import Model
from easysewer.model.expressions import walk_expression
from easysewer.model.treatment import TreatmentRemoval
from easysewer.scenario import ScenarioPatch, RemoveRecord
from easysewer.validation import ValidationError
from d_domain_fixture import (ORACLE,model,imported,edited,edited_oracle,state,ref,schema,
    edit_patch,treatment,GroundwaterCodec,TreatmentCodec,GroundwaterExpressionCodec)
from test_scenario_v2 import portable


class DDomainTests(unittest.TestCase):
    def test_create_edit_shared_aquifer_overrides_and_all_roundtrips(self):
        self.assertEqual(state(model()),state(imported()))
        for original in (model(),imported()):
            before=original.to_json_document().to_bytes();value=edited(original)
            self.assertEqual(original.to_json_document().to_bytes(),before)
            self.assertEqual({r.aquifer.key for r in value.groundwater.values()},{'SharedSoil'})
            self.assertEqual({r.node.key for r in value.groundwater.values()},{'Tank'})
            self.assertIsNone(value.groundwater['Basin'].threshold_elevation)
            self.assertEqual(value.groundwater['S2'].threshold_elevation,0)
            self.assertIsNone(value.groundwater['Basin'].water_table_elevation)
            self.assertEqual(value.groundwater['S2'].water_table_elevation,4)
            removal=[n.pollutant.key for n in walk_expression(value.treatment[('Tank','Tracer')].expression)
                     if isinstance(n,TreatmentRemoval)]
            self.assertEqual(removal,['Solids'])
            self.assertEqual(state(portable(value)),state(value))
            for restored in (Model.from_document(value.to_document(),strict=True),
                Model.from_document(value.to_document(normalize=True),strict=True),
                Model.from_document(InpDocument.from_text(edited_oracle()),strict=True)):
                self.assertEqual(state(restored,relation_order=False),state(value,relation_order=False))
                self.assertTrue(restored.validate(for_run=True).is_valid)

    def test_two_independent_codecs_promote_shared_graph_without_duplicates(self):
        inactive=schema(False)
        value=Model.from_document(InpDocument.from_text(ORACLE),schema=inactive,strict=True)
        self.assertEqual(len(value.support.opaque_records),9)
        self.assertEqual(value.to_document().text,ORACLE)
        with self.assertRaises(ValidationError):value.nodes.rename('J','Tank')
        for cls in (TreatmentCodec,GroundwaterCodec):inactive.register(cls.descriptor,cls())
        upgraded=Model.from_json_document(value.to_json_document(),schema=inactive,strict=True)
        self.assertEqual(state(upgraded),state(imported()))
        document=edited(upgraded).to_document()
        for section,count in (('AQUIFERS',1),('GROUNDWATER',2),('GWF',4),('TREATMENT',2)):
            self.assertEqual(len(document.records(section)),count)

    def test_joint_reference_deletion_cycles_and_failed_patch_are_atomic(self):
        value=imported();before=value.to_json_document().to_bytes()
        for target in (ref('aquifers','SharedSoil'),ref('patterns','Monthly'),ref('nodes','Tank'),ref('pollutants','Solids')):
            with self.assertRaises(ValidationError):
                ScenarioPatch(operations=edit_patch().operations+(RemoveRecord(target=target),)).apply(value)
            self.assertEqual(value.to_json_document().to_bytes(),before)
        changed=edited(value);frozen=changed.to_json_document().to_bytes()
        for mutation in (lambda:changed.groundwater.update('S2',water_table_elevation=30),
            lambda:changed.treatment.update(('Tank','Solids'),expression=treatment('R_Tracer',('Solids','Tracer')))):
            with self.assertRaises(ValidationError):
                with changed.transaction():mutation()
            self.assertEqual(changed.to_json_document().to_bytes(),frozen)
        removed=changed.aquifers.remove('SharedSoil',cascade=True)
        self.assertFalse(changed.groundwater)
        self.assertTrue({ref('groundwater','Basin'),ref('groundwater','S2')}<=set(removed))
        self.assertEqual(len(changed.treatment),2)
        self.assertIn('groundwater.inactive_expression',{d.code for d in changed.validate().diagnostics})
        changed.pollutants.remove('Solids',cascade=True)
        self.assertFalse(changed.treatment)
        self.assertEqual(state(portable(changed)),state(changed))

    def test_unknown_expression_bytes_and_diagnostics_survive_json_and_reject_execution(self):
        source=ORACLE.replace('S LATERAL 0.002 * (HGW - HCB) + 0.0001 * HSW','S LATERAL FUTURE(HGW) ; future gw').replace(
            'J A C=A * EXP(-.1 * HRT)','J A C=FILTER(A) ; future treatment')
        value=Model.from_document(InpDocument.from_text(source,source='future-d.inp'))
        for restored in (value,Model.from_json_document(value.to_json_document())):
            self.assertEqual(restored.document.text,source)
            self.assertNotIn(('S','LATERAL'),restored.gwf)
            self.assertNotIn(('J','A'),restored.treatment)
            self.assertEqual(len(restored.groundwater),2)
            errors=restored.validate(for_run=True).errors
            self.assertTrue({'groundwater.invalid_input','treatment.invalid_input'}<={d.code for d in errors})
            self.assertTrue(all(d.span and d.span.source=='future-d.inp' for d in errors
                if d.code in ('groundwater.invalid_input','treatment.invalid_input')))
            frozen=restored.to_json_document().to_bytes()
            for operation in (lambda:restored.to_document(),lambda:restored.nodes.rename('J','New'),lambda:restored.convert_units('CMS')):
                with self.assertRaises(ValidationError):operation()
                self.assertEqual(restored.to_json_document().to_bytes(),frozen)

    def test_physical_and_pollutant_units_keep_both_expression_domains_and_origins(self):
        value=edited(imported());original=value.copy()
        owners=(ref('aquifers','SharedSoil'),ref('groundwater','Basin'),ref('groundwater','S2'),
            ref('gwf',('Basin','LATERAL')),ref('treatment',('Tank','Solids')),ref('treatment',('Tank','Tracer')))
        value.convert_units('CMS',basis='physical')
        self.assertAlmostEqual(value.aquifers['SharedSoil'].conductivity,.4*25.4)
        self.assertAlmostEqual(value.aquifers['SharedSoil'].tension_slope,15*.3048)
        self.assertAlmostEqual(value.groundwater['S2'].water_table_elevation,4*.3048)
        self.assertIsNone(value.groundwater['Basin'].threshold_elevation)
        self.assertEqual(value.groundwater['S2'].threshold_elevation,0)
        self.assertNotEqual(value.gwf[('Basin','LATERAL')].expression,original.gwf[('Basin','LATERAL')].expression)
        self.assertEqual(value.treatment[('Tank','Solids')].expression,original.treatment[('Tank','Solids')].expression)
        groundwater=value.gwf[('Basin','LATERAL')].expression
        value.convert_pollutant_units('Solids','UG/L')
        self.assertEqual(value.pollutants['Solids'].groundwater_concentration,20000)
        self.assertEqual(value.gwf[('Basin','LATERAL')].expression,groundwater)
        self.assertEqual(value.treatment[('Tank','Solids')].expression,
            treatment('1000 * ((Solids / 1000) * EXP(-.2 * HRT))',('Solids','Tracer')))
        self.assertEqual(value.treatment[('Tank','Tracer')].expression,original.treatment[('Tank','Tracer')].expression)
        restored=Model.from_json_document(value.to_json_document(),strict=True)
        self.assertEqual(state(restored),state(value))
        for owner in owners:self.assertEqual(restored.provenance(owner),value.provenance(owner))
        self.assertEqual(state(portable(value)),state(value))


if __name__=='__main__':unittest.main()
