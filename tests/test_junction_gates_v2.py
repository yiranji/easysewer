"""Literal JUNCTIONS rows, public mutations and explicit per-variant gates."""
from dataclasses import replace
import unittest
from easysewer.io.inp import InpDocument
from easysewer.model import Model,Ref
from easysewer.model.network import Junction
from easysewer.validation import ValidationError

NODE=Ref(collection='swmm:nodes',key='J')
FIELDS=('max_depth','initial_depth','surcharge_depth','ponded_area')
UNITS=('CFS','GPM','MGD','CMS','LPS','MLD')
FLOW=dict(zip(UNITS,(1.,448.831,.64632,.02832,28.317,2.4466)))
CASES={
    'minimum row':(('',(None,None,None,None)),),
    'optional maximum/initial/surcharge depths':(
        (' 0',(0.,None,None,None)),(' 1',(1.,None,None,None)),
        (' 1 .2',(1.,.2,None,None)),(' 1 .2 .3',(1.,.2,.3,None)),(' 0 0 0',(0.,0.,0.,None))),
    'ponded area':((' 1 .2 .3 20',(1.,.2,.3,20.)),(' 1 0 0 0',(1.,0.,0.,0.)),(' 0 0 0 20',(0.,0.,0.,20.))),
}


def source(tail='',units='CFS',ponding=False):
    return (f'[OPTIONS]\nFLOW_UNITS {units}\nFLOW_ROUTING DYNWAVE\nALLOW_PONDING {"YES" if ponding else "NO"}\n'
        'START_DATE 01/01/2020\nEND_DATE 01/01/2020\nEND_TIME 00:02:00\nREPORT_STEP 00:00:05\n'
        'ROUTING_STEP 1\nVARIABLE_STEP 0\n[OUTFALLS]\nO -1 FREE\n'
        f'[JUNCTIONS]\nJ 0{tail} ; junction\n[PUMPS]\nClosed J O * OFF 0 0\n'
        f'[INFLOWS]\nJ FLOW "" FLOW 1 1 {FLOW[units]}\n[REPORT]\nNODES ALL\nLINKS ALL\n')


def load(text,strict=True):
    document=InpDocument.from_bytes(text if isinstance(text,bytes) else text.encode(),source='junction-gates.inp')
    return Model.from_document(document,strict=strict)


def portable(model):
    return Model.from_json_document(model.to_json_document(),strict=True)


def declared(model):
    return tuple(getattr(model.nodes['J'],field) for field in FIELDS)


def created(tail,values,units,ponding):
    text=source(tail,units,ponding).replace(f'J 0{tail} ; junction\n','')
    model=load(text,strict=False)
    model.nodes.add(Junction(id='J',elevation=0,**dict(zip(FIELDS,values))))
    model.validate().raise_for_errors()
    return model


