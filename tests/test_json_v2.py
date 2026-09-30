"""Model JSON contracts, source authority, forward preservation and migrations."""

import codecs
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from easysewer.io.inp import InpDocument
from easysewer.io.inp.network import default_schema
from easysewer.io.json import JsonDocument, JsonField, JsonType, MigrationOutput, MigrationRegistry
from easysewer.model import Model, Ref
from easysewer.model import climate as cl, controls as c, hydrology as h, network as n, surface as s
from easysewer.model.geometry import CrossSection, Circular
from easysewer.model.inflows import FLOW, FlowInflow, DryWeatherFlow
from easysewer.model.options import DayTime, MonthDay
from easysewer.model.resources import Curve, CurvePoint, FileTimeSeries, InlineTimeSeries, Pattern, SeriesPoint
from easysewer.model.values import FileReference, Offset, Point
from easysewer.schema import FeatureDescriptor
from easysewer.validation import ValidationError
from test_controls_v2 import controlled, PROGRAM
from test_hydrology_v2 import hydrology_model, snowpack, INFILTRATION
from test_model_extensions import Sensor, SensorCodec
from test_native_v2_network import CASES, NETWORK, RESOURCES, SETTINGS
from test_nodes_v2 import storage_model, divider_model, SHAPES, CURVE
from test_options_v2 import network
from test_regulators_v2 import regulator_model, KINDS
from test_surface_v2 import INLETS, transect


def entry(data, namespace, key):
    return next(row for block in data['collections'] if block['collection']==namespace
                for row in block['records'] if row['key']==key)


def restore(model, *, source=True):
    data=model.to_json_document().data
    if not source:
        data.pop('source',None)
    return Model.from_json_document(JsonDocument.from_data(data),strict=True)


def domain_models():
    from test_lid_v2 import lid_corpus
    yield from lid_corpus()
    from test_treatment_v2 import treatment_corpus
    yield from treatment_corpus()
    from test_groundwater_v2 import groundwater_corpus
    yield from groundwater_corpus()
    from test_quality_v2 import quality_corpus
    yield from quality_corpus()
    from test_rdii_v2 import rdii_model
    yield rdii_model()
    for shape,parameters in CASES.items():
        yield Model.from_document(InpDocument.from_text(SETTINGS+NETWORK.format(shape=shape+' '+parameters)+RESOURCES.get(shape,'')),strict=True)
    for kind in KINDS:
        yield regulator_model(kind)
    for kind,parameters in SHAPES.items():
        yield Model.from_document(InpDocument.from_text(f'[STORAGE]\nS 10 5 1 {kind} {parameters} .5 .7'+(' 0' if kind=='PARABOLOID' else ' 3 .2 .3')+'\n'+(CURVE if kind=='TABULAR' else '')),strict=True)
    for kind in ('OVERFLOW','CUTOFF','TABULAR','WEIR'):
        yield divider_model(kind)
    for method in INFILTRATION:
        model=hydrology_model(method)
        model.snowpacks.add(snowpack(removal=h.SnowRemoval(threshold=1,out_of_system=.1,to_impervious=.2,to_pervious=.1,immediate_melt=.1,to_subcatchment=0)))
        model.subcatchments.update('S',snowpack=Ref(collection='swmm:snowpacks',key='Snow'),polygon=(Point(x=0,y=0),Point(x=1,y=2)))
        model.update_climate(temperature=cl.SeriesTemperature(series=Ref(collection='swmm:timeseries',key='Air')),
            wind=cl.MonthlyWindSpeeds(values=(10.,)*12),snowmelt=cl.Snowmelt(snowfall_temperature=32,antecedent_weight=.5,negative_melt_ratio=.6,elevation=10,latitude=35,solar_time_correction=0),
            impervious_depletion=cl.ArealDepletion(fractions=(.5,)*10),pervious_depletion=cl.ArealDepletion(fractions=(.6,)*10),
            evaporation=cl.Evaporation(source=cl.MonthlyEvaporation(values=(.1,)*12),dry_only=False),
            adjustments=cl.ClimateAdjustments(temperature=cl.MonthlyTemperatureChanges(values=(1.,)*12),rainfall=cl.MonthlyFactors(values=(1.2,)*12)))
        model.timeseries.add(InlineTimeSeries(id='Air',points=(SeriesPoint(time=timedelta(),value=32),SeriesPoint(time=timedelta(days=3),value=40))))
        model.patterns.add(Pattern(id='Monthly',kind='MONTHLY',factors=(1,1.2)))
        model.subcatchment_adjustments.add(cl.SubcatchmentAdjustments(subcatchment=Ref(collection='swmm:subcatchments',key='S'),infiltration=Ref(collection='swmm:patterns',key='Monthly')))
        model.inflows.add(FlowInflow(node=Ref(collection='swmm:nodes',key='J'),baseline=.1,pattern=Ref(collection='swmm:patterns',key='Monthly')))
        model.dwf.add(DryWeatherFlow(node=Ref(collection='swmm:nodes',key='J'),baseline=.2,patterns=(None,Ref(collection='swmm:patterns',key='Monthly'))))
        yield model
    yield controlled(PROGRAM)


