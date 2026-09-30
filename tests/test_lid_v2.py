"""Complete LID layer, deployment, resource and persistence contracts."""

from dataclasses import dataclass, replace
from pathlib import Path
import tempfile
import unittest

from easysewer.io.inp import InpDocument
from easysewer.model import Model
from easysewer.model import lid as l
from easysewer.model.resources import Curve, CurvePoint
from easysewer.model.values import FileReference
from easysewer.runtime import check_files
from easysewer.scenario import ScenarioPatch, RenameRecord, SetFields, FieldChange, ChangePollutantUnits
from easysewer.validation import ValidationError
from test_quality_v2 import quality_model, ref
from test_scenario_v2 import portable


def control(kind='BC', *, extra=True):
    layers = dict(
        surface=l.LidSurface(storage_depth=3,vegetation_fraction=.1,roughness=.1,slope=2,side_slope=2),
        pavement=l.LidPavement(thickness=4,void_ratio=.25,impervious_fraction=.1,permeability=5,clogging_factor=10,
            regeneration_days=2 if extra else None,regeneration_fraction=.5 if extra else None),
        soil=l.LidSoil(thickness=12,porosity=.5,field_capacity=.2,wilting_point=.1,conductivity=1,conductivity_slope=5,suction=3),
        storage=l.LidStorage(thickness=12,void_ratio=.5,seepage_rate=.1,clogging_factor=10,covered=True if extra else None),
        drain=l.LidDrain(coefficient=.2,exponent=.5,offset=1,delay=2,open_head=2 if extra else None,close_head=.5 if extra else None),
        drain_mat=l.LidDrainMat(thickness=2,void_fraction=.6,roughness=.1))
    names = {'BC':('surface','soil','storage','drain'),'RG':('surface','soil'), 'GR':('surface','soil','drain_mat'),
        'IT':('surface','storage','drain'),'PP':('surface','pavement','soil','storage','drain'),
        'RB':('storage','drain'),'RD':('surface','drain'),'VS':('surface',)}[kind]
    return l.LidControl(id='L',kind=kind,**{name:layers[name] for name in names},
        removals=(l.LidRemoval(pollutant=ref('pollutants','Q0'),percent=25),l.LidRemoval(pollutant=ref('pollutants','Q1'),percent=50)))


def usage(id='lid-usage-1', **changes):
    return l.LidUsage(**(dict(record_id=id,subcatchment=ref('subcatchments','S'),control=ref('lid_controls','L'),number=2,
        area=100,width=10,initial_saturation=50,from_impervious=20,to_pervious=False,from_pervious=10) | changes))


def lid_model(kind='BC', *, extra=True):
    model = quality_model('NONE','EMC')
    model.lid_controls.add(control(kind,extra=extra)); model.lid_usage.add(usage())
    return model


def lid_corpus():
    for kind in l.LID_KINDS:
        yield lid_model(kind,extra=False)
        model = lid_model(kind)
        if model.lid_controls['L'].drain:
            model.curves.add(Curve(id='Head',kind='CONTROL',points=(CurvePoint(x=0,y=.5),CurvePoint(x=36,y=1))))
            model.lid_controls.update('L',drain=replace(model.lid_controls['L'].drain,curve=ref('curves','Head')))
        model.lid_usage.add(usage('lid-usage-2',number=3,area=200,initial_saturation=10,drain_to=ref('nodes','O'),to_pervious=True,
            report_file=FileReference(path='lid detail.txt',direction='output')))
        yield model
    model=lid_model()
    model.lid_usage.add(l.DisabledLidUsage(record_id='lid-usage-2',subcatchment=ref('subcatchments','S'),control=ref('lid_controls','L'),
        parameters=('100','10','50','20','1','ignored detail.txt','NotANode')))
    yield model


