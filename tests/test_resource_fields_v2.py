"""Shared field contracts, original grouped declarations and independent consumers."""
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
import unittest
from unittest.mock import patch

from easysewer.io.inp import InpDocument
from easysewer.io.inp.network import default_schema
from easysewer.model import Model, Ref, CollectionSpec
from easysewer.model.fields import reference, validate_fields
from easysewer.model.network import Outfall, SeriesBoundary
from easysewer.model.resources import CURVE_KINDS, CURVE_DIMENSIONS, PATTERN_LENGTHS, CurvePoint, Pattern
from easysewer.model.units import UnitContext
from easysewer.model.usage import ResourceUse
from easysewer.schema import FeatureDescriptor
from easysewer.schema.field_contracts import FieldRule
from test_scenario_v2 import portable

UNITS = ('CFS', 'GPM', 'MGD', 'CMS', 'LPS', 'MLD')
C = Ref(collection='swmm:curves', key='C')
T = Ref(collection='swmm:timeseries', key='T')
P = Ref(collection='swmm:patterns', key='P')


def load(text, **kwargs):
    return Model.from_document(InpDocument.from_text(text, source='D:/models/resources.inp'), strict=True, **kwargs)


def queries(model):
    result = []
    def walk(owner, value, path=()):
        if is_dataclass(value):
            for f in fields(value):
                p = (*path, f.name)
                result.append(model.inspect_field(owner, p))
                result.append(model.field_provenance(owner, p))
                walk(owner, getattr(value, f.name), p)
        elif isinstance(value, tuple):
            for i, v in enumerate(value):
                if path == ('factors',):
                    result.append(model.inspect_field(owner, (*path, i)))
                    result.append(model.field_provenance(owner, (*path, i)))
                walk(owner, v, (*path, i))
    for collection in ('swmm:curves', 'swmm:timeseries', 'swmm:patterns'):
        for key, value in model.collection(collection).items():
            walk(Ref(collection=collection, key=key), value)
    return tuple(result)


@dataclass(frozen=True, kw_only=True)
class Probe:
    id: str
    target: Ref
    dimensions: tuple[str, ...] = ()
    accepted: tuple[str, ...] = ()
    declared: bool = True
    fake_path: bool = False


class ProbeCodec:
    collections = (CollectionSpec(key='test:probes', record_type=Probe, key_of=lambda r: r.id,
                                  identity_field='id', validate=validate_fields),)
    def decode(self, document, profile):
        raise NotImplementedError
    def encode(self, store, profile):
        return ()
    def validate(self, store, profile):
        return ()
    def resource_uses(self, store, profile):
        for row in store.collection('test:probes').values():
            if row.declared:
                yield ResourceUse(owner=Ref(collection='test:probes', key=row.id), target=row.target,
                    path=('bad' if row.fake_path else 'target',), role='independent test consumer',
                    dimensions=row.dimensions, accepted_kinds=row.accepted)


def with_probes(text):
    # This independent codec owns programmatic records, not INP syntax.
    original = load(text)
    schema = default_schema()
    schema.register(FeatureDescriptor(key='test:probes', sections={'PROBES'}), ProbeCodec())
    model = Model(schema=schema)
    for name in ('curves', 'timeseries'):
        for record in getattr(original, name).values():
            getattr(model, name).add(record)
    return model


