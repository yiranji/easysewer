"""Hydraulic option contexts, independent numeric values and transactional edits."""
from dataclasses import replace
from datetime import timedelta
import unittest
from easysewer.model import Model
from easysewer.model.geometry import CrossSection, ForceMain
from easysewer.validation import ValidationError
from test_option_effects_v2 import OWNER, UNITS, fixture, load


def hydraulic_fixture(units='CFS'):
    model=fixture('allow_ponding',units)
    model.update_options(variable_step=.75)
    return model


def numeric_cases(units):
    length=1 if units in UNITS[:3] else .3048
    return (
        ('lengthening_step','LENGTHENING_STEP',(0,10),'seconds'),
        ('min_slope','MIN_SLOPE',(0,2),'number'),
        ('variable_step','VARIABLE_STEP',(0,.25,.75,2),'number'),
        ('minimum_step','MINIMUM_STEP',(.001,5,10),'seconds'),
        ('max_trials','MAX_TRIALS',(1,8,20),'integer'),
        ('head_tolerance','HEAD_TOLERANCE',tuple(v*length for v in (-1,.0001,.005,1)),'number'),
        ('min_surface_area','MIN_SURFAREA',tuple(v*length**2 for v in (1,12.566,1000)),'number'),
    )


def public_value(value,kind):
    return timedelta(seconds=value) if kind=='seconds' else value


def force_fixture(equation,units='CFS'):
    model=hydraulic_fixture(units)
    length=1 if units in UNITS[:3] else .3048
    roughness=120 if equation=='H-W' else (.01 if units in UNITS[:3] else .254)
    model.reinterpret_force_main_equation(equation)
    for key in ('P','Q'):
        model.links.update(key,section=CrossSection(geometry=ForceMain(diameter=length,roughness=roughness)))
    return model


def literal_force_source(equation,units):
    # Construct native tokens without using the ForceMain codec or transforms.
    source=hydraulic_fixture(units).to_document().text
    length=1 if units in UNITS[:3] else .3048
    roughness=120 if equation=='H-W' else (.01 if units in UNITS[:3] else .254)
    lines=source.splitlines()
    for i,line in enumerate(lines):
        parts=line.split()
        if len(parts)>1 and parts[0] in ('P','Q') and parts[1]=='CIRCULAR':
            lines[i]=f'{parts[0]} FORCE_MAIN {length:.17g} {roughness:.17g} 0 0 1'
    return '\n'.join(lines)+'\n[OPTIONS]\nFORCE_MAIN_EQUATION '+equation+'\n'


class HydraulicOptionTests(unittest.TestCase):
    def test_numeric_options_ordered_sources_edit_clear_and_rollback(self):
        for units in UNITS:
            for field,keyword,values,kind in numeric_cases(units):
                with self.subTest(units=units,field=field):
                    base=hydraulic_fixture(units);base.update_options(**{field:None})
                    text=base.to_document().text+'[OPTIONS]\n'+keyword+' '+str(values[0])+' ; first\n'+keyword+' '+str(values[-1])+' ; last\n'
                    model=load(text)
                    self.assertEqual(getattr(model.options,field),public_value(values[-1],kind))
                    self.assertEqual([d.contributes for d in model.field_provenance(OWNER,field).declarations],[False,True])
                    before=model.to_json_document().to_bytes()
                    with self.assertRaises(RuntimeError):
                        with model.transaction():
                            model.update_options(**{field:public_value(values[0],kind)})
                            raise RuntimeError('rollback')
                    self.assertEqual(model.to_json_document().to_bytes(),before)
                    for value in values:
                        model.update_options(**{field:public_value(value,kind)})
                        restored=Model.from_json_document(model.to_json_document(),strict=True)
                        self.assertEqual(load(restored.to_document(normalize=True).text).options,model.options)
                    model.update_options(**{field:None})
                    self.assertIsNone(getattr(model.options,field))
                    self.assertIn(field,model.effective_options.defaults_used)
                    self.assertEqual(load(model.to_document().text).options,model.options)

    def test_force_equation_reinterpretation_preserves_numbers_and_requires_explicit_context(self):
        for units in UNITS:
            for equation in ('H-W','D-W'):
                with self.subTest(units=units,equation=equation):
                    model=force_fixture(equation,units);opposite='D-W' if equation=='H-W' else 'H-W'
                    before=model.to_json_document().to_bytes();links=tuple(model.links.values())
                    with self.assertRaises(ValidationError):model.update_options(force_main_equation=opposite)
                    self.assertEqual(model.to_json_document().to_bytes(),before)
                    with self.assertRaises(RuntimeError):
                        with model.transaction():
                            model.reinterpret_force_main_equation(opposite)
                            raise RuntimeError('rollback')
                    self.assertEqual(model.to_json_document().to_bytes(),before)
                    model.reinterpret_force_main_equation(opposite)
                    self.assertEqual(tuple(model.links.values()),links)
                    restored=Model.from_json_document(model.to_json_document(),strict=True)
                    self.assertEqual(load(restored.to_document(normalize=True).text).options,model.options)
                    model.reinterpret_force_main_equation(None)
                    self.assertIsNone(model.options.force_main_equation)
                    self.assertEqual(model.effective_options.values.force_main_equation,'H-W')
                    self.assertEqual(tuple(model.links.values()),links)

    def test_offset_conversion_and_reinterpretation_have_separate_meanings(self):
        for units in UNITS:
            with self.subTest(units=units):
                model=hydraulic_fixture(units);length=1 if units in UNITS[:3] else .3048
                model.links.update('P',inlet_offset=.5*length,outlet_offset=.25*length)
                before=model.to_json_document().to_bytes();pipe=model.links['P']
                with self.assertRaises(ValidationError):model.update_options(link_offsets='ELEVATION')
                self.assertEqual(model.to_json_document().to_bytes(),before)
                with self.assertRaises(RuntimeError):
                    with model.transaction():
                        model.convert_link_offsets('ELEVATION')
                        raise RuntimeError('rollback')
                self.assertEqual(model.to_json_document().to_bytes(),before)
                model.convert_link_offsets('ELEVATION')
                self.assertEqual(model.links['P'].inlet_offset,model.nodes['J'].elevation+pipe.inlet_offset)
                self.assertEqual(model.links['P'].outlet_offset,model.nodes['K'].elevation+pipe.outlet_offset)
                restored=Model.from_json_document(model.to_json_document(),strict=True)
                self.assertEqual(load(restored.to_document(normalize=True).text).options,model.options)
                model.convert_link_offsets('DEPTH')
                self.assertAlmostEqual(model.links['P'].inlet_offset,pipe.inlet_offset,places=12)
                self.assertAlmostEqual(model.links['P'].outlet_offset,pipe.outlet_offset,places=12)
                numbers=tuple(model.links.values());model.reinterpret_link_offsets('ELEVATION')
                self.assertEqual(tuple(model.links.values()),numbers)


if __name__=='__main__':unittest.main()
