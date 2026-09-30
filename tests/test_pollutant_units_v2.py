"""Independent feature consumers and atomic concentration conversion plans."""

from dataclasses import dataclass, replace
from datetime import timedelta
import unittest

from easysewer.io.inp import InpDocument
from easysewer.io.inp.network import default_schema
from easysewer.io.json import JsonDocument
from easysewer.io.json.types import JsonField, JsonType
from easysewer.model import CollectionSpec, Model, Ref
from easysewer.model.fields import number, reference, validate_fields
from easysewer.model.inflows import ConcentrationInflow, MassInflow
from easysewer.model.pollutant_units import (
    PollutantUnitTransform, PollutantUnitConversion, PollutantResourceConversion,
)
from easysewer.model.resources import InlineTimeSeries, SeriesPoint
from easysewer.schema import DecodedFeature, FeatureDescriptor, RegistryError, SwmmProfile
from easysewer.schema.structured import EncodedRow, FeatureData, RecordEntry, SourceBinding
from easysewer.scenario import ChangePollutantUnits, ScenarioPatch, RenameRecord, SetFields, FieldChange
from easysewer.validation import ValidationError
from test_quality_v2 import quality_model, ref


@dataclass(frozen=True, kw_only=True)
class QualitySensor:
    id: str
    pollutant: Ref = reference('swmm:pollutants')
    threshold: float = number('concentration')
    series: Ref | None = reference('swmm:timeseries', None)
    backup: Ref | None = reference('swmm:timeseries', None)


def sensor_conversion(row, context):
    if row.pollutant.canonical != context.target.canonical:
        return PollutantUnitConversion(value=row)
    resources = []
    for name in ('series', 'backup'):
        target = getattr(row, name)
        if target is not None:
            series = context.record(target)
            resources.append(PollutantResourceConversion(target=target, path=(name,), value=replace(series,
                points=tuple(replace(p, value=p.value*context.factor) for p in series.points))))
    return PollutantUnitConversion(value=replace(row, threshold=row.threshold*context.factor), resources=tuple(resources))


class SensorCodec:
    collections = (CollectionSpec(key='test:quality_sensors', record_type=QualitySensor,
        key_of=lambda row: row.id, identity_field='id', validate=validate_fields),)
    pollutant_unit_transforms = (PollutantUnitTransform(value_type=QualitySensor, convert=sensor_conversion),)

    def decode(self, document, profile):
        records, bindings = [], []
        for line in document.records('QUALITY_SENSORS'):
            id, pollutant, threshold, series, backup = line.values
            row = QualitySensor(id=id, pollutant=ref('pollutants', pollutant), threshold=float(threshold),
                series=None if series == '*' else ref('timeseries', series), backup=None if backup == '*' else ref('timeseries', backup))
            records.append(RecordEntry(collection='test:quality_sensors', value=row))
            bindings.append(SourceBinding(line=line.number, key=(id,)))
        return DecodedFeature(value=FeatureData(records=tuple(records), bindings=tuple(bindings)),
            claimed_lines=frozenset(b.line for b in bindings))

    def encode(self, store, profile):
        for key, row in store.collection('test:quality_sensors').items():
            yield EncodedRow(key=(row.id,), section='QUALITY_SENSORS', values=(row.id, row.pollutant.key,
                str(row.threshold), row.series.key if row.series else '*', row.backup.key if row.backup else '*'),
                owners=(Ref(collection='test:quality_sensors', key=key),))

    def validate(self, store, profile):
        return ()


def sensor_schema(codec=None):
    schema = default_schema()
    schema.register(FeatureDescriptor(key='test:quality_sensors', sections={'QUALITY_SENSORS'}), codec or SensorCodec())
    scalar = ('object', 'core:ref')
    schema.register_json(JsonType(key='test:quality_sensor', value_type=QualitySensor, fields=(
        JsonField(name='id', attribute='id', shape=('string',)),
        JsonField(name='pollutant', attribute='pollutant', shape=scalar),
        JsonField(name='threshold', attribute='threshold', shape=('number',)),
        *(JsonField(name=name, attribute=name, shape=('union', scalar, ('null',))) for name in ('series', 'backup')))))
    return schema