class LidTests(unittest.TestCase):
    def assert_domain(self,a,b):
        self.assertEqual(tuple(a.lid_controls.values()),tuple(b.lid_controls.values()))
        self.assertEqual(tuple(a.lid_usage.values()),tuple(b.lid_usage.values()))

    def test_all_eight_types_layers_inp_and_source_free_json(self):
        for model in lid_corpus():
            with self.subTest(kind=model.lid_controls['L'].kind):
                self.assertTrue(model.validate(for_run=True).is_valid,model.validate().errors)
                self.assert_domain(model,portable(model))
                self.assert_domain(model,Model.from_document(portable(model).to_document(),strict=True))

    def test_ordered_duplicate_deployments_keep_independent_identity(self):
        model = lid_model(); model.lid_usage.add(usage('lid-usage-2',area=200))
        source = model.to_document().text
        read = Model.from_document(InpDocument.from_text(source),strict=True)
        self.assert_domain(model,read); self.assertEqual(read.to_document().text,source)
        read.lid_usage.move('lid-usage-2',before='lid-usage-1')
        self.assertEqual([r.area for r in read.lid_usage.values()],[200,100])
        self.assert_domain(read,portable(read))
        reread = Model.from_document(read.to_document(),strict=True)
        self.assertEqual([r.area for r in reread.lid_usage.values()],[200,100])
        # INP has no deployment ID; JSON retains IDs across deletion/reordering.
        self.assertEqual(tuple(reread.lid_usage),('lid-usage-1','lid-usage-2'))
        read.lid_usage.remove('lid-usage-1')
        self.assertEqual(len(Model.from_document(read.to_document(),strict=True).lid_usage),1)

    def test_repeated_layer_removals_comments_and_type_order(self):
        base = quality_model().to_document().text
        block = ('[LID_CONTROLS]\nL DRAINMAT bad bad bad ; ignored before GR\nL GR\n'
            'L SURFACE 3 .1 .1 2 2\nL SOIL 12 .5 .2 .1 1 5 3\nL DRAINMAT 2 .6 .1\n'
            'L REMOVALS Q0 10 Q1 20\nL REMOVALS Q0 30 ; last\n')
        model = Model.from_document(InpDocument.from_text(base+block),strict=True)
        self.assertEqual(model.to_document().text,base+block)
        self.assertEqual([r.percent for r in model.lid_controls['L'].removals],[30,20])
        self.assertEqual(model.lid_controls['L'].drain_mat.void_fraction,.6)
        model.lid_controls.update('L',surface=replace(model.lid_controls['L'].surface,slope=3))
        changed = model.to_document().text
        self.assertIn('; ignored before GR',changed); self.assertIn('; last',changed)
        self.assertEqual(len(InpDocument.from_text(changed).records('LID_CONTROLS')),6)
        self.assert_domain(model,Model.from_document(model.to_document(),strict=True))
        for bad in ('L SOIL 12 .5 .2 .1 1 5','L SOIL 12 .5 .2 .1 1 5 bad','L REMOVALS Q0 10 Q1'):
            broken = Model.from_document(InpDocument.from_text(base+block+bad+'\n'))
            self.assertFalse(broken.validate().is_valid); self.assertFalse(broken.lid_controls)

    def test_references_rename_delete_scenario_and_rollback(self):
        model = lid_model()
        patch = ScenarioPatch(operations=(RenameRecord(target=ref('lid_controls','L'),new_id='Bio'),
            SetFields(target=ref('lid_usage','lid-usage-1'),changes=(FieldChange(name='initial_saturation',value=75),)),
            ChangePollutantUnits(target=ref('pollutants','Q0'),units='UG/L')))
        result = patch.apply(model).model
        self.assertEqual(result.lid_usage['lid-usage-1'].control.key,'Bio')
        self.assertEqual(result.lid_usage['lid-usage-1'].initial_saturation,75)
        self.assertEqual(result.lid_controls['Bio'].removals,model.lid_controls['L'].removals)
        result.pollutants.rename('Q0','TSS'); result.subcatchments.rename('S','Basin')
        self.assertEqual(result.lid_controls['Bio'].removals[0].pollutant.key,'TSS')
        self.assertEqual(result.lid_usage['lid-usage-1'].subcatchment.key,'Basin')
        with self.assertRaises(ValidationError): result.lid_controls.remove('Bio')
        before = portable(result).to_json_document().data
        with self.assertRaises(ValidationError):
            with result.transaction(): result.lid_usage.update('lid-usage-1',area=1e9)
        self.assertEqual(portable(result).to_json_document().data,before)
        result.lid_controls.remove('Bio',cascade=True)
        self.assertFalse(result.lid_usage)

    def test_drain_name_precedence_explicit_union_and_conflicts(self):
        model = lid_model()
        model.subcatchments.add(replace(model.subcatchments['S'],id='O'))
        model.lid_usage.update('lid-usage-1',drain_to=ref('subcatchments','O'))
        self.assert_domain(model,Model.from_document(model.to_document(),strict=True))
        model.lid_usage.update('lid-usage-1',drain_to=ref('nodes','O'))
        self.assertIn('lid.shadowed_drain',{d.code for d in model.validate().errors})
        with self.assertRaises(ValidationError): model.to_document()

    def test_hydraulic_units_drain_exponent_roof_capacity_curve_and_percentages(self):
        for kind in l.LID_KINDS:
            model = lid_model(kind); original = model.lid_controls['L']
            if original.drain:
                model.curves.add(Curve(id='Head',kind='CONTROL',points=(CurvePoint(x=0,y=.5),CurvePoint(x=36,y=1))))
                model.lid_controls.update('L',drain=replace(original.drain,curve=ref('curves','Head')))
            model.convert_units('CMS')
            converted = model.lid_controls['L']
            self.assertAlmostEqual(model.lid_usage['lid-usage-1'].area,100*.3048**2)
            self.assertEqual(converted.removals,original.removals)
            if original.drain:
                self.assertAlmostEqual(converted.drain.coefficient,.2*25.4**(1 if kind=='RD' else .5))
                self.assertAlmostEqual(model.curves['Head'].points[1].x,36*25.4)
            model.convert_units('CFS')
            self.assertAlmostEqual(model.lid_usage['lid-usage-1'].area,100)
            if original.drain: self.assertAlmostEqual(model.lid_controls['L'].drain.coefficient,.2)
        model = lid_model(); model.lid_controls.update('L',drain=replace(model.lid_controls['L'].drain,curve=ref('curves','Head')))
        model.curves.add(Curve(id='Head',kind='STORAGE',points=(CurvePoint(x=0,y=1),CurvePoint(x=10,y=2))))
        self.assertIn('resource.wrong_purpose',{d.code for d in model.validate().errors})

    def test_file_consumers_rebasing_collision_and_read_time_opening(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); model=lid_model()
            model.lid_usage.update('lid-usage-1',report_file=FileReference(path='detail.txt',direction='output'))
            model.update_options(ignore_rainfall=True)
            use, = model.file_uses()
            self.assertEqual((use.base,use.access,use.active),('working_directory','write',True))
            self.assertTrue(check_files(model,working_directory=root).report.is_valid)
            resolved=model.resolve_files(working_directory=root)
            self.assertEqual(resolved.lid_usage['lid-usage-1'].report_file.path,str(root/'detail.txt'))
            (root/'detail.txt').write_text('existing',encoding='utf-8')
            self.assertFalse(check_files(model,working_directory=root).report.is_valid)
            model.lid_usage.add(usage('lid-usage-2',report_file=FileReference(path='detail.txt',direction='output')))
            self.assertFalse(check_files(model,working_directory=root,overwrite=True).report.is_valid)
            self.assertEqual((root/'detail.txt').read_text(encoding='utf-8'),'existing')

    def test_local_and_aggregate_physical_constraints_and_native_tolerances(self):
        cases = (dict(number=-1),dict(area=0),dict(initial_saturation=101),dict(from_impervious=101),dict(to_pervious=2))
        for changes in cases:
            model=lid_model()
            with self.assertRaises(ValidationError):
                with model.transaction(): model.lid_usage.update('lid-usage-1',**changes)
        for changes,code in ((dict(from_impervious=90),'lid.capture_total'),(dict(from_pervious=95),'lid.capture_total'),(dict(area=1e6),'lid.area_total')):
            model=lid_model(); model.lid_usage.add(usage('second',**changes))
            self.assertIn(code,{d.code for d in model.validate().errors})
        model=lid_model('RB'); model.lid_controls.update('L',storage=replace(control('RB').storage,void_ratio=0))
        self.assertFalse(model.validate().is_valid)
        model=lid_model('PP'); model.lid_controls.update('L',pavement=replace(control('PP').pavement,permeability=0))
        self.assertFalse(model.validate().is_valid)
        model=lid_model('VS'); model.lid_usage.update('lid-usage-1',width=0)
        self.assertIn('lid.swale_width',{d.code for d in model.validate().errors})

    def test_disabled_records_atoi_truth_flags_and_optional_defaults(self):
        source = lid_model('RB',extra=False).to_document().text
        model = Model.from_document(InpDocument.from_text(source+'[LID_USAGE]\nS L 0 ignored ignored ignored ignored ignored "ignored.txt" NoNode\n'),strict=True)
        self.assertEqual(len(model.lid_usage),2); self.assertEqual(model.lid_usage['lid-usage-2'].number,0)
        self.assertFalse(model.file_uses())
        self.assertEqual(model.to_document().text,source+'[LID_USAGE]\nS L 0 ignored ignored ignored ignored ignored "ignored.txt" NoNode\n')
        self.assert_domain(model,portable(model))
        self.assert_domain(model,Model.from_document(portable(model).to_document(),strict=True))
        tokens=model.lid_usage['lid-usage-2'].parameters
        model.convert_units('CMS')
        self.assertEqual(model.lid_usage['lid-usage-2'].parameters,tokens)
        self.assertFalse(model.file_uses())
        model = Model.from_document(InpDocument.from_text(source+'[LID_USAGE]\nS L 2.7 10 0 0 0 3 * * 0\n'),strict=True)
        self.assertEqual(model.lid_usage['lid-usage-2'].number,2)
        self.assertTrue(model.lid_usage['lid-usage-2'].to_pervious)
        self.assertIn('lid.barrel_cover_default',{d.code for d in model.validate().diagnostics})

    def test_unknown_variants_and_obsolete_source_resource_preflight(self):
        @dataclass(frozen=True,kw_only=True)
        class FutureSoil(l.LidSoil):
            unknown: float=1
        model=lid_model(); row=control().soil
        model.lid_controls.update('L',soil=FutureSoil(**row.__dict__))
        self.assertFalse(model.validate().is_valid)
        base=lid_model().to_document().text
        source=base+'[CURVES]\nOld CONTROL 0 1\n[LID_CONTROLS]\nL DRAIN .2 .5 1 2 0 0 Old\nL DRAIN .2 .5 1 2\n'
        model=Model.from_document(InpDocument.from_text(source),strict=True); model.curves.remove('Old')
        self.assertTrue(model.validate().is_valid)
        self.assertIn('lid.source_reference',{d.code for d in model.validate(for_run=True).errors})
        self.assertTrue(model.validate(for_run=True,normalize=True).is_valid)
        for changes in (dict(drain_to=ref('nodes','*')),dict(report_file=FileReference(path='*',direction='output'))):
            draft=lid_model(); draft.lid_usage.update('lid-usage-1',**changes)
            self.assertFalse(draft.validate(for_run=True).is_valid)


if __name__ == '__main__':
    unittest.main()
