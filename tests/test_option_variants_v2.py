"""Explicit native option alternatives, including disabled normal-flow limits."""
from dataclasses import replace
import unittest

from easysewer.io.inp import InpDocument
from easysewer.io.json import JsonDocument
from easysewer.model import Model, Ref
from easysewer.model.geometry import Circular, CrossSection
from easysewer.model.network import Junction
from easysewer.scenario import FieldChange, ScenarioPatch, SetFields
from easysewer.validation import ValidationError
from test_options_v2 import network

EVIDENCE = []
# Independent spelling inventory reviewed against tagged keywords.c/text.h.
CHOICES = {
    'FLOW_UNITS': ('CFS', 'GPM', 'MGD', 'CMS', 'LPS', 'MLD'),
    'INFILTRATION': ('HORTON', 'MODIFIED_HORTON', 'GREEN_AMPT', 'MODIFIED_GREEN_AMPT', 'CURVE_NUMBER'),
    'FLOW_ROUTING': ('NONE', 'STEADY', 'KINWAVE', 'XKINWAVE', 'DYNWAVE', 'NF', 'KW', 'EKW', 'DW'),
    'LINK_OFFSETS': ('DEPTH', 'ELEVATION'),
    'FORCE_MAIN_EQUATION': ('H-W', 'D-W'),
    'INERTIAL_DAMPING': ('NONE', 'PARTIAL', 'FULL'),
    'NORMAL_FLOW_LIMITED': ('SLOPE', 'FROUDE', 'BOTH', 'NONE'),
    'SURCHARGE_METHOD': ('EXTRAN', 'SLOT'),
}
BOOLEANS = ('IGNORE_RAINFALL', 'IGNORE_SNOWMELT', 'IGNORE_GROUNDWATER', 'IGNORE_RDII',
            'IGNORE_ROUTING', 'IGNORE_QUALITY', 'ALLOW_PONDING', 'SKIP_STEADY_STATE', 'SLOPE_WEIGHTING')
OWNER = Ref(collection='swmm:options', key='settings')


def limitation_model():
    """Partially full conduit between unequal initial depths exercises the cap."""
    m = network(); m.nodes.update('J', initial_depth=.3); m.nodes.update('O', elevation=7)
    m.nodes.add(Junction(id='K', elevation=8, max_depth=5, initial_depth=2))
    m.links.update('P', outlet=Ref(collection='swmm:nodes', key='K'), inlet_offset=0, outlet_offset=0,
        initial_flow=0, maximum_flow=0, losses=None, section=CrossSection(geometry=Circular(diameter=4)))
    m.links.add(replace(m.links['P'], id='Down', inlet=Ref(collection='swmm:nodes', key='K'),
        outlet=Ref(collection='swmm:nodes', key='O')))
    return m


class OptionVariantTests(unittest.TestCase):
    def test_all_native_categorical_boolean_and_compatibility_spellings(self):
        cases = [(k, v) for k, values in CHOICES.items() for v in values]
        cases += [(k, v) for k in BOOLEANS for v in ('YES', 'NO')]
        cases += [('COMPATIBILITY', v) for v in ('3', '4', '5')]
        for keyword, token in cases:
            with self.subTest(keyword=keyword, token=token):
                text = f'[OPTIONS]\n{keyword} {token.lower()} ; keep\n'
                m = Model.from_document(InpDocument.from_text(text), strict=True)
                self.assertEqual(m.to_document().text, text)
                self.assertFalse(m.support.opaque_records)
                q = Model.from_document(m.to_document(normalize=True), strict=True)
                self.assertEqual(q.options, m.options)
                data = m.to_json_document().data; data.pop('source', None)
                restored = Model.from_json_document(JsonDocument.from_data(data), strict=True)
                self.assertEqual(restored.options, m.options)
                if keyword == 'FLOW_ROUTING' and token == 'NONE':
                    self.assertTrue(m.options.ignore_routing)
                    self.assertEqual(m.options.flow_routing, 'DYNWAVE')
                EVIDENCE.append(dict(kind='option-spelling', keyword=keyword, token=token))

    def test_none_is_distinct_from_omission_and_default(self):
        m = limitation_model()
        self.assertIsNone(m.options.normal_flow_limited)
        self.assertEqual(m.effective_options.values.normal_flow_limited, 'BOTH')
        m.update_options(normal_flow_limited='NONE')
        p = Model.from_document(InpDocument.from_text(m.to_document().text, source='normal-flow.inp'), strict=True)
        fact = p.inspect_field(OWNER, 'normal_flow_limited')
        self.assertEqual(fact.semantics.default.value, 'BOTH')
        self.assertEqual(fact.semantics.effective.value, 'NONE')
        self.assertEqual(fact.provenance.status, 'explicit')
        self.assertEqual(fact.provenance.declarations[0].tokens[0].value, 'NONE')
        for current in (p.copy(), Model.from_json_document(p.to_json_document(), strict=True)):
            self.assertEqual(current.inspect_field(OWNER, 'normal_flow_limited'), fact)
            current.update_options(normal_flow_limited=None)
            self.assertNotIn('NORMAL_FLOW_LIMITED', current.to_document().text)
            self.assertEqual(current.effective_options.values.normal_flow_limited, 'BOTH')

    def test_last_assignment_edit_and_scenario_remain_explicit(self):
        text = '[OPTIONS]\nNORMAL_FLOW_LIMITED BOTH\nNORMAL_FLOW_LIMITED NONE\n'
        m = Model.from_document(InpDocument.from_text(text), strict=True)
        self.assertEqual(m.options.normal_flow_limited, 'NONE')
        self.assertEqual(m.to_document().text, text)
        patch = ScenarioPatch(operations=(SetFields(target=OWNER,
            changes=(FieldChange(name='normal_flow_limited', value='SLOPE'),)),))
        changed = ScenarioPatch.from_json_document(patch.to_json_document()).apply(m).model
        self.assertEqual(changed.options.normal_flow_limited, 'SLOPE')
        self.assertEqual(m.options.normal_flow_limited, 'NONE')
        self.assertEqual(len(changed.to_document().records('OPTIONS')), 1)
        changed.update_options(normal_flow_limited='NONE')
        self.assertEqual(Model.from_document(changed.to_document(), strict=True).options.normal_flow_limited, 'NONE')

    def test_unknown_choice_remains_unclaimed_and_transaction_rolls_back(self):
        text = '[OPTIONS]\nNORMAL_FLOW_LIMITED FUTURE\n'
        imported = Model.from_document(InpDocument.from_text(text))
        self.assertEqual(imported.document.text, text)
        self.assertTrue(imported.support.opaque_records)
        m = limitation_model(); m.update_options(normal_flow_limited='NONE')
        before = m.to_json_document().to_bytes()
        with self.assertRaises(ValidationError), m.transaction():
            m.update_options(normal_flow_limited='FUTURE')
        self.assertEqual(m.to_json_document().to_bytes(), before)


if __name__ == '__main__': unittest.main()
