"""Network input declarations versus configured and graph-adjusted values."""
from dataclasses import replace
import unittest

from easysewer.io.inp import InpDocument
from easysewer.model import Model, Ref, Point
from easysewer.model import geometry as g, network as n
from test_scenario_v2 import portable

NODE = Ref(collection='swmm:nodes', key='J')
PIPE = Ref(collection='swmm:links', key='P')


def source(tail='', mode='DEPTH', offsets=('0', '0'), section='CIRCULAR 1 0 0 0', units='CFS'):
    return (f'[OPTIONS]\nFLOW_UNITS {units}\nLINK_OFFSETS {mode}\nSTART_DATE 01/01/2020\n'
            'END_DATE 01/01/2020\nEND_TIME 00:15:00\nREPORT_STEP 00:01:00\nROUTING_STEP 5\n'
            f'[JUNCTIONS]\nJ 10{tail}\n[OUTFALLS]\nO 9 FREE\n'
            f'[CONDUITS]\nP J O 100 .013 {offsets[0]} {offsets[1]}\n[XSECTIONS]\nP {section}\n')


def load(text=None):
    return Model.from_document(InpDocument.from_text(source() if text is None else text, source='network.inp'))


class NetworkFieldTests(unittest.TestCase):
    def test_omission_explicit_zero_and_engine_crown_are_distinct(self):
        for tail, status, wanted in (('', 'omitted', 1), (' 0', 'explicit', 1), (' .25', 'explicit', 1), (' 5', 'explicit', 5)):
            model = load(source(tail))
            info = model.inspect_field(NODE, 'max_depth')
            self.assertEqual(info.provenance.status, status)
            self.assertEqual(info.semantics.default.value, 0)
            self.assertEqual(info.semantics.effective.value, wanted)
            self.assertEqual(model.inspect_field(NODE, 'initial_depth').semantics.effective.value, 0)
            self.assertEqual(model.inspect_field(PIPE, 'initial_flow').semantics.default.value, 0)
            self.assertEqual(model.inspect_field(PIPE, 'inlet_offset').semantics.default.status, 'required')
        info = load().inspect_field(NODE, 'max_depth')
        self.assertIsNone(info.constructor_default.value)
        self.assertIsNone(info.value)
        self.assertEqual(info.provenance.declarations, ())

    def test_units_offsets_markers_and_filled_geometry(self):
        for units in ('CFS', 'GPM', 'MGD', 'CMS', 'LPS', 'MLD'):
            for mode, offsets, expected in (('DEPTH', ('-1', '-1'), 1), ('ELEVATION', ('*', '*'), 1),
                                            ('ELEVATION', ('9', '8'), 1), ('ELEVATION', ('10.5', '9.5'), 1.5)):
                model = load(source(mode=mode, offsets=offsets, units=units))
                self.assertEqual(model.inspect_field(NODE, 'max_depth').semantics.effective.value, expected)
                self.assertEqual(model.inspect_field(PIPE, 'inlet_offset').semantics.effective.value, expected-1)
                self.assertEqual(model.inspect_field(PIPE, 'length').semantics.unit.value, 'ft' if units in ('CFS','GPM','MGD') else 'm')
                self.assertEqual(model.inspect_field(PIPE, 'initial_flow').semantics.unit.value, units)
                if offsets[0] == '*':
                    self.assertEqual(model.field_provenance(PIPE, 'inlet_offset').declarations[0].role, 'marker')
        filled = load(source(section='FILLED_CIRCULAR 2 .5 0 0'))
        self.assertEqual(filled.inspect_field(PIPE, 'inlet_offset').semantics.effective.value, .5)
        self.assertEqual(filled.inspect_field(NODE, 'max_depth').semantics.effective.value, 2)

    def test_exact_tokens_repeated_coordinates_losses_and_indexed_paths(self):
        model = load(source()+'[COORDINATES]\nJ 1 2\nJ 3 4\n[VERTICES]\nP 5 6\nP 5 6\n'
                     '[LOSSES]\nP 0 .2 .3\n[MAP]\nUNITS METERS\n')
        info = model.inspect_field(NODE, ('position','x'))
        self.assertEqual(info.semantics.unit.value, 'm')
        self.assertEqual([d.tokens[0].raw for d in info.provenance.declarations], ['1','3'])
        self.assertEqual([d.contributes for d in info.provenance.declarations], [False,True])
        self.assertEqual(model.field_provenance(PIPE, ('inlet','collection')).status, 'derived')
        self.assertEqual(model.field_provenance(PIPE, ('inlet','key')).declarations[0].tokens[0].raw, 'J')
        loss = model.inspect_field(PIPE, ('losses','flap_gate'))
        self.assertEqual(loss.provenance.status, 'omitted')
        self.assertIs(loss.semantics.default.value, False)
        self.assertIs(model.inspect_field(PIPE,'losses').semantics.effective.value.flap_gate,False)
        self.assertEqual(model.inspect_field(PIPE,'losses').semantics.effective.value.seepage,0)
        self.assertEqual(model.field_provenance(PIPE, ('losses','exit')).declarations[0].tokens[0].raw, '.2')
        self.assertEqual(model.field_provenance(PIPE, ('vertices',0,'x')).status, 'explicit')
        self.assertEqual(model.inspect_field(PIPE, ('vertices',0,'x')).provenance.status, 'untracked_path')
        self.assertEqual(len(model.field_provenance(PIPE,'vertices').declarations), 2)
        self.assertEqual(model.field_provenance(PIPE,'section').status, 'explicit')

    def test_invalid_lines_and_duplicate_entities_block_false_omission(self):
        model = load(source()+'[JUNCTIONS]\nJ 11\n[LOSSES]\nP bad 0 0\n[COORDINATES]\nJ bad 4\n')
        for owner, name in ((NODE,'max_depth'),(NODE,'position'),(PIPE,'losses')):
            self.assertEqual(model.field_provenance(owner,name).status, 'unknown')
        self.assertEqual(model.field_provenance(PIPE,'length').status, 'explicit')

    def test_unknown_preprocessing_and_invalid_graph_do_not_invent_effective_values(self):
        for section, expected in (('HORIZ_ELLIPSE 1 0 0 0', 14/12), ('DUMMY 0 0 0 0', 1.e-6)):
            self.assertEqual(load(source(section=section)).inspect_field(NODE,'max_depth').semantics.effective.value, expected)
        model = load(source(mode='DEPTH', offsets=('*','0')))
        self.assertEqual(model.inspect_field(PIPE,'inlet_offset').semantics.effective.status, 'invalid')
        model = load(); model.links.update('P', inlet=Ref(collection='swmm:nodes',key='missing'))
        self.assertEqual(model.inspect_field(PIPE,'inlet').semantics.effective.status, 'invalid')
        model.links.update('P', inlet=NODE, outlet=Ref(collection='swmm:nodes',key='missing'))
        self.assertEqual(model.inspect_field(NODE,'max_depth').semantics.effective.status, 'invalid')
        model.links.update('P', outlet=Ref(collection='swmm:nodes',key='O'), length=-1)
        self.assertEqual(model.inspect_field(PIPE,'length').semantics.effective.status, 'invalid')

    def test_lifecycle_rename_recreate_rollback_json_and_units(self):
        model = load(source()+'[COORDINATES]\nJ 1 2\n[MAP]\nUNITS FEET\n')
        before = model.inspect_field(PIPE,('inlet','key'))
        with self.assertRaises(RuntimeError):
            with model.transaction():
                model.nodes.rename('J','Other')
                raise RuntimeError('rollback')
        self.assertEqual(model.inspect_field(PIPE,('inlet','key')),before)
        model.nodes.rename('J','Renamed')
        info = model.inspect_field(PIPE,('inlet','key'))
        self.assertTrue(info.changed); self.assertEqual(info.provenance,before.provenance)
        rebuilt = Model.from_json_document(model.to_json_document(), strict=True)
        self.assertEqual(rebuilt.inspect_field(PIPE,('inlet','key')), info)
        self.assertEqual(portable(model).inspect_field(PIPE,'length').provenance.status, 'untracked')
        renamed = Ref(collection='swmm:nodes',key='Renamed')
        model.convert_units('CMS')
        self.assertAlmostEqual(model.inspect_field(renamed,'max_depth').semantics.effective.value,.3048)
        self.assertEqual(model.inspect_field(renamed,('position','x')).semantics.unit.value,'ft')
        old=model.links['P']; model.links.remove('P'); model.links.add(old)
        self.assertEqual(model.inspect_field(PIPE,'length').provenance.status,'created')

    def test_ponding_and_omitted_losses_are_explicit(self):
        model=load(source(' 5 0 0 4'))
        self.assertEqual(model.inspect_field(NODE,'ponded_area').semantics.effective.status,'not_applicable')
        model.update_options(allow_ponding=True)
        self.assertEqual(model.inspect_field(NODE,'ponded_area').semantics.effective.value,4)
        losses=model.inspect_field(PIPE,'losses')
        self.assertEqual(losses.provenance.status,'omitted')
        self.assertEqual(losses.semantics.effective.value,n.ConduitLosses(entry=0,exit=0,average=0,flap_gate=False,seepage=0))


if __name__ == '__main__':
    unittest.main()