class ResourceFieldTests(unittest.TestCase):
    def test_all_curve_kinds_units_and_original_pairs(self):
        for kind in CURVE_KINDS:
            for units in UNITS:
                model = load(f'[OPTIONS]\nFLOW_UNITS {units}\n[CURVES]\nC {kind} 0 2 1 3\nc 2 4\n')
                for i in range(3):
                    for axis in ('x', 'y'):
                        info = model.inspect_field(C, ('points', i, axis))
                        self.assertEqual(info.semantics.effective.value, getattr(model.curves['C'].points[i], axis))
                        self.assertEqual(info.provenance.status, 'untracked_path')
                        source = model.field_provenance(C, ('points', i, axis))
                        self.assertEqual(source.status, 'explicit')
                        self.assertEqual(float(source.declarations[0].tokens[0].raw), info.value)
                        if kind == 'CONTROL':
                            self.assertEqual(info.semantics.unit.status, 'unknown')
                        else:
                            self.assertEqual(info.semantics.unit.value, UnitContext(flow_units=units).unit(CURVE_DIMENSIONS[kind][axis == 'y']))
                declarations = model.field_provenance(C, 'id').declarations
                self.assertEqual([d.contributes for d in declarations], [True, False])
                self.assertEqual(queries(Model.from_json_document(model.to_json_document(), strict=True)), queries(model))

    def test_pattern_period_padding_capacity_discarded_tokens_and_items(self):
        for kind, count in PATTERN_LENGTHS.items():
            for factors in ((), (0, -2), tuple(range(24))):
                text = f'[PATTERNS]\nP {kind} ' + ' '.join(map(str, factors)) + '\n'
                if len(factors) == 24:
                    text += 'P ignored-tail invalid-number\n'
                model = load(text)
                info = model.inspect_field(P, 'factors')
                self.assertEqual(info.semantics.default.value, (1.,) * count)
                self.assertEqual(info.semantics.effective.value, (factors + (1.,) * count)[:count])
                self.assertEqual(info.provenance.status, 'explicit' if factors else 'omitted')
                for i, factor in enumerate(factors):
                    item = model.inspect_field(P, ('factors', i))
                    self.assertEqual(item.constructor_default.status, 'not_applicable')
                    self.assertEqual(item.json_default.status, 'not_applicable')
                    self.assertEqual(item.semantics.default.value, 1)
                    self.assertEqual(item.semantics.effective.status, 'known' if i < count else 'not_applicable')
                    self.assertEqual(float(model.field_provenance(P, ('factors', i)).declarations[0].tokens[0].raw), factor)
                if len(factors) == 24:
                    self.assertTrue(all(not d.contributes and d.role == 'retained' for d in info.provenance.declarations[-2:]))
                self.assertEqual(queries(Model.from_json_document(model.to_json_document(), strict=True)), queries(model))
                self.assertEqual(model.to_document().text, text)

    def test_series_date_carry_across_groups_full_start_and_precision(self):
        model = load('[OPTIONS]\nSTART_DATE 01/01/2020\nSTART_TIME 12:00\n[TIMESERIES]\n'
                     'T 0 2 .04 3\nOther 0 1\nT 01/01/2020 12:05 4 12:06 5\n'
                     '[TIMESERIES]\nT 12:07:30.9 6\n')
        expected = [datetime(2020, 1, 1, 12), datetime(2020, 1, 1, 12, 2, 24),
                    datetime(2020, 1, 1, 12, 5), datetime(2020, 1, 1, 12, 6), datetime(2020, 1, 1, 12, 7, 30)]
        for i, stamp in enumerate(expected):
            info = model.inspect_field(T, ('points', i, 'time'))
            self.assertEqual(info.semantics.effective.value, stamp)
            source = model.field_provenance(T, ('points', i, 'time'))
            self.assertEqual(source.status, 'derived' if i >= 3 else 'explicit')
            if i >= 3:
                self.assertEqual(source.declarations[0].tokens[0].raw, '01/01/2020')
                self.assertTrue(all(d.contributes for d in source.declarations))
        self.assertEqual([p.time for p in model.inspect_field(T, 'points').semantics.effective.value], expected)
        self.assertEqual(queries(Model.from_json_document(model.to_json_document(), strict=True)), queries(model))
        model.update_options(start_time=time(13))
        self.assertEqual(model.inspect_field(T, 'points').semantics.effective.status, 'invalid')

    def test_external_file_lexical_sources_and_expected_units_without_io(self):
        model = load('[TIMESERIES]\nT FILE "missing series.dat"\n')
        model.nodes.add(Outfall(id='O', elevation=0, boundary=SeriesBoundary(series=T)))
        with patch.object(Path, 'open', side_effect=AssertionError('No I/O during inspection')):
            self.assertEqual(model.inspect_field(T, 'file').semantics.unit.value, 'ft')
            self.assertEqual(model.inspect_field(T, 'file').semantics.effective.value, model.timeseries['T'].file)
            for name in ('base_directory', 'flavor', 'direction'):
                self.assertEqual(model.field_provenance(T, ('file', name)).status, 'derived')
            self.assertEqual(model.field_provenance(T, ('file', 'path')).declarations[0].tokens[0].value, 'missing series.dat')
        self.assertEqual(model.timeseries['T'].file.base_directory, 'D:\\models')

    def test_consumer_dimensions_conflicts_missing_declarations_and_invalid_paths(self):
        model = with_probes('[TIMESERIES]\nT 0 32 1 50\n')
        probes = model.collection('test:probes')
        path = ('points', 0, 'value')
        self.assertEqual(model.inspect_field(T, path).semantics.unit.status, 'unknown')
        probes.add(Probe(id='a', target=T, dimensions=('temperature',)))
        self.assertEqual(model.inspect_field(T, path).semantics.unit.value, 'F')
        model.reinterpret_units('CMS')
        self.assertEqual(model.inspect_field(T, path).semantics.unit.value, 'C')
        probes.add(Probe(id='b', target=T, dimensions=('temperature',)))
        for changes, status in ((dict(declared=False), 'unknown'), (dict(dimensions=()), 'unknown'),
                                (dict(dimensions=('flow',)), 'ambiguous'), (dict(dimensions=('flow', 'ratio')), 'invalid'),
                                (dict(dimensions=('temperature',), fake_path=True), 'invalid')):
            probes.replace('b', Probe(id='b', target=T, **changes))
            self.assertEqual(model.inspect_field(T, path).semantics.unit.status, status)
        probes.remove('b')
        probes.update('a', dimensions=('unregistered-dimension',))
        self.assertEqual(model.inspect_field(T, path).semantics.unit.status, 'unknown')

    def test_control_curve_independent_consumers_and_intrinsic_kind_mismatch(self):
        model = with_probes('[CURVES]\nC CONTROL 0 0 1 1\n')
        probes = model.collection('test:probes')
        probes.add(Probe(id='a', target=C, dimensions=('rain_depth', 'ratio'), accepted=('CONTROL',)))
        self.assertEqual(model.inspect_field(C, 'points').semantics.unit.value, ('in', '1'))
        model.curves.update('C', kind='STORAGE')
        self.assertEqual(model.inspect_field(C, 'points').semantics.effective.status, 'invalid')
        probes.update('a', accepted=())
        self.assertEqual(model.inspect_field(C, 'points').semantics.unit.status, 'ambiguous')
        probes.update('a', declared=False)
        self.assertEqual(model.inspect_field(C, 'points').semantics.unit.value, ('ft', 'ft2'))

    def test_identity_rollback_portable_edits_and_unit_conversion(self):
        model = load('[CURVES]\nC STORAGE 0 10 2 20\n[PATTERNS]\nP DAILY 0 -2\n')
        before = queries(model)
        with self.assertRaises(RuntimeError):
            with model.transaction():
                model.curves.rename('C', 'Changed')
                raise RuntimeError
        self.assertEqual(queries(model), before)
        source = model.field_provenance(C, ('points', 1, 'y'))
        model.curves.rename('C', 'Renamed')
        owner = Ref(collection='swmm:curves', key='Renamed')
        model.curves.update('Renamed', points=(CurvePoint(x=0, y=50),))
        self.assertEqual(model.field_provenance(owner, ('points', 1, 'y')).value, source.value)
        self.assertEqual(model.inspect_field(owner, ('points', 0, 'y')).provenance.status, 'untracked_path')
        model.convert_units('CMS')
        self.assertEqual(model.inspect_field(owner, ('points', 0, 'y')).semantics.unit.value, 'm2')
        self.assertEqual(queries(Model.from_json_document(model.to_json_document(), strict=True)), queries(model))
        self.assertEqual(portable(model).field_provenance(owner, 'kind').status, 'untracked')
        model.patterns.remove('P'); model.patterns.add(Pattern(id='P', kind='DAILY'))
        self.assertEqual(model.field_provenance(P, 'kind').status, 'created')

    def test_invalid_group_is_not_partially_claimed_and_index_resolver_is_opt_in(self):
        for text in ('[CURVES]\nC STORAGE 0 1\nC bad 2\n', '[TIMESERIES]\nT 0 1\nT FILE x\n'):
            model = Model.from_document(InpDocument.from_text(text))
            self.assertEqual(len(model.curves) + len(model.timeseries), 0)
            self.assertFalse(model.validate().is_valid)
        with self.assertRaises(TypeError):
            FieldRule(value_type=Pattern, field='factors', resolve=lambda c: None, resolve_item=42)
        model = load('[CURVES]\nC STORAGE 0 1\n')
        self.assertEqual(model.inspect_field(C, ('points', 0)).semantics.effective.status, 'unknown')


if __name__ == '__main__':
    unittest.main()