def sensor_model(codec=None):
    schema = sensor_schema(codec)
    model = Model.from_document(quality_model().to_document(), schema=schema, strict=True)
    model.collection('test:quality_sensors').add(QualitySensor(id='Sensor', pollutant=ref('pollutants', 'Q0'), threshold=2))
    return model, schema


def add_shared_series(model):
    series = ref('timeseries', 'Concentration')
    model.timeseries.add(InlineTimeSeries(id=series.key, points=(SeriesPoint(time=timedelta(), value=3),)))
    model.inflows.add(ConcentrationInflow(node=ref('nodes', 'J'), constituent=ref('pollutants', 'Q0'), series=series, baseline=4))
    return series


class PollutantUnitTests(unittest.TestCase):
    def test_extension_converts_through_snapshot_json_and_scenario(self):
        model, schema = sensor_model()
        before = model.to_json_document().to_bytes()
        patch = ScenarioPatch(operations=(ChangePollutantUnits(target=ref('pollutants', 'q0'), units='UG/L'),),
            required_capabilities=('scenario:pollutant_units',))
        patch = ScenarioPatch.from_json_document(patch.to_json_document(schema=schema), schema=schema)
        result = patch.apply(model)
        self.assertEqual(before, model.to_json_document().to_bytes())
        self.assertEqual(result.model.collection('test:quality_sensors')['Sensor'].threshold, 2000)
        self.assertTrue(any(c.target.collection == 'test:quality_sensors' for c in result.changes))
        for rebuilt in (Model.from_document(result.model.to_document(), schema=schema, strict=True),
                Model.from_json_document(result.model.to_json_document(), schema=schema, strict=True), result.model.copy()):
            rebuilt.convert_pollutant_units('Q0', 'MG/L')
            self.assertEqual(rebuilt.collection('test:quality_sensors')['Sensor'].threshold, 2)

    def test_shared_resource_agreement_across_features_and_paths_converts_once(self):
        model, _ = sensor_model()
        series = add_shared_series(model)
        model.collection('test:quality_sensors').update('Sensor', series=series, backup=series)
        model.inflows.add(ConcentrationInflow(node=ref('nodes', 'O'), constituent=ref('pollutants', 'Q0'), series=series))
        model.convert_pollutant_units('Q0', 'UG/L')
        self.assertEqual(model.timeseries[series.key].points[0].value, 3000)
        self.assertEqual(model.collection('test:quality_sensors')['Sensor'].threshold, 2000)
        model.convert_pollutant_units('Q0', 'MG/L')
        self.assertEqual(model.timeseries[series.key].points[0].value, 3)

    def test_uncovered_other_pollutant_and_mass_consumers_block_resource_rewrite(self):
        for kind in ('other', 'mass', 'path'):
            with self.subTest(kind=kind):
                codec = SensorCodec()
                if kind == 'path':
                    def one_path(row, context):
                        result = sensor_conversion(row, context)
                        return replace(result, resources=result.resources[:1])
                    codec.pollutant_unit_transforms = (PollutantUnitTransform(value_type=QualitySensor, convert=one_path),)
                model, _ = sensor_model(codec)
                series = add_shared_series(model)
                model.collection('test:quality_sensors').update('Sensor', series=series, backup=series)
                if kind == 'other':
                    model.collection('test:quality_sensors').update('Sensor', pollutant=ref('pollutants', 'Q1'))
                elif kind == 'mass':
                    model.inflows.add(MassInflow(node=ref('nodes', 'O'), constituent=ref('pollutants', 'Q0'), series=series))
                before = model.to_json_document().to_bytes()
                with self.assertRaisesRegex(ValidationError, 'without an agreeing|conflicting_dimensions'):
                    model.convert_pollutant_units('Q0', 'UG/L')
                self.assertEqual(before, model.to_json_document().to_bytes())

    def test_conflicting_resource_proposals_rollback_every_record(self):
        def incompatible(row, context):
            plan = sensor_conversion(row, context)
            if plan.resources:
                resource = plan.resources[0]
                return replace(plan, resources=(replace(resource,
                    value=replace(resource.value, points=(SeriesPoint(time=timedelta(), value=99),))),))
            return plan
        codec = SensorCodec()
        codec.pollutant_unit_transforms = (PollutantUnitTransform(value_type=QualitySensor, convert=incompatible),)
        model, _ = sensor_model(codec)
        model.collection('test:quality_sensors').update('Sensor', series=add_shared_series(model))
        before = model.to_json_document().to_bytes()
        with self.assertRaisesRegex(ValidationError, 'disagree'):
            model.convert_pollutant_units('Q0', 'UG/L')
        self.assertEqual(before, model.to_json_document().to_bytes())

    def test_unknown_consumer_exact_subclass_and_nested_variants_rejected(self):
        codec = SensorCodec(); codec.pollutant_unit_transforms = ()
        model, _ = sensor_model(codec)
        before = model.to_json_document().to_bytes()
        with self.assertRaisesRegex(ValidationError, 'explicit pollutant-unit'):
            model.convert_pollutant_units('Q0', 'UG/L')
        self.assertEqual(before, model.to_json_document().to_bytes())
        @dataclass(frozen=True, kw_only=True)
        class FutureSensor(QualitySensor):
            pass
        model, _ = sensor_model()
        rows = model.collection('test:quality_sensors')
        rows.replace('Sensor', FutureSensor(id='Sensor', pollutant=ref('pollutants', 'Q0'), threshold=7))
        with self.assertRaisesRegex(ValidationError, 'explicit pollutant-unit'):
            model.convert_pollutant_units('Q0', 'UG/L')
        self.assertEqual(model.pollutants['Q0'].units, 'MG/L')
        from easysewer.model import quality as q
        @dataclass(frozen=True, kw_only=True)
        class FutureBuildup(q.BuildupFunction):
            pass
        @dataclass(frozen=True, kw_only=True)
        class FutureWashoff(q.WashoffFunction):
            pass
        for namespace, value in (('buildup', FutureBuildup()), ('washoff', FutureWashoff())):
            model = quality_model()
            model.collection('swmm:'+namespace).update(('Land','Q0'), function=value)
            with self.assertRaisesRegex(ValidationError, 'extension .* formula'):
                model.convert_pollutant_units('Q0', 'UG/L')
            self.assertEqual(model.pollutants['Q0'].units, 'MG/L')

    def test_registration_validation_conflict_snapshot_and_profile_filter(self):
        schema = sensor_schema()
        count = len(schema.bindings)
        codec = SensorCodec()
        with self.assertRaisesRegex(RegistryError, 'Conflicting pollutant-unit'):
            schema.register(FeatureDescriptor(key='test:duplicate', sections={'DUPLICATE'}), codec)
        self.assertEqual(len(schema.bindings), count)
        from easysewer.schema.profiles import EPA_SWMM_5_2_4
        self.assertEqual(schema.snapshot().pollutant_unit_transforms(EPA_SWMM_5_2_4), schema.pollutant_unit_transforms(EPA_SWMM_5_2_4))
        self.assertEqual(schema.pollutant_unit_transforms(SwmmProfile(key='test:other', engine_version='future', sections=frozenset())), ())
        for bad in ([], (object(),)):
            codec.pollutant_unit_transforms = bad
            with self.assertRaises(TypeError):
                sensor_schema(codec)
        rule = PollutantUnitTransform(value_type=QualitySensor, convert=sensor_conversion)
        codec.pollutant_unit_transforms = (rule, rule)
        with self.assertRaises(RegistryError):
            sensor_schema(codec)
        for kwargs in ({'value_type':1, 'convert':sensor_conversion}, {'value_type':QualitySensor, 'convert':None}):
            with self.assertRaises(TypeError):
                PollutantUnitTransform(**kwargs)

    def test_invalid_plans_identity_reference_paths_and_nonfinite_rollback(self):
        def bad_plan(kind):
            def convert(row, context):
                if kind == 'return': return row
                if kind == 'type': return PollutantUnitConversion(value=2)
                if kind == 'id': return PollutantUnitConversion(value=replace(row, id='Renamed'))
                if kind == 'reference': return PollutantUnitConversion(value=replace(row, pollutant=ref('pollutants', 'Q1')))
                if kind == 'nan': return PollutantUnitConversion(value=replace(row, threshold=float('nan')))
                if kind == 'bool': return PollutantUnitConversion(value=replace(row, threshold=True))
                plan = sensor_conversion(row, context)
                p = plan.resources[0]
                if kind == 'duplicate': return replace(plan, resources=(p, p))
                if kind == 'path': return replace(plan, resources=(replace(p, path=('threshold',)),))
                return replace(plan, resources=(replace(p, value=replace(p.value, id='Renamed')),))
            return convert
        for kind in ('return', 'type', 'id', 'reference', 'nan', 'bool', 'duplicate', 'path', 'resource_id'):
            codec = SensorCodec()
            codec.pollutant_unit_transforms = (PollutantUnitTransform(value_type=QualitySensor, convert=bad_plan(kind)),)
            model, _ = sensor_model(codec)
            model.collection('test:quality_sensors').update('Sensor', series=add_shared_series(model))
            if kind == 'bool':
                model.collection('test:quality_sensors').update('Sensor', threshold=1.)
            before = model.to_json_document().to_bytes()
            with self.subTest(kind=kind), self.assertRaises(ValidationError):
                model.convert_pollutant_units('Q0', 'UG/L')
            self.assertEqual(before, model.to_json_document().to_bytes())

    def test_resource_own_transform_must_agree_and_file_guards_still_apply(self):
        codec = SensorCodec()
        codec.pollutant_unit_transforms += (PollutantUnitTransform(value_type=InlineTimeSeries,
            convert=lambda row, context: PollutantUnitConversion(value=replace(row,
                points=tuple(replace(p, value=p.value*2) for p in row.points)))),)
        model, _ = sensor_model(codec)
        model.collection('test:quality_sensors').update('Sensor', series=add_shared_series(model))
        before = model.to_json_document().to_bytes()
        with self.assertRaisesRegex(ValidationError, 'Record transform and consumer'):
            model.convert_pollutant_units('Q0','UG/L')
        self.assertEqual(before, model.to_json_document().to_bytes())
        from test_files_v2 import bind
        for kind in ('HOTSTART', 'RUNOFF', 'INFLOWS'):
            model = quality_model()
            bind(model, kind, 'USE', 'unopened.bin')
            before = model.to_json_document().to_bytes()
            with self.assertRaisesRegex(ValidationError, 'external_identity_dependency'):
                model.convert_pollutant_units('Q0','UG/L')
            self.assertEqual(before, model.to_json_document().to_bytes())

    def test_self_co_pollutant_and_already_selected_units(self):
        model = quality_model()
        model.pollutants.update('Q0', co_pollutant=ref('pollutants','Q0'), co_fraction=.2)
        model.convert_pollutant_units('Q0','UG/L')
        self.assertEqual(model.pollutants['Q0'].co_fraction, .2)
        before = model.to_json_document().to_bytes()
        result = ScenarioPatch(operations=(ChangePollutantUnits(target=ref('pollutants','Q0'), units='UG/L'),)).apply(model)
        self.assertEqual(before, result.model.to_json_document().to_bytes())
        self.assertEqual(result.changes, ())

    def test_formula_unit_substitution_can_move_reference_paths_in_ast(self):
        from easysewer.model.expressions import ExpressionNode, ExpressionNumber, BinaryExpression
        @dataclass(frozen=True, kw_only=True)
        class Concentration(ExpressionNode):
            pollutant: Ref = reference('swmm:pollutants')
        @dataclass(frozen=True, kw_only=True)
        class Formula:
            id: str
            expression: ExpressionNode
        def convert(row, context):
            # A new variable must be divided by the concentration conversion
            # factor before entering an empirical formula with old-unit constants.
            return PollutantUnitConversion(value=replace(row, expression=BinaryExpression(operator='/',
                left=row.expression, right=ExpressionNumber(value=context.factor))))
        class FormulaCodec:
            collections = (CollectionSpec(key='test:formulas', record_type=Formula, key_of=lambda row:row.id,
                identity_field='id', validate=validate_fields),)
            pollutant_unit_transforms = (PollutantUnitTransform(value_type=Formula, convert=convert),)
            def decode(self, document, profile): return DecodedFeature(value=FeatureData())
            def encode(self, store, profile): return ()
            def validate(self, store, profile): return ()
        schema = default_schema()
        schema.register(FeatureDescriptor(key='test:formulas', sections={'FORMULAS'}), FormulaCodec())
        model = Model.from_document(quality_model().to_document(), schema=schema, strict=True)
        model.collection('test:formulas').add(Formula(id='F', expression=Concentration(pollutant=ref('pollutants','Q0'))))
        model.convert_pollutant_units('Q0','UG/L')
        expression = model.collection('test:formulas')['F'].expression
        self.assertEqual(expression.left.pollutant, ref('pollutants','Q0'))
        self.assertEqual(expression.right.value, 1000)
        self.assertTrue(any(use.path == ('expression','left','pollutant') for use in model._store.referenced_by(ref('pollutants','Q0'))))

    def test_callback_reads_original_snapshot_independent_of_collection_order(self):
        observed = []
        def observe(row, context):
            observed.append((context.record(context.target).units, context.record(row.series).points[0].value,
                tuple(use.path for use in context.referenced_by(row.series))))
            return sensor_conversion(row, context)
        codec = SensorCodec()
        codec.pollutant_unit_transforms = (PollutantUnitTransform(value_type=QualitySensor, convert=observe),)
        model, _ = sensor_model(codec)
        model.collection('test:quality_sensors').update('Sensor', series=add_shared_series(model))
        model.convert_pollutant_units('Q0', 'UG/L')
        self.assertEqual(observed, [('MG/L', 3, (('series',), ('series',)))])

    def test_scenario_conversion_reinterpret_rename_failure_and_unknown_fields(self):
        model = quality_model()
        before = model.to_json_document().to_bytes()
        operation = ChangePollutantUnits(target=ref('pollutants','Q0'), units='UG/L')
        data = ScenarioPatch(operations=(operation,)).to_json_document().data
        del data['operations'][0]['mode']
        document = JsonDocument.from_data(data)
        patch = ScenarioPatch.from_json_document(document)
        self.assertEqual(patch.to_json_document().to_bytes(), document.to_bytes())
        self.assertEqual(patch.apply(model).model.pollutants['Q0'].rainfall_concentration, 2000)
        for units in ('UG/L', '#/L'):
            result = ScenarioPatch(operations=(replace(operation, mode='reinterpret', units=units),)).apply(model)
            self.assertEqual(result.model.pollutants['Q0'].units, units)
            self.assertEqual(result.model.pollutants['Q0'].rainfall_concentration, 2)
        renamed = ScenarioPatch(operations=(RenameRecord(target=operation.target, new_id='Moved'),
            replace(operation, target=ref('pollutants', 'Moved')))).apply(model)
        self.assertEqual(renamed.model.pollutants['Moved'].units, 'UG/L')
        failed = ScenarioPatch(operations=(operation, SetFields(target=ref('pollutants','Q1'),
            changes=(FieldChange(name='rainfall_concentration', value=-1),))))
        with self.assertRaises(ValidationError): failed.apply(model)
        with self.assertRaises(ValidationError):
            ScenarioPatch(operations=(replace(operation, units='#/L'),)).apply(model)
        self.assertEqual(before, model.to_json_document().to_bytes())
        data['operations'][0]['future'] = True
        unknown = ScenarioPatch.from_json_document(JsonDocument.from_data(data))
        self.assertEqual(unknown.to_json_document().data, data)
        with self.assertRaises(ValidationError): unknown.apply(model)
        for changes in ({'target':ref('nodes','J')}, {'target':ref('pollutants',('Q0','Q1'))},
                {'mode':'automatic'}, {'units':'mg/l'}):
            with self.assertRaises(ValueError): replace(operation, **changes)


if __name__ == '__main__':
    unittest.main()