def supplemental_values():
    from easysewer.model.events import EventSchedule, RoutingEvent
    from easysewer.model.files import InterfaceFile
    from easysewer.model.project import ProjectTitle, JsonAnnotation, MapExtent, MapSettings, Backdrop, ObjectTag, MapLabel, MapLabels, ProfilePlot
    from easysewer.model.report import ReportOptions, ReportSelection
    series=Ref(collection='swmm:timeseries',key='Series')
    curve=Ref(collection='swmm:curves',key='Curve')
    file=FileReference(path='气候 data.dat',base_directory=r'D:\tmp',flavor='windows')
    inlet_model=Model.from_document(InpDocument.from_text(INLETS),strict=True)
    return (MapLabels(entries=(MapLabel(position=Point(x=2,y=3),text='label',anchor=Ref(collection='swmm:nodes',key='J')),)),
        ProfilePlot(name='North profile',links=(Ref(collection='swmm:links',key='P'),)),
        ObjectTag(target=Ref(collection='swmm:nodes',key='J'),text='雨水 node'),
        EventSchedule(periods=(RoutingEvent(start=datetime(2020,1,1),end=datetime(2020,1,2)),)),
        InterfaceFile(kind='HOTSTART', mode='USE', file=file), ProjectTitle(lines=('A title with "quotes" and ; semicolon',)), JsonAnnotation.from_value('easysewer:labels', {'name':'项目','number':2}),
        MapSettings(extent=MapExtent(lower_left=Point(x=0,y=0),upper_right=Point(x=100,y=200)),units='DEGREES'),
        Backdrop(file=file), ReportOptions(nodes=ReportSelection(mode='SELECTED',members=(Ref(collection='swmm:nodes',key='J'),)),averages=True),
        Offset.NODE_INVERT,FileTimeSeries(id='FileSeries',file=file),DayTime(clock=time(2,3,4),day_offset=3),MonthDay(month=2,day=28),
        n.NormalBoundary(),n.FixedBoundary(stage=1.234),n.TidalBoundary(curve=curve),n.SeriesBoundary(series=series),
        *inlet_model.inlets.values(),s.InletUsage(link=Ref(collection='swmm:links',key='P'),inlet=Ref(collection='swmm:inlets',key='G'),node=Ref(collection='swmm:nodes',key='J'),count=2,percent_clogged=25,maximum_flow=.1,local_depression=.2,local_width=2,placement='ON_GRADE'),
        cl.FileTemperature(),cl.ClimateFile(file=file,start_date=date(2020,1,2),units='C10'),cl.FileWind(),
        cl.ConstantEvaporation(rate=.1),cl.SeriesEvaporation(series=series),cl.TemperatureEvaporation(),cl.FileEvaporation(pan_coefficients=cl.MonthlyFactors(values=(.7,)*12)),
        h.FileRainfall(file=file,station='Station',units='MM',start_date=date(2020,1,3)),
        c.UnaryExpression(operator='-',operand=c.FunctionExpression(function='ABS',argument=c.ExpressionNumber(value=-2))),
        c.NumericSetting(value=.5),c.CurveSetting(curve=curve),c.SeriesSetting(series=series),c.PIDSetting(gain=.1,integral_time=timedelta(minutes=1),derivative_time=timedelta(seconds=3)))


