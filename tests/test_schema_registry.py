import unittest
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from easysewer.io.inp import InpDocument
from easysewer.schema import (
    EPA_SWMM_5_2_4, DecodedFeature, FeatureDescriptor, RegistryError,
    SchemaRegistry, SupportLevel, SwmmProfile,
)
from easysewer.validation import Diagnostic, Severity, ValidationReport


@dataclass(frozen=True)
class DryWeatherFlow:
    node: str
    baseline: Decimal
    patterns: tuple[str, ...]


class FlowDecoder:
    """A test extension for one variant of a shared section, not production DWF."""

    def decode(self, document, profile):
        values, claims, errors = [], [], []
        for row in document.records('DWF'):
            if len(row.values) < 2 or row.values[1].upper() != 'FLOW':
                continue
            try:
                baseline = Decimal(row.values[2])
                if not baseline.is_finite():
                    raise InvalidOperation
            except (IndexError, InvalidOperation):
                errors.append(Diagnostic(code='example.invalid_flow', message='Invalid FLOW baseline',
                                         span=row.tokens[0].span, feature='example:dwf.flow'))
                continue
            values.append(DryWeatherFlow(row.values[0], baseline, row.values[3:]))
            claims.append(row.number)
        return DecodedFeature(value=tuple(values), claimed_lines=frozenset(claims),
                              report=ValidationReport(diagnostics=tuple(errors)))


class RecordDecoder:
    def __init__(self, sections, predicate=lambda row: True):
        self.sections = sections
        self.predicate = predicate

    def decode(self, document, profile):
        rows = tuple(row for name in self.sections for row in document.records(name)
                     if self.predicate(row))
        return DecodedFeature(value=tuple(row.values for row in rows),
                              claimed_lines=frozenset(row.number for row in rows))


class ClaimDecoder:
    def __init__(self, claims):
        self.claims = claims

    def decode(self, document, profile):
        return DecodedFeature(value=None, claimed_lines=frozenset(self.claims))


