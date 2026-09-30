"""Independent feature promotion through Model without changing its implementation."""

from dataclasses import dataclass, replace
import unittest

from easysewer.io.inp import InpDocument
from easysewer.io.inp.network import default_schema
from easysewer.model import CollectionSpec, Model, Ref
from easysewer.model.fields import number, reference, validate_fields
from easysewer.schema import DecodedFeature, FeatureDescriptor, RegistryError, SwmmProfile
from easysewer.schema.structured import EncodedRow, FeatureData, RecordEntry, SourceBinding
from easysewer.validation import ValidationReport
from easysewer.model.units import UnitTransform


@dataclass(frozen=True, kw_only=True)
class Sensor:
    id: str
    node: Ref = reference("swmm:nodes")
    threshold: float = number("depth", minimum=0)


class SensorCodec:
    collections = (CollectionSpec(key="test:sensors", record_type=Sensor, key_of=lambda row: row.id,
                                   identity_field="id", validate=validate_fields),)

    def decode(self, document, profile):
        records, bindings = [], []
        for line in document.records("SENSORS"):
            identity, node, threshold = line.values
            sensor = Sensor(id=identity, node=Ref(collection="swmm:nodes", key=node), threshold=float(threshold))
            records.append(RecordEntry(collection="test:sensors", value=sensor))
            bindings.append(SourceBinding(line=line.number, key=(identity,)))
        return DecodedFeature(value=FeatureData(records=tuple(records), bindings=tuple(bindings)),
                              claimed_lines=frozenset(item.line for item in bindings))

    def encode(self, store, profile):
        for sensor in store.collection("test:sensors").values():
            yield EncodedRow(key=(sensor.id,), section="SENSORS",
                             values=(sensor.id, sensor.node.key, str(sensor.threshold)),
                             owners=(Ref(collection="test:sensors", key=sensor.id),))

    def validate(self, store, profile):
        return ()


class ExtensionTests(unittest.TestCase):
    def test_raw_text_requires_explicit_section_contract(self):
        from easysewer.schema.structured import FeatureEncoding
        class RawCodec:
            collections = ()
            def encode(self, store, profile):
                return FeatureEncoding(rows=(EncodedRow(key=('raw',), section='NOTES', values=(), raw_text='literal "text', owners=()),))
            def decode(self, document, profile):
                return DecodedFeature(value=FeatureData())
            def validate(self, store, profile):
                return ()
        schema = default_schema()
        schema.register(FeatureDescriptor(key='test:raw', sections={'NOTES'}), RawCodec())
        with self.assertRaisesRegex(RegistryError, 'declared raw section'):
            Model(schema=schema).to_document()
        schema = default_schema()
        schema.register(FeatureDescriptor(key='test:raw', sections={'NOTES'}, raw_sections={'NOTES'}), RawCodec())
        self.assertIn('literal "text', Model(schema=schema).to_document().text)
        with self.assertRaises(ValueError):
            FeatureDescriptor(key='test:raw', sections={'NOTES'}, raw_sections={'REPORT'})

    def test_raw_writer_cannot_inject_headers_newlines_or_nul(self):
        from easysewer.io.inp.project import ProjectCodec
        class RawCodec(ProjectCodec):
            def encode(self, store, profile):
                return (EncodedRow(key=('raw',), section='TITLE', values=(), raw_text=self.text, owners=()),)
        from easysewer.schema.structured import ModelSchema
        for text in ('one\ntwo', '[OPTIONS]', ' \t[REPORT]', 'nul\x00'):
            codec = RawCodec(); codec.text = text
            schema = ModelSchema(); schema.register(codec.descriptor, codec)
            with self.assertRaisesRegex(RegistryError, 'physical line'):
                Model(schema=schema).to_document()

    def test_explicit_unit_transform_extends_conversion_without_core_changes(self):
        from easysewer.model.network import Junction
        class CalibrationCodec(SensorCodec):
            unit_transforms = (UnitTransform(value_type=Sensor, convert=lambda value, context:
                                replace(value, threshold=context.number(value.threshold, "depth"))),)
        schema = default_schema()
        schema.register(FeatureDescriptor(key="test:sensors", sections={"SENSORS"}), CalibrationCodec())
        model = Model(schema=schema)
        model.nodes.add(Junction(id="J", elevation=10))
        model.collection("test:sensors").add(Sensor(id="S", node=Ref(collection="swmm:nodes", key="J"), threshold=2))
        model.convert_units("CMS")
        output = model.to_document()
        rebuilt = Model.from_document(output, schema=schema, strict=True)
        self.assertAlmostEqual(rebuilt.collection("test:sensors")["S"].threshold, .6096)
        self.assertAlmostEqual(rebuilt.nodes["J"].elevation, 3.048)

    def test_promotion_read_edit_rename_and_write_have_one_owner(self):
        document = InpDocument.from_text("[JUNCTIONS]\nJ 2\n[SENSORS]\nS J 1.5 ; limit\n")
        schema = default_schema()
        original = Model.from_document(document, schema=schema)
        self.assertEqual(len(original.support.opaque_records), 1)
        schema.register(FeatureDescriptor(key="test:sensors", sections={"SENSORS"}), SensorCodec())
        promoted = Model.from_document(document, schema=schema, strict=True)
        self.assertEqual(len(promoted.support.opaque_records), 0)
        self.assertEqual(len(original.support.opaque_records), 1)
        promoted.nodes.rename("J", "Moved")
        promoted.collection("test:sensors").update("S", threshold=2.5)
        output = promoted.to_document()
        self.assertEqual(len(output.records("SENSORS")), 1)
        self.assertIn("; limit", output.text)
        reread = Model.from_document(output, schema=schema, strict=True)
        self.assertEqual(reread.collection("test:sensors")["S"].node.key, "Moved")
        self.assertEqual(reread.collection("test:sensors")["S"].threshold, 2.5)

    def test_profile_cannot_silently_drop_structured_records(self):
        from easysewer.model.network import Junction
        profile = SwmmProfile(key="test:future", engine_version="future", sections={"JUNCTIONS"})
        model = Model(profile=profile)
        model.nodes.add(Junction(id="J", elevation=1))
        with self.assertRaisesRegex(RegistryError, "no active INP writer"):
            model.to_document()

    def test_codec_must_bind_every_claimed_line(self):
        class BrokenCodec(SensorCodec):
            def decode(self, document, profile):
                return DecodedFeature(value=FeatureData(), claimed_lines=frozenset({2}))
        schema = default_schema()
        schema.register(FeatureDescriptor(key="test:sensors", sections={"SENSORS"}), BrokenCodec())
        with self.assertRaisesRegex(RegistryError, "bind every claimed line"):
            Model.from_document(InpDocument.from_text("[SENSORS]\nS J 1\n"), schema=schema)


if __name__ == "__main__":
    unittest.main()
