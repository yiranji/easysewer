"""Reviewed TAGS variant gates against literal GUI records and public model edits."""
from dataclasses import replace
from datetime import time, timedelta
import unittest

from easysewer.io.inp import InpDocument
from easysewer.model import Model, Ref
from easysewer.model.project import ObjectTag
from easysewer.validation import ValidationError
from test_hydrology_v2 import hydrology_model
from test_scenario_v2 import portable

TARGETS = (('Gage','swmm:raingages','R'),('Subcatch','swmm:subcatchments','S'),
           ('Node','swmm:nodes','J'),('Link','swmm:links','P'))
UNITS = ('CFS','GPM','MGD','CMS','LPS','MLD')
CASES = {word: (f'{word} {key} "雨水 zone"\n', ((namespace,key,'雨水 zone'),))
         for word,namespace,key in TARGETS}
CASES['empty text'] = (''.join(f'{word} {key} ""\n' for word,_,key in TARGETS),
                       tuple((namespace,key,'') for _,namespace,key in TARGETS))
CASES['repeated last assignment'] = (
    ''.join(f'{word} {key} first ; earlier\n' for word,_,key in TARGETS) + '[tAgS]\n' +
    ''.join(f'{word.lower()} {key.lower()} "final zone" ignored\n' for word,_,key in reversed(TARGETS)),
    tuple((namespace,key.lower(),'final zone') for _,namespace,key in TARGETS))
CASES['keyword prefixes'] = ('gAgEs R "rain zone"\nSUBCanything S "catch zone"\n'
                             'nOdEs J "node zone"\nLINKextra P "link zone"\n',
    tuple((namespace,key,text) for (_,namespace,key),text in zip(TARGETS,
         ('rain zone','catch zone','node zone','link zone'))))


def base_model(units='CFS'):
    model=hydrology_model()
    model.update_options(end_date=model.options.start_date,end_time=time(6),
                         report_step=timedelta(minutes=5),allow_ponding=True)
    if units!='CFS':model.convert_units(units)
    return model


def fixture(variant, units='CFS'):
    model=base_model(units);body,expected=CASES[variant]
    source=model.to_document().text+'\n; TAGS fixture\n[TAGS]\n'+body
    return source,expected


def parse(source):
    document=InpDocument.from_bytes(source if isinstance(source,bytes) else source.encode('utf-8'),source='tags-gates.inp')
    return Model.from_document(document,strict=True)


def values(model):
    return tuple((row.target.collection,row.target.key.casefold(),row.text) for row in model.tags.values())


