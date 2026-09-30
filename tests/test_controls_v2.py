from dataclasses import fields, replace
from datetime import timedelta
import unittest

from easysewer.io.inp import InpDocument
from easysewer.io.inp.expressions import ExpressionCodec
from easysewer.model import Model, Ref
from easysewer.model import controls as c
from easysewer.model.control_rules import expression_dimension
from easysewer.model.resources import Curve, CurvePoint
from easysewer.validation import ValidationError
from test_options_v2 import network
from test_regulators_v2 import regulator_model


def symbol(kind, name):
    return Ref(collection="swmm:controls", key=(kind,name))


def rebuild(model):
    result = Model()
    result.update_options(**{field.name:getattr(model.options,field.name) for field in fields(model.options)})
    for spec in model._store.specifications:
        if spec.key == "swmm:options":
            continue
        for record in model.collection(spec.key).values():
            result.collection(spec.key).add(record)
    return result


def controlled(text, *, model=None):
    return Model.from_document(InpDocument.from_text((model or network()).to_document().text+'[CONTROLS]\n'+text), strict=True)


PROGRAM = '''VARIABLE DepthValue = NODE J DEPTH
VARIABLE RateValue = LINK P FLOW
EXPRESSION AverageRate = (RateValue + RateValue) / 2
RULE first
IF DepthValue > 1
OR NODE J DEPTH > NODE O DEPTH
AND AverageRate >= 0.1
THEN CONDUIT P STATUS = CLOSED
ELSE CONDUIT P STATUS = OPEN
PRIORITY 2
RULE second
IF SIMULATION TIME >= 00:00:20
THEN CONDUIT P STATUS = OPEN
'''


