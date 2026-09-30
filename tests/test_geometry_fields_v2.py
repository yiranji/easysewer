"""Cross-section syntax, configured semantics and source lifecycle."""

from dataclasses import dataclass, fields, replace
import unittest

from easysewer.io.inp import InpDocument
from easysewer.io.inp.geometry import CrossSectionCodec, GeometrySyntax
from easysewer.model import Model, Ref
from easysewer.model import geometry as g
from easysewer.validation import ValidationError
from test_native_v2_network import CASES, RESOURCES
from test_network_fields_v2 import source, load, NODE, PIPE
from test_scenario_v2 import portable


def queries(model):
    shape = model.links['P'].section.geometry
    paths = [('section', name) for name in ('geometry', 'barrels', 'culvert')]
    paths += [('section', 'geometry', field.name) for field in fields(shape)]
    return tuple(model.inspect_field(PIPE, path) for path in paths)


class GeometryFieldTests(unittest.TestCase):
    def test_all_builtin_shapes_have_nested_sources_units_and_semantics(self):
        for units in ('CFS', 'GPM', 'MGD', 'CMS', 'LPS', 'MLD'):
            for kind, parameters in CASES.items():
                with self.subTest(units=units, kind=kind):
                    model = load(source(units=units, section=kind+' '+parameters)+RESOURCES.get(kind, ''))
                    for info in queries(model):
                        self.assertIn(info.provenance.status, ('explicit', 'derived', 'omitted'))
                        self.assertIn(info.semantics.effective.status, ('known', 'not_applicable'))
                        self.assertNotEqual(info.semantics.unit.status, 'unknown')
                    self.assertEqual(model.inspect_field(NODE, 'max_depth').semantics.effective.status, 'known')
                    self.assertEqual(queries(Model.from_json_document(model.to_json_document(), strict=True)), queries(model))

    def test_standard_code_precedence_dimensions_and_ignored_tokens(self):
        for kind, depth, width in (('HORIZ_ELLIPSE', 22/12, 34/12), ('VERT_ELLIPSE', 34/12, 22/12), ('ARCH', 15.5/12, 26/12)):
            for parameters, token in (('3 0 99 0', '3'), ('99 88 3 0', '3')):
                model = load(source(section=kind+' '+parameters))
                info = model.inspect_field(PIPE, ('section','geometry','full_depth'))
                self.assertIsNone(info.value)
                self.assertEqual(info.semantics.effective.value, depth)
                self.assertEqual(info.provenance.status, 'derived')
                self.assertEqual(model.inspect_field(PIPE, ('section','geometry','width')).semantics.effective.value, width)
                declared = model.field_provenance(PIPE, ('section','geometry','size_code'))
                self.assertEqual(declared.declarations[0].tokens[0].raw, token)
                self.assertEqual(len(info.provenance.declarations), 2 if parameters.startswith('99') else 1)
                if parameters.startswith('99'):
                    self.assertFalse(info.provenance.declarations[1].contributes)
                    self.assertEqual(info.provenance.declarations[1].role, 'retained')
                model.convert_units('CMS')
                self.assertAlmostEqual(model.inspect_field(PIPE, ('section','geometry','full_depth')).semantics.effective.value, depth*.3048)
                self.assertEqual(model.links['P'].section.geometry.size_code, 3)

    def test_out_of_range_codes_are_invalid_and_export_is_rejected(self):
        for kind, maximum in (('HORIZ_ELLIPSE',23), ('VERT_ELLIPSE',23), ('ARCH',102)):
            model = load(source(section=f'{kind} {maximum+1} 0 0 0'))
            self.assertEqual(model.inspect_field(NODE, 'max_depth').semantics.effective.status, 'invalid')
            self.assertEqual(model.inspect_field(PIPE, ('section','geometry','size_code')).semantics.effective.status, 'invalid')
            self.assertIn('geometry.standard_size', {d.code for d in model.validate().errors})
            with self.assertRaises(ValidationError):
                model.to_document()

    def test_conditional_defaults_and_force_main_units(self):
        model = load(source(section='RECT_OPEN 2 3 0 0'))
        for name, wanted in (('barrels',1), ('culvert',0)):
            info=model.inspect_field(PIPE, ('section',name))
            self.assertIsNone(info.value)
            self.assertEqual(info.provenance.status,'omitted')
            self.assertEqual(info.semantics.default.value,wanted)
            self.assertEqual(info.semantics.effective.value,wanted)
        for kind, field, width in (('RECT_ROUND','bottom_radius',3), ('MODBASKETHANDLE','top_radius',3)):
            info=load(source(section=kind+' 2 3 0 0')).inspect_field(PIPE,('section','geometry',field))
            self.assertEqual(info.value,0)
            self.assertEqual(info.semantics.effective.value,width/2)
        for units in ('CFS','CMS'):
            model=load(source(units=units,section='FORCE_MAIN 1 120 0 0'))
            path=('section','geometry','roughness')
            self.assertEqual(model.inspect_field(PIPE,path).semantics.unit.value,'1')
            model.reinterpret_force_main_equation('D-W')
            self.assertEqual(model.inspect_field(PIPE,path).semantics.unit.value,'in' if units=='CFS' else 'mm')

    def test_required_slots_and_nonportable_native_integer_values(self):
        model=load(source(section='RECT_OPEN 2 3 0 0'))
        info=model.inspect_field(PIPE,('section','geometry','ignored_sides'))
        self.assertEqual(info.semantics.default.status,'required')
        self.assertEqual(info.semantics.effective.value,0)
        for count,expected in ((1,'known'),(127,'known'),(128,'unknown'),(256,'unknown'),(257,'unknown')):
            model=load(source(section=f'CIRCULAR 1 0 0 0 {count}'))
            info=model.inspect_field(PIPE,('section','barrels'))
            self.assertEqual(info.value,count)
            self.assertEqual(info.semantics.effective.status,expected)
            self.assertEqual(info.provenance.status,'explicit')
        model=load(source(section='CIRCULAR 1 0 0 0 1 2147483648'))
        self.assertEqual(model.inspect_field(PIPE,('section','culvert')).semantics.effective.status,'unknown')
        model=load(source(section='ARCH 2 3 0 0'))
        self.assertEqual(model.inspect_field(PIPE,('section','geometry','size_code')).semantics.default.status,'not_applicable')

    def test_regulator_ignored_columns_are_retained_without_contributing(self):
        text='[JUNCTIONS]\nJ 10 5\n[OUTFALLS]\nO 9 FREE\n[WEIRS]\nP J O TRANSVERSE 0 3\n[XSECTIONS]\nP RECT_OPEN 2 3 1.5 0 99 55\n'
        model=load(text)
        for path in (('section','barrels'), ('section','culvert'), ('section','geometry','ignored_sides')):
            info=model.inspect_field(PIPE,path)
            self.assertEqual(info.semantics.effective.status,'not_applicable')
            self.assertEqual(len(info.provenance.declarations),1)
            self.assertEqual(info.provenance.declarations[0].role,'retained')
            self.assertFalse(info.provenance.declarations[0].contributes)
        self.assertEqual(model.inspect_field(PIPE,('section','geometry','width')).semantics.effective.value,3)

    def test_resources_invalid_variants_and_numerical_depth_collapse(self):
        for kind, field, namespace in (('IRREGULAR','transect','swmm:transects'), ('STREET','street','swmm:streets'), ('CUSTOM','curve','swmm:curves')):
            model=load(source(section=kind+' '+CASES[kind]))
            self.assertEqual(model.inspect_field(NODE,'max_depth').semantics.effective.status,'invalid')
            declared=model.field_provenance(PIPE,('section','geometry',field,'collection'))
            self.assertEqual(declared.status,'derived')
            self.assertEqual(declared.value.value,namespace)
        model=load(source(section='IRREGULAR Transect1')+RESOURCES['IRREGULAR'])
        model.transects.update('Transect1',elevation_offset=1.e20)
        self.assertEqual(model.inspect_field(NODE,'max_depth').semantics.effective.status,'invalid')
        model=load(source(section='STREET Street1')+RESOURCES['STREET'])
        model.streets.update('Street1',gutter_width=6)
        self.assertEqual(model.inspect_field(NODE,'max_depth').semantics.effective.status,'invalid')

    def test_lifecycle_and_unknown_extension_geometry(self):
        model=load(source(section='ARCH 3 0 0 0'))
        before=queries(model)
        with self.assertRaises(RuntimeError):
            with model.transaction():
                model.links.update('P',section=g.CrossSection(geometry=g.Circular(diameter=2)))
                raise RuntimeError('rollback')
        self.assertEqual(queries(model),before)
        model.links.rename('P','Other')
        self.assertEqual(model.field_provenance(Ref(collection='swmm:links',key='Other'),('section','geometry','size_code')), replace(before[5].provenance,owner=Ref(collection='swmm:links',key='Other')))
        rebuilt=Model.from_json_document(model.to_json_document(),strict=True)
        self.assertEqual(rebuilt.links['Other'],model.links['Other'])
        self.assertEqual(portable(model).field_provenance(Ref(collection='swmm:links',key='Other'),('section','geometry','size_code')).status,'untracked')
        @dataclass(frozen=True,kw_only=True)
        class Extension(g.Geometry):
            kind='EXTRA'
            second: float
            first: float
        codec=CrossSectionCodec((GeometrySyntax(kind='EXTRA',record_type=Extension,fields=('first','second')),))
        _,assignments=codec.field_layout(('EXTRA','2','3','0','0'))
        self.assertIn((('geometry','first'),(1,),'value',True),assignments)
        model=load();model.links.update('P',section=codec.parse(('EXTRA','2','3','0','0')))
        self.assertEqual(model.inspect_field(NODE,'max_depth').semantics.effective.status,'unknown')

    def test_invalid_duplicate_section_blocks_nested_completeness(self):
        for row in ('P CIRCULAR bad 0 0 0', 'P CIRCULAR 5 0 0 0'):
            model=load(source()+'[XSECTIONS]\n'+row+'\n')
            self.assertEqual(model.field_provenance(PIPE,('section','geometry','diameter')).status,'unknown')


if __name__ == '__main__':
    unittest.main()
