"""Graph contract tests using hydraulic objects and an independently keyed relation."""

from dataclasses import dataclass, replace
import unittest

from easysewer.model import CollectionSpec, OpaqueConstraint, RecordConflictError, RecordStore, Ref
from easysewer.model.geometry import Circular, CrossSection, Custom
from easysewer.model.network import Conduit, Junction, network_collections
from easysewer.validation import ValidationError


def node_ref(key):
    return Ref(collection="swmm:nodes", key=key)


@dataclass(frozen=True, kw_only=True)
class Load:
    node: Ref
    constituent: str
    factor: float


LOADS = CollectionSpec(key="test:loads", record_type=Load,
                      key_of=lambda row: (row.node.key, row.constituent))


class GraphTests(unittest.TestCase):
    def setUp(self):
        self.store = RecordStore((*network_collections(), LOADS))
        self.nodes = self.store.collection("swmm:nodes")
        self.links = self.store.collection("swmm:links")
        self.loads = self.store.collection("test:loads")
        self.nodes.add(Junction(id="A", elevation=2))
        self.nodes.add(Junction(id="B", elevation=1))
        self.links.add(Conduit(id="A", inlet=node_ref("a"), outlet=node_ref("B"),
                               length=100, roughness=.013,
                               section=CrossSection(geometry=Circular(diameter=1))))
        self.loads.add(Load(node=node_ref("A"), constituent="FLOW", factor=1))

    def test_namespaces_order_and_ascii_case(self):
        self.assertEqual(list(self.nodes), ["A", "B"])
        self.assertEqual(self.nodes["a"].id, "A")
        self.assertEqual(self.links["a"].id, "A")
        with self.assertRaises(RecordConflictError):
            self.nodes.add(Junction(id="a", elevation=9))
        self.nodes.add(Junction(id="ä", elevation=0))
        self.nodes.add(Junction(id="Ä", elevation=0))
        self.assertEqual(len(self.nodes), 4)

    def test_rename_updates_roles_and_derived_keys_only_in_target_namespace(self):
        self.links.update("A", outlet=node_ref("a"))
        self.nodes.rename("a", "New")
        self.assertEqual(list(self.nodes), ["New", "B"])
        self.assertEqual(list(self.links), ["A"])
        self.assertEqual(self.links["A"].inlet, node_ref("New"))
        self.assertEqual(self.links["A"].outlet, node_ref("New"))
        self.assertEqual(list(self.loads), [("New", "FLOW")])
        self.assertEqual(len(self.store.referenced_by(node_ref("new"))), 3)
        self.assertTrue(self.store.validate().is_valid)

    def test_case_only_rename_updates_spelling_everywhere(self):
        self.nodes.rename("A", "a")
        self.assertEqual(self.loads[("A", "flow")].node.key, "a")
        self.assertEqual(list(self.nodes), ["a", "B"])

    def test_replace_cannot_bypass_identity_changes(self):
        with self.assertRaises(RecordConflictError):
            self.nodes.replace("a", replace(self.nodes["A"], id="a"))
        with self.assertRaises(RecordConflictError):
            self.nodes.update("A", id="Z")
        self.nodes.replace("a", replace(self.nodes["A"], elevation=3))
        self.assertEqual(self.nodes["A"].elevation, 3)

    def test_failed_rename_has_no_partial_changes(self):
        revision = self.store.revision
        with self.assertRaises(RecordConflictError):
            self.nodes.rename("A", "b")
        self.assertEqual(self.store.revision, revision)
        self.assertEqual(self.loads[("A", "FLOW")].node.key, "A")
        with self.assertRaises(ValueError):
            self.nodes.rename("A", "bad name")
        self.assertEqual(self.store.revision, revision)

    def test_rename_collision_in_derived_relation_rolls_back(self):
        self.loads.add(Load(node=node_ref("Future"), constituent="FLOW", factor=2))
        revision = self.store.revision
        with self.assertRaises(RecordConflictError):
            self.nodes.rename("A", "Future")
        self.assertEqual(list(self.nodes), ["A", "B"])
        self.assertEqual(self.store.revision, revision)

    def test_delete_preview_and_explicit_cascade(self):
        plan = self.store.deletion_plan(node_ref("a"))
        self.assertEqual({row.collection for row in plan}, {"swmm:nodes", "swmm:links", "test:loads"})
        with self.assertRaises(ValidationError):
            self.nodes.remove("A")
        self.assertEqual(len(self.nodes), 2)
        self.assertEqual(self.nodes.remove("A", cascade=True), plan)
        self.assertEqual(list(self.nodes), ["B"])
        self.assertFalse(self.links)
        self.assertFalse(self.loads)

    def test_scoped_opaque_data_blocks_only_related_changes(self):
        self.store.set_opaque_constraints((OpaqueConstraint(description="Unparsed control", collections={"swmm:links"}),))
        self.nodes.rename("B", "Outlet")
        with self.assertRaises(ValidationError):
            self.links.rename("A", "P")
        with self.assertRaises(ValidationError):
            self.nodes.remove("A", cascade=True)
        self.assertEqual(len(self.nodes), 2)
        self.assertEqual(len(self.links), 1)

    def test_transaction_failure_restores_data_constraints_specs_and_revision(self):
        revision = self.store.revision
        with self.assertRaises(ValidationError):
            with self.store.transaction():
                self.nodes.rename("A", "New")
                self.links.update("A", outlet=node_ref("Missing"))
                self.store.register(CollectionSpec(key="test:extra", record_type=Junction, key_of=lambda row: row.id))
                self.store.set_opaque_constraints((OpaqueConstraint(description="unknown"),))
        self.assertEqual(list(self.nodes), ["A", "B"])
        self.assertEqual(self.store.revision, revision)
        self.assertEqual(self.store.opaque_constraints, ())
        self.assertEqual(len(self.store.specifications), 3)
        self.assertTrue(self.store.validate().is_valid)

    def test_nested_transactions_and_clone_are_independent(self):
        with self.store.transaction():
            self.nodes.update("A", elevation=4)
            with self.assertRaises(RuntimeError):
                with self.store.transaction():
                    self.nodes.update("A", elevation=9)
                    raise RuntimeError("rollback inner")
            self.assertEqual(self.nodes["A"].elevation, 4)
        clone = self.store.clone()
        clone.collection("swmm:nodes").rename("A", "Copy")
        self.assertEqual(list(self.nodes), ["A", "B"])
        self.assertEqual(self.links["A"].inlet.key, "a")

    def test_recursive_variants_and_wrong_reference_kind_are_validated(self):
        self.links.update("A", section=CrossSection(geometry=Custom(
            full_depth=1, curve=Ref(collection="swmm:curves", key="shape"))))
        report = self.store.validate()
        self.assertEqual(report.errors[0].field, "section.geometry.curve")
        self.links.update("A", inlet=Ref(collection="swmm:links", key="A"))
        self.assertIn("model.invalid_field", [item.code for item in self.store.validate().errors])

    def test_mutable_or_cyclic_extension_values_are_rejected(self):
        @dataclass(frozen=True)
        class Extension:
            id: str
            content: object
        self.store.register(CollectionSpec(key="test:extension", record_type=Extension, key_of=lambda row: row.id))
        rows = self.store.collection("test:extension")
        with self.assertRaises(TypeError):
            rows.add(Extension("x", [1, 2]))
        cyclic = Extension("x", None)
        object.__setattr__(cyclic, "content", cyclic)
        with self.assertRaisesRegex(TypeError, "cyclic"):
            rows.add(cyclic)

    def test_graph_cycles_use_refs_and_have_finite_deletion_plan(self):
        @dataclass(frozen=True)
        class Item:
            id: str
            other: Ref
        self.store.register(CollectionSpec(key="test:cycle", record_type=Item, key_of=lambda row: row.id))
        rows = self.store.collection("test:cycle")
        rows.add(Item("one", Ref(collection="test:cycle", key="two")))
        rows.add(Item("two", Ref(collection="test:cycle", key="one")))
        self.assertEqual(len(rows.remove("one", cascade=True)), 2)


if __name__ == "__main__":
    unittest.main()