class JunctionGateTests(unittest.TestCase):
    def test_each_variant_source_create_edit_clear_and_roundtrip(self):
        for variant,cases in CASES.items():
            for tail,values in cases:
                for units in UNITS:
                    for ponding in (False,True):
                        with self.subTest(variant=variant,tail=tail,units=units,ponding=ponding):
                            text=source(tail,units,ponding)
                            raw=b'\xef\xbb\xbf'+text.replace('\n','\r\n').encode()
                            model=load(raw);self.assertEqual(model.to_document().to_bytes(),raw)
                            self.assertEqual(declared(model),values)
                            fresh=created(tail,values,units,ponding)
                            for current in (fresh,portable(model),load(model.to_document(normalize=True).text)):
                                self.assertEqual(declared(current),values)
                                self.assertTrue(current.validate(for_run=True).is_valid)
                            model.nodes.update('J',max_depth=2,initial_depth=.25,surcharge_depth=.5,ponded_area=30)
                            self.assertEqual(declared(portable(model)),(2,.25,.5,30))
                            self.assertEqual(declared(load(model.to_document().text)),(2,.25,.5,30))
                            for field in FIELDS:
                                model.nodes.update('J',**{field:None})
                                self.assertIsNone(getattr(portable(model).nodes['J'],field))
                                reread=load(model.to_document(normalize=True).text)
                                for name in FIELDS:
                                    a=model.inspect_field(NODE,name).semantics.effective
                                    b=reread.inspect_field(NODE,name).semantics.effective
                                    self.assertEqual((a.status,a.value),(b.status,b.value))
                            self.assertEqual(declared(load(model.to_document().text)),(None,)*4)

    def test_each_variant_identity_order_references_delete_and_rollback(self):
        for variant,cases in CASES.items():
            for tail,_ in cases:
                with self.subTest(variant=variant,tail=tail):
                    model=load(source(tail));model.nodes.add(Junction(id='K',elevation=0))
                    model.nodes.move('K',before='J')
                    self.assertEqual(tuple(portable(model).nodes),tuple(model.nodes))
                    self.assertEqual(tuple(load(model.to_document().text).nodes),tuple(model.nodes))
                    before=model.to_json_document()
                    with self.assertRaises((ValueError,ValidationError)):model.nodes.rename('J','K')
                    self.assertEqual(model.to_json_document(),before)
                    with self.assertRaisesRegex(RuntimeError,'rollback'):
                        with model.transaction():
                            model.nodes.rename('J','Temporary');model.nodes.update('Temporary',ponded_area=77)
                            raise RuntimeError('rollback')
                    self.assertEqual(model.to_json_document(),before)
                    model.nodes.rename('J','Changed')
                    self.assertEqual(model.links['Closed'].inlet.key,'Changed')
                    self.assertTrue(model.referenced_by(Ref(collection='swmm:nodes',key='Changed')))
                    self.assertEqual(portable(model).links['Closed'].inlet.key,'Changed')
                    with self.assertRaises(ValidationError):model.nodes.remove('Changed')
                    model.nodes.remove('Changed',cascade=True)
                    self.assertNotIn('Closed',model.links)
                    self.assertNotIn('Changed',load(model.to_document().text).nodes)

    def test_each_variant_defaults_units_context_and_sources(self):
        for variant,cases in CASES.items():
            for tail,values in cases:
                for units in UNITS:
                    for ponding in (False,True):
                        model=load(source(tail,units,ponding))
                        for index,name in enumerate(FIELDS):
                            info=model.inspect_field(NODE,name)
                            self.assertEqual(info.provenance.status,'omitted' if values[index] is None else 'explicit')
                            self.assertEqual(info.semantics.default.value,0.)
                            self.assertEqual(info.semantics.unit.value,('ft2' if units in UNITS[:3] else 'm2') if name=='ponded_area' else ('ft' if units in UNITS[:3] else 'm'))
                            if name=='ponded_area' and not ponding:self.assertEqual(info.semantics.effective.status,'not_applicable')
                            else:self.assertEqual(info.semantics.effective.value,values[index] or 0.)
                        changed=model.copy();changed.convert_units('CMS' if units in UNITS[:3] else 'CFS')
                        factor=.3048 if units in UNITS[:3] else 1/.3048
                        for name,value in zip(FIELDS,values):
                            if value is None:self.assertIsNone(getattr(changed.nodes['J'],name))
                            else:self.assertAlmostEqual(getattr(changed.nodes['J'],name),value*factor**(2 if name=='ponded_area' else 1),places=10)

    def test_each_variant_invalid_unknown_and_diagnostic_sources(self):
        for variant,cases in CASES.items():
            for tail,_ in cases:
                base=source(tail)
                for bad in ('J','J bad','J 0 -1','J 0 1 -.1','J 0 1 0 -.1','J 0 1 0 0 -.1','J 0 1 0 0 20 extra'):
                    text=base.replace(f'J 0{tail} ; junction',bad)
                    model=load(text,strict=False)
                    self.assertEqual(model.document.text,text)
                    report=model.validate();self.assertFalse(report.is_valid)
                    self.assertTrue(any(d.span or d.locations for d in report.errors))
                    with self.assertRaises(ValidationError):model.to_document()
                text=base+'[JUNCTIONS]\nj 0 1\n';model=load(text,strict=False)
                self.assertEqual(model.document.text,text);self.assertFalse(model.validate().is_valid)
                opaque=load(base+'[FUTURE]\nJ hidden-reference\n')
                self.assertIn('hidden-reference',opaque.to_document().text)
                with self.assertRaises(ValidationError):opaque.nodes.rename('J','Changed')
                model=load(base);before=model.to_json_document()
                for name in FIELDS:
                    with self.assertRaises((ValueError,ValidationError)):
                        with model.transaction():model.nodes.update('J',**{name:-1})
                    self.assertEqual(model.to_json_document(),before)
                    draft=model.copy();draft.nodes.update('J',**{name:-1})
                    self.assertEqual(getattr(draft.nodes['J'],name),-1)
                    self.assertFalse(draft.validate().is_valid)
                    with self.assertRaises(ValidationError):draft.to_document()

    def test_optional_slot_combinations_and_native_initial_bound(self):
        for units in UNITS:
            for maximum in (None,0.,1.):
                for initial in (None,0.,.2):
                    for surcharge in (None,0.,.3):
                        for area in (None,0.,20.):
                            values=(maximum,initial,surcharge,area)
                            model=created('',values,units,True)
                            self.assertEqual(declared(portable(model)),values)
                            restored=load(model.to_document(normalize=True).text)
                            for name in FIELDS:
                                a=model.inspect_field(NODE,name).semantics.effective
                                b=restored.inspect_field(NODE,name).semantics.effective
                                self.assertEqual((a.status,a.value),(b.status,b.value))
                            rejected=(initial or 0)>(maximum or 0)+(surcharge or 0)
                            self.assertEqual(any(d.code=='node.initial_depth' for d in model.validate(for_run=True).errors),rejected)
