"""Trusted parsing must not disable checks outside its private scope."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import math
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from easysewer.io.inp.durations import DurationCodec
from easysewer.io.inp.field_sources import FieldSources
from easysewer.io.inp.geometry import finite_number, number_text
from easysewer.model._trusted import active, trusted_scope
from easysewer.model.fields import validate_fields
from easysewer.model.identity import Ref, canonical_key, require_immutable
from easysewer.model.network import Junction
from easysewer.model.units import UnitContext, UnitRules
from easysewer.model.values import FileReference
from easysewer.schema.structured import RecordEntry
from easysewer.validation._cooperative import checkpoint, checkpoint_scope


class TrustedInpScopeTests(unittest.TestCase):
    def tearDown(self):
        self.assertFalse(active())

    def test_nested_scopes_restore_state_after_failure(self):
        self.assertFalse(active())
        with trusted_scope():
            self.assertTrue(active())
            with self.assertRaisesRegex(RuntimeError, "decode failed"):
                with trusted_scope():
                    self.assertTrue(active())
                    raise RuntimeError("decode failed")
            self.assertTrue(active())
        self.assertFalse(active())

    def test_other_threads_keep_normal_validation(self):
        def other_thread():
            self.assertFalse(active())
            with self.assertRaises(ValueError):
                FileReference(path="")
            with trusted_scope():
                self.assertTrue(active())
            return active()

        with trusted_scope(), ThreadPoolExecutor(max_workers=1) as executor:
            self.assertFalse(executor.submit(other_thread).result(timeout=5))
            self.assertTrue(active())

    def test_checkpoint_callbacks_use_normal_checks_and_restore_trust(self):
        observed = []
        invalid = Junction(id="J", elevation=0.0, max_depth=-1.0)

        def callback():
            observed.append(active())
            with self.assertRaises(ValueError):
                FileReference(path="")
            self.assertTrue(tuple(validate_fields(invalid)))
            # A nested import can opt into its own scope without leaking it
            # into the rest of the caller's callback.
            with trusted_scope():
                self.assertTrue(active())
            self.assertFalse(active())

        with trusted_scope():
            with checkpoint_scope(callback):
                self.assertTrue(active())
                checkpoint()
                self.assertTrue(active())
            self.assertTrue(active())
        self.assertEqual(observed, [False, False, False])

    def test_checkpoint_failure_preserves_exception_and_restores_scope(self):
        failure = ValueError("caller cancelled")

        def callback():
            self.assertFalse(active())
            raise failure

        with trusted_scope():
            with self.assertRaises(ValueError) as caught:
                with checkpoint_scope(callback):
                    self.fail("The entry callback should stop this operation")
            self.assertIs(caught.exception, failure)
            self.assertTrue(active())

    def test_field_validation_is_skipped_only_during_trusted_conversion(self):
        value = Junction(id="J", elevation=0.0, max_depth=-1.0)
        self.assertTrue(tuple(validate_fields(value)))
        with trusted_scope():
            self.assertEqual(tuple(validate_fields(value)), ())
        self.assertTrue(tuple(validate_fields(value)))

    def test_constructor_and_immutable_checks_remain_public(self):
        with self.assertRaises(ValueError):
            FileReference(path="")
        with self.assertRaises(TypeError):
            RecordEntry(collection="swmm:nodes", value=[])
        with trusted_scope():
            self.assertEqual(FileReference(path="").path, "")
            self.assertEqual(RecordEntry(collection="swmm:nodes", value=[]).value, [])
            require_immutable([])
        with self.assertRaises(TypeError):
            require_immutable([])
        with self.assertRaises(ValueError):
            FileReference(path="")

    def test_trusted_values_still_receive_normalization_and_detachment(self):
        factors = [[name, 1.0] for name in ("CFS", "GPM", "MGD", "CMS", "LPS", "MLD")]
        with trusted_scope():
            context = UnitContext(flow_units="cms")
            rules = UnitRules(key="test", flow_from_cfs=factors)
        self.assertEqual(context.flow_units, "CMS")
        self.assertIsInstance(rules.flow_from_cfs, tuple)
        self.assertTrue(all(isinstance(pair, tuple) for pair in rules.flow_from_cfs))
        factors[0][1] = 2.0
        self.assertEqual(rules.flow_from_cfs[0], ("CFS", 1.0))

    def test_identity_normalization_preserves_ascii_only_matching(self):
        key = ("pipe", "stra\u00dfe")
        expected = canonical_key(key)
        with trusted_scope():
            self.assertEqual(canonical_key(key), expected)
            self.assertEqual(Ref(collection="swmm:links", key="pipe").canonical.key, "PIPE")
        self.assertEqual(expected, ("PIPE", "STRA\u00dfE"))

    def test_numeric_conversion_keeps_values_without_legality_scan(self):
        with self.assertRaises(ValueError):
            finite_number("nan")
        with trusted_scope():
            self.assertTrue(math.isnan(finite_number("nan")))
            self.assertEqual(number_text(-0.0), "-0.0")
            value = finite_number("1.2345678901234567")
            self.assertEqual(float(number_text(value)), value)
            with self.assertRaises(ValueError):
                finite_number("not-a-number")
        with self.assertRaises(ValueError):
            finite_number("nan")

    def test_duration_conversion_never_silently_truncates_precision(self):
        codec = DurationCodec(numeric_unit="seconds")
        with trusted_scope():
            for token in ("0.0000001", "00:00:00.0000001", "1e-20"):
                with self.subTest(token=token), self.assertRaises(ValueError):
                    codec.parse(token)
            value = codec.parse("0.000001")
            self.assertEqual(value, timedelta(microseconds=1))
            self.assertEqual(codec.parse(codec.format(value)), value)
            self.assertEqual(codec.parse("-1"), timedelta(seconds=-1))
            with self.assertRaises(ValueError):
                DurationCodec(numeric_unit="seconds", resolution=timedelta(seconds=1)).parse("0.5")
        with self.assertRaises(ValueError):
            codec.parse("-1")

    def test_trusted_decode_does_not_build_field_declarations(self):
        owner = Ref(collection="swmm:nodes", key="J")
        line = SimpleNamespace(number=1)
        with patch("easysewer.io.inp.field_sources.FieldBinding", side_effect=AssertionError("source allocation")):
            with trusted_scope():
                sources = FieldSources()
                sources.cover(owner, ("elevation",))
                sources.add(owner, ("elevation",), line, (1,))
                self.assertEqual(sources.finish({}), {"field_coverage": (), "field_bindings": ()})
        sources = FieldSources()
        sources.cover(owner, ("elevation",))
        self.assertIn((owner.canonical, ("elevation",)), sources.coverage)


if __name__ == "__main__":
    unittest.main()
