"""Contribution history and bounded work for unrelated source declarations."""
import random
from types import SimpleNamespace
import unittest

from easysewer.io.inp.field_sources import FieldSources
from easysewer.model import Ref, Point
from easysewer.model.network import Junction


class CountedList(list):
    visits = 0

    def __iter__(self):
        for value in super().__iter__():
            self.visits += 1
            yield value


class CountedSet(set):
    visits = 0

    def __iter__(self):
        for value in super().__iter__():
            self.visits += 1
            yield value


class FieldSourceScalingTests(unittest.TestCase):
    def test_contributors_match_later_assignment_policy_with_aggregates(self):
        randomizer = random.Random(117)
        for trial in range(20):
            fields = FieldSources()
            operations = []
            for index in range(120):
                owner = Ref(collection='swmm:nodes', key=randomizer.choice(('J', 'j')) + str(randomizer.randrange(5)))
                path = randomizer.choice((('max_depth',), ('position',), ('position', 'x')))
                overwrite, contributes = bool(randomizer.randrange(2)), bool(randomizer.randrange(2))
                role = randomizer.choice(('value', 'marker', 'derived', 'retained'))
                operations.append((owner, path, overwrite, contributes, role))
                fields.add(owner, path, SimpleNamespace(number=index+1), (1,),
                           overwrite=overwrite, contributes=contributes, role=role)
            for index, (owner, path, _, contributes, role) in enumerate(operations):
                expected = contributes and not any(
                    later_owner.canonical == owner.canonical and later_path == path and assign and active
                    for later_owner, later_path, assign, active, _ in operations[index+1:])
                actual = fields.bindings[index]
                self.assertEqual((actual.owner, actual.path, actual.line, actual.tokens, actual.role, actual.contributes),
                                 (owner, path, index+1, (1,), role, expected), (trial, index))

    def test_invalid_new_binding_preserves_existing_contributors(self):
        fields = FieldSources()
        owner = Ref(collection='swmm:nodes', key='J')
        fields.add(owner, ('max_depth',), SimpleNamespace(number=1), (2,))
        before = tuple(fields.bindings)
        for args in (dict(line=SimpleNamespace(number=0), tokens=(2,)),
                     dict(line=SimpleNamespace(number=2), tokens=()),
                     dict(line=SimpleNamespace(number=2), tokens=(1, 1))):
            with self.assertRaises(ValueError):
                fields.add(owner, ('max_depth',), **args)
            self.assertEqual(tuple(fields.bindings), before)
        fields.add(owner, ('max_depth',), SimpleNamespace(number=3), (2,))
        self.assertEqual([row.contributes for row in fields.bindings], [False, True])

    def test_distinct_owners_do_not_rescan_all_prior_declarations(self):
        fields = FieldSources()
        fields.bindings = CountedList()
        for index in range(2000):
            fields.add(Ref(collection='swmm:nodes', key=f'J{index}'), ('max_depth',),
                       SimpleNamespace(number=index+1), (2,))
        self.assertLessEqual(fields.bindings.visits, 4000)
        self.assertEqual(len(fields.bindings), 2000)

    def test_invalid_rows_for_other_owners_do_not_multiply_coverage_work(self):
        fields = FieldSources()
        fields.uncertain = CountedSet()
        records = {}
        for index in range(800):
            owner = Ref(collection='swmm:nodes', key=f'J{index}')
            records[owner.canonical] = Junction(id=f'J{index}', elevation=0, position=Point(x=1, y=2))
            fields.cover(owner, ('max_depth',), ('position',), ('position', 'x'), ('position', 'y'))
            fields.block(owner, ('position',))
        result = fields.finish(records)
        self.assertLessEqual(fields.uncertain.visits, 1600)
        self.assertEqual({(c.owner, c.path) for c in result['field_coverage']},
                         {(owner, ('max_depth',)) for owner in records})
        first = next(iter(records))
        fields.block(first, ())
        fields.block_value(Ref(collection='swmm:nodes', key='J1'), ('max_depth',))
        self.assertEqual(len(fields.finish(records)['field_coverage']), 798)


if __name__ == '__main__':
    unittest.main()