class JsonTests(unittest.TestCase):
    def assert_graph_equal(self, before, after):
        self.assertEqual(before.profile,after.profile)
        for spec in before._store.specifications:
            self.assertEqual(list(before.collection(spec.key).items()),list(after.collection(spec.key).items()),spec.key)

    def test_domain_corpus_keeps_graph_order_variants_and_independent_source(self):
        for model in domain_models():
            with self.subTest(nodes=tuple(model.nodes),links=tuple(type(v).__name__ for v in model.links.values())):
                after=restore(model)
                self.assert_graph_equal(model,after)
                self.assertEqual(after.to_document().to_bytes(),model.to_document().to_bytes())
                rebuilt=restore(model,source=False)
                self.assert_graph_equal(model,rebuilt)
                rows=lambda document:[(line.section,line.values) for line in document.lines if line.kind=='data']
                self.assertEqual(rows(rebuilt.to_document(normalize=True)),rows(model.to_document(normalize=True)))

    def test_all_declared_types_have_real_domain_roundtrip_fixtures(self):
        registry=Model()._schema.json_types
        seen=set()
        def visit(value):
            seen.add(type(value))
            if isinstance(value,tuple):
                for child in value:
                    visit(child)
            elif is_dataclass(value):
                for field in fields(value):
                    visit(getattr(value,field.name))
        values=list(supplemental_values())
        for model in domain_models():
            values.extend(row for spec in model._store.specifications for row in model.collection(spec.key).values())
        for value in values:
            with self.subTest(kind=type(value).__name__):
                visit(value)
                self.assertEqual(registry.decode(registry.encode(value)),value)
        self.assertEqual({d.key for d in registry.declarations if d.value_type not in seen},set())

    def test_explicit_wire_names_and_temporal_precision_do_not_use_inp_tokens(self):
        model=network()
        model.update_options(rule_step=timedelta(),routing_step=timedelta(microseconds=123456),
            start_date=date(2020,2,29),end_time=DayTime(clock=time(1,2,3,456789),day_offset=2),sweep_start=MonthDay(month=3,day=1),ignore_rainfall=False)
        data=model.to_json_document().data
        value=entry(data,'swmm:options','settings')['value']
        self.assertEqual(value['type'],'swmm:options.options')
        self.assertEqual(value['rule_step'],dict(type='core:duration',days=0,seconds=0,microseconds=0))
        self.assertEqual(value['routing_step']['microseconds'],123456)
        self.assertEqual(value['end_time']['day_offset'],2)
        self.assertFalse(value['ignore_rainfall'])
        self.assertIsNone(value['report_start_date'])
        self.assert_graph_equal(model,restore(model))
        codec=model._schema.json_types
        for value in (timedelta(days=-1,microseconds=123),datetime(2020,2,29,1,2,3,456789,fold=1),time(1,2,3,456789,fold=1)):
            self.assertEqual(codec.decode(codec.encode(value)),value)
        restored=restore(model)
        restored.update_options(start_time=time(1,2,fold=0))
        restored=restore(restored)
        restored.update_options(start_time=time(1,2,fold=1))
        self.assertEqual(entry(restored.to_json_document().data,'swmm:options','settings')['value']['start_time']['fold'],1)

    def test_original_encoding_comments_unknown_inp_and_edits_survive_json(self):
        source=InpDocument.from_bytes('[TITLE]\r\n中文说明\r\n[JUNCTIONS]\nJ 2 ; keep\n[Future]\nx y\n'.encode('gb18030'),encoding='gb18030',source=r'D:\tmp\original\模型.inp')
        model=Model.from_document(source)
        model.nodes.update('J',elevation=3)
        restored=restore(model)
        self.assertEqual(restored.document.to_bytes(),source.to_bytes())
        self.assertEqual(restored.to_document().to_bytes(),model.to_document().to_bytes())
        self.assertEqual(restored.document.source,source.source)
        self.assertEqual(restored.document.encoding,source.encoding)
        with self.assertRaises(ValidationError):
            restored.nodes.rename('J','Renamed')

    def test_source_snapshot_never_resurrects_deleted_records(self):
        model=Model.from_document(InpDocument.from_text('[JUNCTIONS]\nJ 1\nUnused 2\n'),strict=True)
        model.nodes.remove('Unused')
        model.nodes.rename('J','Changed')
        result=restore(model)
        self.assertEqual(tuple(result.nodes),('Changed',))
        self.assertEqual(result.to_document().text,model.to_document().text)

    def test_json_copies_and_data_access_do_not_share_mutable_state(self):
        original=restore(network())
        clone=original.copy()
        clone.nodes.update('J',elevation=123)
        self.assertNotEqual(original.nodes['J'],clone.nodes['J'])
        data=original.json_document.data
        entry(data,'swmm:nodes','J')['value']['elevation']=999
        self.assertNotEqual(original.nodes['J'].elevation,999)
        self.assert_graph_equal(original,restore(original))

    def test_restored_graph_rename_and_unit_conversion_match_original(self):
        original=controlled(PROGRAM)
        restored=restore(original)
        for model in (original,restored):
            model.nodes.rename('J','上游')
            model.links.rename('P','Pipe')
            model.controls.rename(('VARIABLE','RateValue'),'FlowSignal')
            model.controls.rename(('EXPRESSION','AverageRate'),'MeanFlow')
            model.controls.rename(('RULE','first'),'Primary')
            model.controls.move(('RULE','second'),before=('RULE','Primary'))
            model.convert_units('CMS')
        self.assert_graph_equal(original,restored)
        self.assertEqual(original.to_document().text,restored.to_document().text)
        self.assert_graph_equal(restored,restore(restored))

    def test_unknown_field_is_preserved_through_safe_edit_but_blocks_lossy_export(self):
        data=network().to_json_document().data
        entry(data,'swmm:nodes','J')['value']['future_hydraulics']={'nested':[1,{'a':'b'}]}
        model=Model.from_json_document(JsonDocument.from_data(data))
        model.nodes.update('J',elevation=22)
        exported=model.to_json_document().data
        self.assertEqual(entry(exported,'swmm:nodes','J')['value']['future_hydraulics'],{'nested':[1,{'a':'b'}]})
        self.assertEqual(entry(exported,'swmm:nodes','J')['value']['elevation'],22)
        self.assertIn('json.unknown_field',{d.code for d in model.validate().diagnostics})
        with self.assertRaises(ValidationError):
            model.to_document()
        with self.assertRaises(ValidationError):
            model.nodes.rename('J','Moved')
        with self.assertRaises(ValidationError):
            model.convert_units('CMS')
        self.assertIn('json.incomplete_model',{d.code for d in model.validate(for_run=True).errors})

    def test_unknown_array_fields_cannot_be_attached_to_a_different_point(self):
        model=network()
        model.links.update('P',vertices=(Point(x=1,y=2),Point(x=3,y=4)))
        data=model.to_json_document().data
        entry(data,'swmm:links','P')['value']['vertices'][0]['label']='protected'
        model=Model.from_json_document(JsonDocument.from_data(data))
        model.links.update('P',length=200)
        self.assertEqual(entry(model.to_json_document().data,'swmm:links','P')['value']['vertices'][0]['label'],'protected')
        with self.assertRaises(ValidationError):
            with model.transaction():
                model.links.update('P',vertices=tuple(reversed(model.links['P'].vertices)))
        self.assertEqual(model.links['P'].vertices[0],Point(x=1,y=2))

    def test_unknown_collection_type_literal_and_root_extension_survive(self):
        data=network().to_json_document().data
        data['schema_version']='1.9'
        data['future_root']={'language':'中文'}
        data['extensions']={'vendor:settings':{'opaque':[1,2]}}
        entry(data,'swmm:links','P')['value']['type']='vendor:future_conduit'
        data['collections'].append({'collection':'vendor:records','records':[{'key':['one','two'],'value':{'type':'vendor:new','data':123}}]})
        model=Model.from_json_document(JsonDocument.from_data(data))
        self.assertEqual(len(model.links),0)
        self.assertEqual(model.to_json_document().data,data)
        self.assertIn('json.future_minor',{d.code for d in model.validate().diagnostics})
        curve_model=Model()
        curve_model.curves.add(Curve(id='C',kind='CONTROL',points=(CurvePoint(x=0,y=1),)))
        data=curve_model.to_json_document().data
        entry(data,'swmm:curves','C')['value']['kind']='NEW_CURVE_PURPOSE'
        model=Model.from_json_document(JsonDocument.from_data(data))
        self.assertFalse(model.curves)
        self.assertEqual(model.to_json_document().data,data)

    def test_bom_and_unusual_json_spacing_remain_exact_if_unchanged(self):
        text=network().to_json_document().text.replace('  ','\t').replace('\n','\r\n')
        doc=JsonDocument.from_bytes(codecs.BOM_UTF8+text.encode())
        restored=Model.from_json_document(doc)
        self.assertEqual(restored.to_json_document().to_bytes(),doc.to_bytes())

    def test_damaged_types_duplicate_ids_versions_and_temporal_values_are_rejected(self):
        base=network().to_json_document().data
        changes=(lambda d:d.update(schema_version='2.0'),lambda d:d.update(profile='unknown:engine'),
            lambda d:entry(d,'swmm:nodes','J')['value'].update(elevation=True),
            lambda d:entry(d,'swmm:nodes','J').update(key='Different'),
            lambda d:entry(d,'swmm:nodes','J')['value'].pop('id'),
            lambda d:d['collections'].append(d['collections'][0]),
            lambda d:next(b for b in d['collections'] if b['collection']=='swmm:nodes')['records'].append(entry(d,'swmm:nodes','J')),
            lambda d:entry(d,'swmm:options','settings')['value'].update(rule_step={'type':'core:duration','days':0,'seconds':1.5,'microseconds':0}))
        for change in changes:
            data=JsonDocument.from_data(base).data
            change(data)
            with self.subTest(data=data),self.assertRaises(ValidationError):
                Model.from_json_document(JsonDocument.from_data(data))
        for text in ('{"a":1,"a":2}','{"a":NaN}','{"a":Infinity}','{"a":9007199254740992}','[1,]'):
            with self.subTest(text=text),self.assertRaises(ValidationError):
                JsonDocument.from_text(text)
        with self.assertRaises(ValidationError):
            Model.from_json_document(JsonDocument.from_data({'rain':{},'calc':{}}))
        with self.assertRaises(ValidationError) as caught:
            JsonDocument.from_text('{\n"x": @\n}',source='broken.json')
        issue=caught.exception.report.errors[0]
        self.assertEqual((issue.code,issue.span.source,issue.span.line,issue.span.column),('json.syntax','broken.json',2,6))
        restored=restore(network())
        self.assertEqual(restored.nodes['J'].initial_depth,1)
        restored.nodes.update('J',initial_depth=True)
        with self.assertRaises(ValidationError):
            restored.to_json_document()

    def test_optional_contract_defaults_are_explicit_and_source_omission_is_retained(self):
        data=network().to_json_document().data
        value=entry(data,'swmm:nodes','J')['value']
        value.pop('ponded_area')
        model=Model.from_json_document(JsonDocument.from_data(data))
        self.assertIsNone(model.nodes['J'].ponded_area)
        self.assertNotIn('ponded_area',entry(model.to_json_document().data,'swmm:nodes','J')['value'])

    def test_extension_json_registration_promotion_aliases_and_schema_snapshot(self):
        schema=default_schema()
        schema.register(FeatureDescriptor(key='test:sensors',sections={'SENSORS'}),SensorCodec())
        model=Model(schema=schema)
        model.nodes.add(n.Junction(id='J',elevation=0))
        model.collection('test:sensors').add(Sensor(id='S',node=Ref(collection='swmm:nodes',key='J'),threshold=1))
        with self.assertRaises(ValidationError):
            model.to_json_document()
        schema.register_json(JsonType(key='test:sensor',value_type=Sensor,fields=(
            JsonField(name='id',attribute='id',shape=('string',)),
            JsonField(name='node',attribute='node',shape=('object','core:ref')),
            JsonField(name='limit',attribute='threshold',shape=('number',)),)))
        with self.assertRaises(ValidationError):
            model.to_json_document()  # Existing model snapshots did not change.
        promoted=Model.from_document(model.to_document(),schema=schema,strict=True)
        data=promoted.to_json_document().data
        self.assertEqual(entry(data,'test:sensors','S')['value']['limit'],1)
        opaque=Model.from_json_document(JsonDocument.from_data(data))
        self.assertEqual(opaque.to_json_document().data,data)
        restored=Model.from_json_document(opaque.to_json_document(),schema=schema,strict=True)
        restored.nodes.rename('J','Upstream')
        self.assertEqual(restored.collection('test:sensors')['S'].node.key,'Upstream')
        self.assertEqual(len(restored.to_document().records('SENSORS')),1)

        promoted.collection('test:sensors').remove('S')
        old_reader=Model.from_json_document(promoted.to_json_document())
        reenabled=Model.from_json_document(old_reader.to_json_document(),schema=schema,strict=True)
        self.assertFalse(reenabled.collection('test:sensors'))
        self.assertFalse(reenabled.to_document().records('SENSORS'))

    def test_new_source_codec_promotes_only_previously_unstructured_records(self):
        model=Model.from_document(InpDocument.from_text('[JUNCTIONS]\nJ 2\n[SENSORS]\nS J 1.5\n'))
        data=model.to_json_document()
        schema=default_schema()
        schema.register(FeatureDescriptor(key='test:sensors',sections={'SENSORS'}),SensorCodec())
        promoted=Model.from_json_document(data,schema=schema,strict=True)
        self.assertEqual(promoted.collection('test:sensors')['S'].threshold,1.5)
        self.assertEqual(len(promoted.to_document().records('SENSORS')),1)

    def test_increasing_migrations_report_actual_changes_and_unmapped_paths(self):
        data=network().to_json_document().data
        data['schema_version']='0.8'
        data['model_profile']=data.pop('profile')
        data['extensions']={'test:unmapped':{'value':1}}
        source=JsonDocument.from_data(data)
        registry=MigrationRegistry()
        def first(document):
            value=document.data
            value['schema_version']='0.9'
            value['profile']=value.pop('model_profile')
            return MigrationOutput(document=JsonDocument.from_data(value))
        def second(document):
            value=document.data
            value['schema_version']='1.0'
            return MigrationOutput(document=JsonDocument.from_data(value),unmapped=('$/extensions/test:unmapped',))
        registry.register('0.8','0.9',first)
        registry.register('0.9','1.0',second)
        result=registry.upgrade(source,target='1.0')
        self.assertEqual(result.steps,(('0.8','0.9'),('0.9','1.0')))
        self.assertEqual({c.path for c in result.changes},{'$/schema_version','$/model_profile','$/profile'})
        self.assertEqual(source.data['schema_version'],'0.8')
        restored=Model.from_json_document(source,migrations=registry)
        self.assertEqual(restored.json_migration.unmapped,('$/extensions/test:unmapped',))
        self.assertEqual(restored.to_json_document().data['extensions'],data['extensions'])
        with self.assertRaises(ValidationError):
            MigrationRegistry().upgrade(source,target='1.0')

    def test_real_file_move_preserves_resource_base_and_write_failures_are_atomic(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            original=root/'original'
            original.mkdir()
            (original/'rain data.dat').write_text('fixture')
            model=Model.from_document(InpDocument.from_text('[TIMESERIES]\nRain FILE "rain data.dat"\n',source=str(original/'source.inp')),strict=True)
            target=root/'moved'/'模型.json'
            model.to_json(target)
            restored=Model.from_json(target,strict=True)
            self.assertEqual(restored.timeseries['Rain'].file.resolve(),original/'rain data.dat')
            restored.to_inp(root/'export'/'result.inp')
            self.assertIn('original', (root/'export'/'result.inp').read_text())
            before=target.read_bytes()
            restored.timeseries.rename('Rain','Storm')
            for action in ('replace','fsync'):
                with patch('easysewer.io._atomic.os.'+action,side_effect=OSError('injected')):
                    with self.assertRaises(OSError):
                        restored.to_json(target)
                self.assertEqual(target.read_bytes(),before)
                self.assertEqual(list(target.parent.iterdir()),[target])

    def test_schema_has_declared_field_types_references_and_future_variants(self):
        schema=Model().json_schema()
        self.assertEqual(JsonDocument.read(Path(__file__).parents[1]/'docs'/'model-json-1.0.schema.json').data,schema)
        defs=schema['$defs']
        self.assertEqual(schema['properties']['kind'],{'const':'easysewer:model'})
        self.assertEqual(defs['swmm:network.conduit']['properties']['length'],{'type':'number'})
        self.assertEqual(defs['core:duration']['properties']['microseconds']['maximum'],999999)
        self.assertIn('swmm:controls.control_rule',defs)
        self.assertIn('unknown_value',defs)
        for declaration in Model()._schema.json_types.declarations:
            if declaration.singleton is None:
                self.assertEqual({f.name for f in fields(declaration.value_type)}, {f.attribute for f in declaration.fields},declaration.key)

    def test_reference_defaults_and_wire_registry_conflicts_are_explicit(self):
        schema=default_schema()
        original=schema.json_types.declarations
        with self.assertRaises(ValueError):
            schema.register_json(original[0])
        self.assertEqual(schema.json_types.declarations,original)
        detached=schema.json_types
        @dataclass(frozen=True,kw_only=True)
        class Extra:
            value: str
        detached.register(JsonType(key='test:extra',value_type=Extra,fields=(JsonField(name='value',attribute='value',shape=('string',)),)))
        self.assertEqual(schema.json_types.declarations,original)


if __name__=='__main__':
    unittest.main()
