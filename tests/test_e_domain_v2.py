"""E independent registration, ordered seasonal assignments and shared graph."""
from dataclasses import replace
import unittest
from easysewer.io.inp import InpDocument
from easysewer.model import Model
from easysewer.validation import ValidationError
from e_domain_fixture import ORACLE,model,imported,edited,edited_oracle,state,ref,schema,RdiiCodec
from test_scenario_v2 import portable
from test_files_v2 import bind

class EDomainTests(unittest.TestCase):
    def test_created_and_legacy_imported_shared_graph_edits_roundtrip_without_flattening(self):
        self.assertEqual(state(model()),state(imported()))
        self.assertEqual(imported().to_document().text,ORACLE)
        for original in (model(),imported()):
            frozen=original.to_json_document().to_bytes();value=edited(original)
            self.assertEqual(original.to_json_document().to_bytes(),frozen)
            group=value.hydrographs['Seasonal']
            self.assertEqual(len(group.responses),7)
            self.assertEqual([r.fraction for r in group.for_month(1)],[.1,.08,.04])
            self.assertEqual([r.fraction for r in group.for_month(2)],[.4,.16,.04])
            self.assertEqual([r.fraction for r in group.for_month(3)],[.1,.08,.04])
            self.assertIsNone(group.responses[5].maximum_abstraction)
            self.assertEqual(group.responses[6].maximum_abstraction,0)
            self.assertEqual(group.prior_rain_gages,(ref('raingages','Activated'),))
            self.assertEqual(group.rain_gage,ref('raingages','Gauge'))
            self.assertEqual({r.hydrograph for r in value.rdii.values()},{ref('hydrographs','Seasonal')})
            self.assertEqual(state(portable(value)),state(value))
            for restored in (Model.from_document(value.to_document(),strict=True),
                Model.from_document(value.to_document(normalize=True),strict=True),
                Model.from_document(InpDocument.from_text(edited_oracle()),strict=True)):
                self.assertEqual(state(restored,relation_order=False),state(value,relation_order=False))
                self.assertTrue(restored.validate(for_run=True).is_valid)

    def test_independent_codec_promotes_all_rows_once_and_retains_origins(self):
        inactive=schema(False)
        value=Model.from_document(InpDocument.from_text(ORACLE,source='e-original.inp'),schema=inactive,strict=True)
        self.assertEqual(len(value.support.opaque_records),9)
        self.assertEqual(value.to_document().text,ORACLE)
        with self.assertRaises(ValidationError):value.raingages.rename('Earlier','Unsafe')
        inactive.register(RdiiCodec.descriptor,RdiiCodec())
        restored=Model.from_json_document(value.to_json_document(),schema=inactive,strict=True)
        self.assertEqual(state(restored),state(imported()))
        changed=edited(restored);document=changed.to_document(normalize=True)
        self.assertEqual(len(document.records('HYDROGRAPHS')),9)
        self.assertEqual(len(document.records('RDII')),2)
        rebuilt=Model.from_json_document(changed.to_json_document(),schema=schema(),strict=True)
        for owner in (ref('hydrographs','Seasonal'),ref('rdii','Receiving'),ref('rdii','K')):
            self.assertTrue(changed.provenance(owner))
            self.assertEqual(rebuilt.provenance(owner),changed.provenance(owner))

    def test_shared_dependencies_failed_edits_and_cache_identity_guards(self):
        value=edited();frozen=value.to_json_document().to_bytes()
        for collection,key in (('hydrographs','Seasonal'),('raingages','Activated'),('raingages','Gauge'),('timeseries','Storm')):
            with self.assertRaises(ValidationError):getattr(value,collection).remove(key)
            self.assertEqual(value.to_json_document().to_bytes(),frozen)
        with self.assertRaises(ValidationError):
            with value.transaction():
                group=value.hydrographs['Seasonal']
                value.rdii.update('K',sewer_area=9)
                value.hydrographs.update('Seasonal',responses=tuple(replace(r,fraction=2) for r in group.responses))
        self.assertEqual(value.to_json_document().to_bytes(),frozen)
        bind(value,'RDII','USE','history.bin');frozen=value.to_json_document().to_bytes()
        for operation in (lambda:value.nodes.rename('K','Other'),lambda:value.nodes.move('K',before='Receiving'),
            lambda:value.rdii.update('K',sewer_area=5),lambda:value.hydrographs.update('Seasonal',prior_rain_gages=()),
            lambda:value.convert_units('CMS')):
            with self.assertRaises(ValidationError):operation()
            self.assertEqual(value.to_json_document().to_bytes(),frozen)
        value.files.remove(('RDII','USE'));bind(value,'RDII','SAVE','new.bin')
        value.rdii.update('K',sewer_area=5)
        removed=value.hydrographs.remove('Seasonal',cascade=True)
        self.assertFalse(value.rdii)
        self.assertTrue({ref('rdii','Receiving'),ref('rdii','K')}<=set(removed))

    def test_physical_units_keep_assignment_order_response_timing_and_provenance(self):
        value=edited();before=value.copy();value.convert_units('CMS',basis='physical')
        a=before.hydrographs['Seasonal'];b=value.hydrographs['Seasonal']
        self.assertEqual([(r.month,r.response,r.fraction,r.time_to_peak,r.recession_ratio) for r in a.responses],
            [(r.month,r.response,r.fraction,r.time_to_peak,r.recession_ratio) for r in b.responses])
        for first,second in zip(a.responses,b.responses):
            for field in ('maximum_abstraction','recovery_rate','initial_abstraction'):
                old,new=getattr(first,field),getattr(second,field)
                if old is None:self.assertIsNone(new)
                else:self.assertAlmostEqual(new,old*25.4)
        self.assertAlmostEqual(value.rdii['K'].sewer_area,4*.40468564224)
        self.assertEqual(state(portable(value)),state(value))
        rebuilt=Model.from_json_document(value.to_json_document(),schema=schema(),strict=True)
        self.assertEqual(state(rebuilt),state(value))
        self.assertEqual(rebuilt.provenance(ref('hydrographs','Seasonal')),value.provenance(ref('hydrographs','Seasonal')))

if __name__=='__main__':unittest.main()
