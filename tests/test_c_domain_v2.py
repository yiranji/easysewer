"""C joint LID contract: layer composition, identity, graph and output paths."""
import tempfile
from pathlib import Path
import unittest

from easysewer.io.inp import InpDocument
from easysewer.io.inp.lid import LidCodec
from easysewer.model import Model, FileReference
from easysewer.runtime import check_files
from easysewer.scenario import ScenarioPatch, RemoveRecord
from easysewer.validation import ValidationError
from test_scenario_v2 import portable
from c_domain_fixture import ORACLE, REPORTS, schema, model, imported, edited, edit_patch, edited_oracle, state, ref


class CDomainTests(unittest.TestCase):
    def test_typed_creation_shared_definitions_and_reordered_roundtrips(self):
        self.assertEqual(state(model()), state(imported()))
        for value in (model(), imported()):
            self.assertIsNotNone(value.lid_controls['Bio'].storage)
            self.assertIsNone(value.lid_controls['Bio'].drain_mat)
            self.assertIsNotNone(value.lid_controls['Green'].drain_mat)
            self.assertIsNone(value.lid_controls['Green'].storage)
            before = value.to_json_document().to_bytes()
            changed = edited(value)
            self.assertEqual(value.to_json_document().to_bytes(), before)
            self.assertEqual(tuple(changed.lid_usage), ('lid-usage-2','lid-usage-1','lid-usage-3','lid-usage-4'))
            self.assertEqual(sum(r.control.key=='Garden' for r in changed.lid_usage.values()), 3)
            for restored in (portable(changed), Model.from_json_document(changed.to_json_document(),strict=True)):
                self.assertEqual(state(restored),state(changed))
            for restored in (Model.from_document(changed.to_document(),strict=True),
                Model.from_document(changed.to_document(normalize=True),strict=True),
                Model.from_document(InpDocument.from_text(edited_oracle()),strict=True)):
                self.assertEqual(state(restored,identities=False),state(changed,identities=False))
                self.assertEqual(tuple(restored.lid_usage),tuple(f'lid-usage-{i}' for i in range(1,5)))
                self.assertTrue(restored.validate(for_run=True).is_valid)

    def test_independent_registration_promotes_control_blocks_and_usages_once(self):
        inactive = schema(False)
        value = Model.from_document(InpDocument.from_text(ORACLE),schema=inactive,strict=True)
        self.assertEqual(len(value.support.opaque_records),13)
        self.assertEqual(value.to_document().text,ORACLE)
        with self.assertRaises(ValidationError):
            value.subcatchments.rename('S','Basin')
        inactive.register(LidCodec.descriptor,LidCodec())
        restored = Model.from_json_document(value.to_json_document(),schema=inactive,strict=True)
        self.assertEqual(state(restored),state(imported()))
        document = edited(restored).to_document()
        self.assertEqual(len(document.records('LID_CONTROLS')),9)
        self.assertEqual(len(document.records('LID_USAGE')),4)

    def test_shared_control_units_and_different_layer_dimensions(self):
        value = model()
        value.convert_units('CMS',basis='physical')
        self.assertAlmostEqual(value.lid_controls['Bio'].soil.thickness,12*25.4)
        self.assertAlmostEqual(value.lid_controls['Bio'].drain.coefficient,.2*25.4**.5)
        self.assertAlmostEqual(value.lid_controls['Green'].drain_mat.thickness,2*25.4)
        self.assertAlmostEqual(value.curves['Head'].points[1].x,36*25.4)
        for i,area in enumerate((100,200,150,80),1):
            row = value.lid_usage[f'lid-usage-{i}']
            self.assertAlmostEqual(row.area,area*.3048**2)
            self.assertAlmostEqual(row.width,10*.3048)
            self.assertEqual(row.from_impervious,20)
            self.assertEqual(row.report_file.path,REPORTS[i-1])
        self.assertEqual(state(portable(value)),state(value))

    def test_rename_delete_failed_area_and_json_keep_independent_origins(self):
        value = imported()
        before = value.to_json_document().to_bytes()
        for target in (ref('lid_controls','Garden'),ref('curves','DrainCurve'),ref('subcatchments','Basin')):
            with self.assertRaises(ValidationError):
                ScenarioPatch(operations=edit_patch(value).operations+(RemoveRecord(target=target),)).apply(value)
            self.assertEqual(value.to_json_document().to_bytes(),before)
        changed = edited(value)
        frozen = changed.to_json_document().to_bytes()
        with self.assertRaises(ValidationError):
            with changed.transaction():
                changed.lid_usage.update('lid-usage-2',area=1e8)
        self.assertEqual(changed.to_json_document().to_bytes(),frozen)
        restored = Model.from_json_document(changed.to_json_document(),strict=True)
        for key in changed.lid_usage:
            self.assertEqual(restored.provenance(ref('lid_usage',key)),changed.provenance(ref('lid_usage',key)))
        removed = changed.lid_controls.remove('Garden',cascade=True)
        self.assertEqual(tuple(changed.lid_usage),('lid-usage-3',))
        for i in (1,2,4):
            self.assertIn(ref('lid_usage',f'lid-usage-{i}'),removed)
        self.assertEqual(state(portable(changed)),state(changed))

    def test_four_output_paths_resolve_independently_and_collisions_preserve_files(self):
        value = model()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            uses = tuple(value.file_uses())
            self.assertEqual(len(uses),4)
            self.assertTrue(all(u.base=='working_directory' and u.access=='write' for u in uses))
            self.assertTrue(check_files(value,working_directory=root).report.is_valid)
            resolved = value.resolve_files(working_directory=root)
            self.assertEqual(tuple(Path(r.report_file.path) for r in resolved.lid_usage.values()),tuple(root/n for n in REPORTS))
            target = root/REPORTS[0]
            target.write_bytes(b'existing report')
            self.assertFalse(check_files(value,working_directory=root).report.is_valid)
            value.lid_usage.update('lid-usage-2',report_file=FileReference(path=REPORTS[0],direction='output'))
            self.assertFalse(check_files(value,working_directory=root,overwrite=True).report.is_valid)
            self.assertEqual(target.read_bytes(),b'existing report')


if __name__ == '__main__':
    unittest.main()