class TagGateTests(unittest.TestCase):
    def test_long_gui_text_is_preserved_without_silently_shortening_it(self):
        for count in (1014,1015,2000):
            for text in ('x'*count,'雨'*count):
                model=base_model();model.tags.add(ObjectTag(target=Ref(collection='swmm:nodes',key='J'),text=text))
                source=model.to_document().text
                self.assertEqual(parse(source).tags[('swmm:nodes','J')].text,text)
                self.assertEqual(portable(model).tags[('swmm:nodes','J')].text,text)
                # GUI preservation does not imply that a solver with a bounded
                # physical input record can execute it. Native gates check that.
                self.assertIn(text,source)

    def test_each_variant_source_creation_edit_clear_and_roundtrip(self):
        for variant in CASES:
            for units in UNITS:
                with self.subTest(variant=variant,units=units):
                    source,expected=fixture(variant,units)
                    raw=b'\xef\xbb\xbf'+source.replace('\n','\r\n').encode('utf-8')
                    model=parse(raw);self.assertEqual(model.to_document().to_bytes(),raw)
                    wanted=tuple((ns,key.casefold(),text) for ns,key,text in expected)
                    self.assertEqual(values(model),wanted)
                    created=base_model(units)
                    for ns,key,text in expected:created.tags.add(ObjectTag(target=Ref(collection=ns,key=key),text=text))
                    self.assertEqual(values(created),wanted)
                    for current in (model,created,portable(model),parse(model.to_document(normalize=True).text)):
                        self.assertEqual(values(current),wanted)
                        self.assertEqual(current.units.flow_units,units)
                    first=next(iter(model.tags));original=model.to_document().to_bytes()
                    with self.assertRaisesRegex(RuntimeError,'rollback'):
                        with model.transaction():
                            model.tags.update(first,text='rollback draft');raise RuntimeError('rollback')
                    self.assertEqual(model.to_document().to_bytes(),original)
                    model.tags.update(first,text='修改后的 tag')
                    self.assertEqual(portable(model).tags[first].text,'修改后的 tag')
                    model.tags.update(first,text='')
                    self.assertEqual(parse(model.to_document().text).tags[first].text,'')
                    for key in tuple(model.tags):model.tags.remove(key)
                    self.assertFalse(model.tags);self.assertFalse(model.to_document().records('TAGS'))
                    self.assertFalse(portable(model).tags)

    def test_each_target_rename_collision_delete_and_identity_rollback(self):
        for _,ns,key in TARGETS:
            with self.subTest(target=ns):
                model=base_model();collection=model.collection(ns)
                model.tags.add(ObjectTag(target=Ref(collection=ns,key=key),text='bound'))
                collection.add(replace(collection[key],id='Conflict'))
                old=model.to_document().to_bytes()
                with self.assertRaises(ValueError):collection.rename(key,'Conflict')
                self.assertEqual(model.to_document().to_bytes(),old)
                with self.assertRaises((ValueError,ValidationError)):
                    with model.transaction():
                        model.tags.update((ns,key),target=Ref(collection=ns,key='Conflict'))
                self.assertEqual(model.to_document().to_bytes(),old)
                collection.rename(key,'NewID')
                self.assertEqual(model.tags[(ns,'newid')].target.key,'NewID')
                self.assertEqual(portable(model).tags[(ns,'NewID')].text,'bound')
                with self.assertRaises(ValidationError):collection.remove('NewID')
                self.assertEqual(model.tags[(ns,'NewID')].text,'bound')
                collection.remove('NewID',cascade=True)
                self.assertNotIn((ns,'NewID'),model.tags)
                self.assertFalse(parse(model.to_document().text).tags)

    def test_source_order_provenance_empty_and_unit_independence(self):
        for variant in CASES:
            with self.subTest(variant=variant):
                source,expected=fixture(variant);model=parse(source)
                for ns,key,text in expected:
                    owner=Ref(collection='swmm:tags',key=(ns,key))
                    provenance=model.field_provenance(owner,'text')
                    self.assertEqual(provenance.status,'explicit')
                    self.assertEqual(provenance.declarations[-1].tokens[0].value,text)
                    self.assertTrue(provenance.declarations[-1].contributes)
                    self.assertEqual(model.inspect_field(owner,'text').value,text)
                    self.assertEqual(model.inspect_field(owner,'text').semantics.unit.status,'not_applicable')
                before=values(model)
                for units in UNITS:
                    changed=model.copy();changed.convert_units(units)
                    self.assertEqual(values(changed),before)
                key=next(iter(model.tags));model.tags.move(key)
                self.assertEqual(values(parse(model.to_document().text)),values(model))
                self.assertEqual(values(portable(model)),values(model))

    def test_each_target_invalid_rows_unknown_prefixes_and_location(self):
        base=base_model().to_document().text
        for word,ns,key in TARGETS:
            for row in (f'{word} {key}',f'{word} {key} "unterminated',f'{word} Missing text'):
                with self.subTest(row=row):
                    source=base+'[TAGS]\n'+row+'\n'
                    model=Model.from_document(InpDocument.from_text(source,source='bad-tags.inp'))
                    self.assertEqual(model.document.text,source)
                    report=model.validate();self.assertFalse(report.is_valid)
                    self.assertTrue(any(d.span and d.span.source=='bad-tags.inp' or d.locations for d in report.errors))
                    with self.assertRaises(ValidationError):model.to_document()
            for short in (word[:3], 'Future'):
                source=base+'[TAGS]\n'+f'{short} {key} text\n'
                model=parse(source);self.assertFalse(model.tags)
                self.assertTrue(model.support.opaque_records)
                self.assertEqual(model.to_document().text,source)
                with self.assertRaises(ValidationError):model.collection(ns).rename(key,'Changed')
            for text in ('bad;tag','bad"tag','bad\ntag','bad\rtag','bad\0tag'):
                model=base_model();before=model.to_document().to_bytes()
                with self.assertRaises((ValueError,ValidationError)):
                    with model.transaction():model.tags.add(ObjectTag(target=Ref(collection=ns,key=key),text=text))
                self.assertEqual(model.to_document().to_bytes(),before)


if __name__=='__main__':unittest.main()