class TestSchemaRegistry(unittest.TestCase):
    def test_catalog_recognition_is_not_structured_support(self):
        document = InpDocument.from_text('[DWF]\nN1 FLOW 2\n[Future]\na b\n')
        decoded = SchemaRegistry().decode(document)
        self.assertEqual(len(EPA_SWMM_5_2_4.sections), 57)
        self.assertEqual([row.number for row in decoded.opaque_records], [2, 4])
        self.assertEqual(decoded.support_for(2), SupportLevel.PRESERVED)
        self.assertEqual(decoded.report.diagnostics[0].code, 'schema.unknown_section')
        self.assertEqual(decoded.report.diagnostics[0].severity, Severity.INFO)
        self.assertEqual(decoded.document.to_bytes(), document.to_bytes())

    def test_partial_section_support_preserves_other_constituents(self):
        document = InpDocument.from_text('[DWF]\nN1 FLOW 2.5 P1\nN1 TSS 30\n[DWF]\nN2 FLOW 4\n')
        registry = SchemaRegistry()
        registry.register(FeatureDescriptor(key='example:dwf.flow', sections={'DWF'}), FlowDecoder())
        decoded = registry.decode(document)
        self.assertEqual(decoded.features['example:dwf.flow'].value,
                         (DryWeatherFlow('N1', Decimal('2.5'), ('P1',)),
                          DryWeatherFlow('N2', Decimal('4'), ())))
        self.assertEqual([r.values for r in decoded.opaque_records], [('N1', 'TSS', '30')])
        self.assertEqual(decoded.support_for(2), SupportLevel.STRUCTURED)
        self.assertEqual(decoded.support_for(3), SupportLevel.PRESERVED)
        self.assertEqual(decoded.document.to_bytes(), document.to_bytes())

    def test_bad_record_is_diagnosed_and_remains_unclaimed(self):
        document = InpDocument.from_text('[DWF]\nN1 FLOW invalid\nN2 FLOW 1\n')
        registry = SchemaRegistry()
        registry.register(FeatureDescriptor(key='example:dwf.flow', sections={'DWF'}), FlowDecoder())
        decoded = registry.decode(document)
        self.assertFalse(decoded.report.is_valid)
        self.assertEqual(decoded.report.errors[0].span.line, 2)
        self.assertEqual([row.number for row in decoded.opaque_records], [2])
        self.assertEqual(decoded.support_for(2), SupportLevel.PRESERVED)

    def test_explicit_promotion_keeps_old_snapshot_and_single_source(self):
        document = InpDocument.from_text('[DWF]\nN1 FLOW 2\nN1 TSS 30\n')
        registry = SchemaRegistry()
        registry.register(FeatureDescriptor(key='example:dwf.flow', sections={'DWF'}), FlowDecoder())
        before = registry.decode(document)
        registry.register(FeatureDescriptor(key='example:dwf.pollutants', sections={'DWF'}),
                          RecordDecoder(['DWF'], lambda row: row.values[1] != 'FLOW'))
        after = registry.decode(document)
        self.assertEqual(len(before.opaque_records), 1)
        self.assertEqual(len(after.opaque_records), 0)
        self.assertEqual(len(after.owners), 2)
        self.assertIs(before.document, after.document)
        self.assertEqual(after.document.to_bytes(), document.to_bytes())
        with self.assertRaises(TypeError):
            before.owners[3] = 'wrong'
        with self.assertRaises(TypeError):
            before.features['new'] = None

    def test_decoder_can_aggregate_multiple_lines_and_sections(self):
        document = InpDocument.from_text(
            '[LID_CONTROLS]\nBio BC\nBio SURFACE 100 0 0.1 1 0\n'
            '[LID_USAGE]\nS1 Bio 2 10 1 0 30 0\n[LID_CONTROLS]\nBio SOIL 500 .5 .2 .1 5 10 100\n'
        )
        registry = SchemaRegistry()
        registry.register(FeatureDescriptor(key='example:lid', sections={'LID_CONTROLS', 'LID_USAGE'}),
                          RecordDecoder(['LID_CONTROLS', 'LID_USAGE']))
        decoded = registry.decode(document)
        self.assertEqual(len(decoded.features['example:lid'].value), 4)
        self.assertFalse(decoded.opaque_records)
        self.assertEqual(len(decoded.owners), 4)

    def test_duplicate_registration_is_rejected_without_replacing_decoder(self):
        registry = SchemaRegistry()
        descriptor = FeatureDescriptor(key='example:dwf', sections={'DWF'})
        registry.register(descriptor, FlowDecoder())
        with self.assertRaises(RegistryError):
            registry.register(descriptor, ClaimDecoder({999}))
        result = registry.decode(InpDocument.from_text('[DWF]\nN1 FLOW 1\n'))
        self.assertEqual(len(result.owners), 1)

    def test_competing_claims_fail(self):
        registry = SchemaRegistry()
        for key in ('example:a', 'example:b'):
            registry.register(FeatureDescriptor(key=key, sections={'DWF'}), FlowDecoder())
        with self.assertRaisesRegex(RegistryError, 'claimed by both'):
            registry.decode(InpDocument.from_text('[DWF]\nN1 FLOW 1\n'))

    def test_invalid_source_claims_are_rejected(self):
        document = InpDocument.from_text('[DWF]\n; comment\nN1 FLOW 1\n\n[OPTIONS]\nRULE_STEP 0\n')
        for claimed in (1, 2, 4, 5, 6, 100):
            with self.subTest(claimed=claimed):
                registry = SchemaRegistry()
                registry.register(FeatureDescriptor(key='example:test', sections={'DWF'}),
                                  ClaimDecoder({claimed}))
                with self.assertRaises(RegistryError):
                    registry.decode(document)

    def test_dependency_order_does_not_require_registration_order(self):
        calls = []

        class EmptyDecoder:
            def __init__(self, key):
                self.key = key

            def decode(self, document, profile):
                calls.append(self.key)
                return DecodedFeature(value=())

        registry = SchemaRegistry()
        registry.register(FeatureDescriptor(key='example:b', sections={'DWF'}, requires=('example:a',)),
                          EmptyDecoder('b'))
        registry.register(FeatureDescriptor(key='example:a', sections={'PATTERNS'}), EmptyDecoder('a'))
        registry.decode(InpDocument.from_text(''))
        self.assertEqual(calls, ['a', 'b'])

    def test_missing_dependency_and_cycle_are_errors(self):
        missing = SchemaRegistry()
        missing.register(FeatureDescriptor(key='example:a', sections={'DWF'}, requires=('example:b',)),
                         ClaimDecoder(set()))
        with self.assertRaisesRegex(RegistryError, 'Missing'):
            missing.decode(InpDocument.from_text(''))
        missing.register(FeatureDescriptor(key='example:b', sections={'DWF'}, requires=('example:a',)),
                         ClaimDecoder(set()))
        with self.assertRaisesRegex(RegistryError, 'Cyclic'):
            missing.decode(InpDocument.from_text(''))

    def test_unsupported_profile_and_dependencies_keep_source(self):
        registry = SchemaRegistry()
        registry.register(FeatureDescriptor(key='example:a', sections={'DWF'}, profiles={'other:1'}),
                          ClaimDecoder({2}))
        registry.register(FeatureDescriptor(key='example:b', sections={'DWF'}, requires=('example:a',)),
                          ClaimDecoder({2}))
        document = InpDocument.from_text('[DWF]\nN1 FLOW 1\n')
        result = registry.decode(document)
        self.assertEqual(len(result.opaque_records), 1)
        self.assertEqual(len(result.report.diagnostics), 2)
        self.assertFalse(result.features)
        self.assertEqual(result.document.to_bytes(), document.to_bytes())

    def test_local_registry_has_no_implicit_discovery(self):
        populated = SchemaRegistry()
        populated.register(FeatureDescriptor(key='example:dwf.flow', sections={'DWF'}), FlowDecoder())
        document = InpDocument.from_text('[DWF]\nN1 FLOW 1\n')
        self.assertFalse(populated.decode(document).opaque_records)
        self.assertEqual(len(SchemaRegistry().decode(document).opaque_records), 1)

    def test_bad_extension_errors_are_not_swallowed(self):
        class BrokenDecoder:
            def decode(self, document, profile):
                raise RuntimeError('extension bug')

        registry = SchemaRegistry()
        registry.register(FeatureDescriptor(key='example:broken', sections={'DWF'}), BrokenDecoder())
        with self.assertRaisesRegex(RuntimeError, 'extension bug'):
            registry.decode(InpDocument.from_text('[DWF]\nN1 FLOW 1\n'))

    def test_descriptor_normalizes_sections_but_requires_namespaces(self):
        descriptor = FeatureDescriptor(key='example:dwf', sections={'[ dwf ]'}, ordered_sections={'dwf'})
        self.assertEqual(descriptor.sections, frozenset({'DWF'}))
        self.assertEqual(descriptor.ordered_sections, frozenset({'DWF'}))
        with self.assertRaises(ValueError):
            FeatureDescriptor(key='example:dwf', sections={'DWF'}, ordered_sections={'STORAGE'})
        with self.assertRaises(ValueError):
            FeatureDescriptor(key='DWF', sections={'DWF'})
        with self.assertRaises(RegistryError):
            SchemaRegistry().register(
                FeatureDescriptor(key='example:opaque', sections={'DWF'}, support=SupportLevel.PRESERVED),
                ClaimDecoder(set()),
            )

    def test_profile_is_explicit_and_immutable(self):
        source = {'OPTIONS'}
        profile = SwmmProfile(key='test:1', engine_version='1', sections=source)
        source.add('DWF')
        self.assertEqual(profile.sections, frozenset({'OPTIONS'}))
        document = InpDocument.from_text('[DWF]\nN1 FLOW 1\n')
        result = SchemaRegistry().decode(document, profile=profile)
        self.assertEqual(result.report.diagnostics[0].code, 'schema.unknown_section')


if __name__ == '__main__':
    unittest.main()
