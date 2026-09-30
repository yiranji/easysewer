"""Small decoder helper for explicit assignments, not positional inference."""

from dataclasses import replace

from ...model.inspection import field_at
from ...schema.structured import FieldBinding, FieldCoverage, FieldLineBinding


class FieldSources:
    def __init__(self):
        self.bindings = []
        self.coverage = set()
        self.uncertain = set()
        self.uncertain_values = set()
        self._active = {}

    def cover(self, owner, *paths):
        self.coverage.update((owner.canonical, path) for path in paths)

    def block(self, owner, *prefixes):
        self.uncertain.update((owner.canonical, path) for path in prefixes)

    def block_value(self, owner, path):
        self.uncertain_values.add((owner.canonical, path))

    def add(self, owner, path, line, tokens, *, role='value', overwrite=True, contributes=True):
        binding = FieldBinding(owner=owner, path=path, line=line.number,
                               tokens=tuple(tokens), role=role, contributes=contributes)
        self._append(binding, overwrite)

    def add_line(self, owner, path, line, *, role='value', overwrite=True, contributes=True):
        self._append(FieldLineBinding(owner=owner, path=path, line=line.number,
                                      role=role, contributes=contributes), overwrite)

    def _append(self, binding, overwrite):
        key = binding.owner.canonical, binding.path
        if overwrite and binding.contributes:
            for index in self._active.get(key, ()):
                self.bindings[index] = replace(self.bindings[index], contributes=False)
            self._active[key] = []
        index = len(self.bindings)
        self.bindings.append(binding)
        if binding.contributes:
            self._active.setdefault(key, []).append(index)

    def finish(self, records):
        """Nested paths removed by a later parent assignment have no final field."""
        def exists(owner, path):
            if owner not in records:
                return False
            try:
                field_at(records[owner], path)
                return True
            except KeyError:
                return False
        coverage = tuple(FieldCoverage(owner=owner, path=path)
            for owner, path in sorted(self.coverage, key=lambda row: (row[0].collection, repr(row[0].key), repr(row[1])))
            if exists(owner, path) and (owner, path) not in self.uncertain_values and not any(
                (owner, path[:size]) in self.uncertain for size in range(len(path) + 1)))
        bindings = tuple(binding for binding in self.bindings if exists(binding.owner.canonical, binding.path))
        return dict(field_coverage=coverage, field_bindings=bindings)