class ControlTests(unittest.TestCase):
    def test_program_roundtrip_source_order_and_rebuild(self):
        model = controlled(PROGRAM)
        self.assertEqual(model.to_document(), model.document)
        self.assertFalse(model.support.opaque_records)
        self.assertEqual([row.conjunction for row in model.controls[("RULE","first")].conditions], ["IF","OR","AND"])
        parsed = Model.from_document(rebuild(model).to_document(),strict=True)
        self.assertEqual(list(parsed.controls.values()),list(model.controls.values()))

    def test_composite_statement_rename_propagates_through_arithmetic_and_rules(self):
        model = controlled(PROGRAM)
        model.controls.rename(("VARIABLE","RateValue"),"FlowValue")
        model.controls.rename(("EXPRESSION","AverageRate"),"MeanFlow")
        model.controls.rename(("RULE","first"),"Renamed")
        model.nodes.rename("J","Upstream")
        model.links.rename("P","Pipe")
        parsed = Model.from_document(model.to_document(),strict=True)
        self.assertEqual(parsed.controls[("VARIABLE","DepthValue")].value.target.key,"Upstream")
        self.assertEqual(parsed.controls[("VARIABLE","FlowValue")].value.target.key,"Pipe")
        self.assertEqual(parsed.controls[("RULE","Renamed")].conditions[-1].left.reference,symbol("EXPRESSION","MeanFlow"))
        with self.assertRaises(ValidationError):
            model.controls.remove(("VARIABLE","FlowValue"))

    def test_rule_and_variable_can_share_id_without_namespace_collision(self):
        model = controlled('VARIABLE Same = NODE J DEPTH\nRULE Same\nIF Same > 0\nTHEN CONDUIT P STATUS = OPEN\n')
        model.controls.rename(("RULE","Same"),"Rule")
        self.assertEqual(model.controls[("RULE","Rule")].conditions[0].left.reference,symbol("VARIABLE","Same"))

    def test_move_is_ordered_transactional_and_preserves_first_rule_semantics(self):
        model = controlled(PROGRAM)
        model.controls.move(("RULE","second"),before=("RULE","first"))
        parsed = Model.from_document(model.to_document(),strict=True)
        self.assertEqual([r.id for r in parsed.controls.values() if isinstance(r,c.ControlRule)],["second","first"])
        before = tuple(model.controls)
        with self.assertRaises(KeyError):
            model.controls.move(("RULE","second"),before=("RULE","missing"))
        self.assertEqual(tuple(model.controls),before)
        with self.assertRaises(RuntimeError):
            with model.transaction():
                model.controls.move(("RULE","second"))
                raise RuntimeError("rollback")
        self.assertEqual(tuple(model.controls),before)

    def test_definitions_between_clauses_are_hoisted_without_reordering_rules(self):
        source = 'RULE R\nIF NODE J DEPTH >= 0\nVARIABLE V = NODE J DEPTH\nAND V > 0\nTHEN CONDUIT P STATUS = OPEN\n'
        model = controlled(source)
        parsed = Model.from_document(rebuild(model).to_document(),strict=True)
        self.assertEqual(parsed.controls[("RULE","R")],model.controls[("RULE","R")])
        self.assertEqual(next(iter(parsed.controls)),("VARIABLE","V"))

    def test_all_arithmetic_functions_power_association_and_signed_number_syntax(self):
        codec = ExpressionCodec()
        cases = [f"{f}(Value / 2)" for f in c.FUNCTIONS] + ['2^3^2','-2^2','- 2^2','-(Value*2)+3','Value / -2','Value ^ (-2)','1e-3 + .2']
        for source in cases:
            with self.subTest(source=source):
                node = codec.parse(source,lambda name:symbol("VARIABLE",name))
                self.assertEqual(codec.parse(codec.format(node),lambda name:symbol("VARIABLE",name)),node)
        self.assertIsInstance(codec.parse('-2^2',lambda name:None).left,c.ExpressionNumber)
        self.assertIsInstance(codec.parse('- 2^2',lambda name:None),c.UnaryExpression)
        for source in ('Value.__class__','__import__("os")','[1]','Value + - Other','Value junk','Value(1)'):
            with self.subTest(source=source),self.assertRaises(ValueError):
                codec.parse(source,lambda name:symbol("VARIABLE",name))

    def test_native_time_dayofyear_and_optional_priority(self):
        for attribute, token in (("TIME",".0004"),("CLOCKTIME","25:01:02"),("DATE","01/02/2020"),("DAYOFYEAR","03/01"),("DAYOFYEAR","60.5"),("DAY","1.5"),("MONTH","12")):
            model = controlled(f'RULE R\nIF SIMULATION {attribute} >= {token}\nTHEN CONDUIT P STATUS = OPEN\nPRIORITY 0\n')
            row=model.controls[("RULE","R")]
            self.assertEqual(row.priority,0)
            self.assertEqual(Model.from_document(rebuild(model).to_document()).controls[("RULE","R")],row)
        model = controlled('RULE R\nIF SIMULATION TIME > 00:00:01.9\nTHEN CONDUIT P STATUS = OPEN\n')
        self.assertEqual(model.controls[("RULE","R")].conditions[0].right.value,timedelta(seconds=1))

    def test_flow_expression_thresholds_and_control_curve_axes_convert_together(self):
        model = regulator_model("SIDE",storage=False)
        model.curves.add(Curve(id="Control",kind="CONTROL",points=(CurvePoint(x=0,y=.2),CurvePoint(x=2,y=.8))))
        model = controlled('VARIABLE Rate = LINK P FLOW\nEXPRESSION MeanRate = (Rate + Rate)/2\nRULE R\nIF MeanRate >= 1\nTHEN ORIFICE P SETTING = CURVE Control\n',model=model)
        model.convert_units("CMS")
        self.assertAlmostEqual(model.controls[("RULE","R")].conditions[0].right.value,.02832)
        self.assertAlmostEqual(model.curves["Control"].points[1].x,2*.02832)
        self.assertEqual(model.curves["Control"].points[1].y,.8)

    def test_unknown_expression_dimensions_fail_conversion_atomically(self):
        model = controlled('VARIABLE DepthValue = NODE J DEPTH\nEXPRESSION Offset = DepthValue + 2\nRULE R\nIF Offset > 1\nTHEN CONDUIT P STATUS = OPEN\n')
        before=model.to_document().text
        with self.assertRaises(ValidationError):
            model.convert_units("CMS")
        self.assertEqual(model.to_document().text,before)

    def test_dimensionless_wrappers_do_not_hide_unknown_expression_units(self):
        for expression in ('SGN(DepthValue - 2)', 'STEP(DepthValue - 2)', '(DepthValue - 2)^0'):
            with self.subTest(expression=expression):
                model=controlled(f'VARIABLE DepthValue = NODE J DEPTH\nEXPRESSION Value = {expression}\nRULE R\nIF Value > 0\nTHEN CONDUIT P STATUS = OPEN\n')
                before=model.to_document().text
                with self.assertRaises(ValidationError):
                    model.convert_units('CMS')
                self.assertEqual(model.to_document().text,before)

    def test_missing_link_age_cannot_guess_modulated_controller_units(self):
        for premise in ('ORIFICE P TIMEOPEN = 0', 'SIMULATION TIME >= ORIFICE P TIMECLOSED', 'AgeValue >= 0'):
            for setting in ('PID .1 1 .05', 'CURVE Control'):
                with self.subTest(premise=premise,setting=setting):
                    model=regulator_model('SIDE')
                    if setting.startswith('CURVE'):
                        model.curves.add(Curve(id='Control',kind='CONTROL',points=(CurvePoint(x=0,y=0),CurvePoint(x=1,y=1))))
                    model=controlled(f'VARIABLE Age = ORIFICE P TIMEOPEN\nEXPRESSION AgeValue = Age\nRULE R\nIF {premise}\nTHEN ORIFICE P SETTING = {setting}\nELSE ORIFICE P SETTING = {setting}\n',model=model)
                    before=model.to_document().text
                    with self.assertRaises(ValidationError):
                        model.convert_units('CMS')
                    self.assertEqual(model.to_document().text,before)

    def test_hoisted_symbol_shadowing_preserves_whole_source_program(self):
        programs=(
            'RULE R\nIF NODE J DEPTH > 0\nTHEN CONDUIT P STATUS = OPEN\nVARIABLE N = NODE O DEPTH\n',
            'VARIABLE Value = NODE J DEPTH\nRULE R\nIF Value > 0\nTHEN CONDUIT P STATUS = OPEN\nEXPRESSION V = 0\n',
            'RULE R\nIF CONDUIT P STATUS = OPEN\nTHEN CONDUIT P STATUS = OPEN\nVARIABLE O = NODE J DEPTH\n',
        )
        for source in programs:
            with self.subTest(source=source):
                model=controlled(source)
                self.assertEqual(len(model.controls),0)
                self.assertEqual(model.to_document(),model.document)
                self.assertIn('control.unsupported_program',{d.code for d in model.validate().diagnostics})

    def test_programmatic_symbol_conflicts_are_rejected_before_export(self):
        model=controlled('VARIABLE Value = NODE J DEPTH\nRULE R\nIF Value > 0\nTHEN CONDUIT P STATUS = OPEN\n')
        model.controls.add(c.ControlExpression(id='V',expression=c.ExpressionNumber(value=0)))
        self.assertIn('control.symbol_shadow',{d.code for d in model.validate().errors})
        with self.assertRaises(ValidationError):
            model.to_document()
        model.controls.remove(('EXPRESSION','V'))
        model.controls.add(c.ControlVariable(id='ValueLong',value=c.Attribute(object_type='NODE',attribute='DEPTH',target=Ref(collection='swmm:nodes',key='O'))))
        rule=model.controls[('RULE','R')]
        model.controls.update(('RULE','R'),conditions=(replace(rule.conditions[0],left=c.NamedOperand(reference=symbol('VARIABLE','ValueLong'))),))
        self.assertIn('control.symbol_shadow',{d.code for d in model.validate().errors})

    def test_unsupported_program_is_not_partly_claimed(self):
        source = PROGRAM+'UNKNOWN extension\n'
        model = controlled(source)
        self.assertEqual(len(model.controls),0)
        self.assertEqual(model.to_document(),model.document)
        with self.assertRaises(ValidationError):
            model.nodes.rename("J","Changed")

    def test_statement_deletion_preserves_comments_and_empty_program(self):
        model = controlled('RULE R ; keep\nIF NODE J DEPTH > 0\nTHEN CONDUIT P STATUS = OPEN\n')
        model.controls.remove(("RULE","R"))
        output=model.to_document()
        self.assertFalse(output.records("CONTROLS"))
        self.assertIn('; keep',output.text)

    def test_profile_alias_and_native_outlet_missing_setting_are_explicit(self):
        model=controlled('RULE R\nIF LINK P MAXDEPTH > 0\nTHEN CONDUIT P STATUS = OPEN\n')
        self.assertIn('control.native_fulldepth',{d.code for d in model.validate(for_run=True).errors})
        self.assertTrue(model.validate(for_run=True,normalize=True).is_valid)
        model=controlled('RULE R\nIF OUTLET P SETTING > 0\nTHEN OUTLET P SETTING = .5\n',model=regulator_model('FUNCTIONAL/HEAD'))
        self.assertIn('control.native_outlet_setting',{d.code for d in model.validate(for_run=True).errors})


if __name__ == '__main__':
    unittest.main()
