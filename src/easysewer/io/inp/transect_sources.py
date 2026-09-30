"""Trace the NC state carried through X1 blocks and native finalizations."""
from dataclasses import replace
from ...model.identity import Ref
from .surface_sources import cover


class TransectSources:
    def __init__(self, source):
        self.source = source
        self.state = [(), (), ()]
        self.current = None

    def manning(self, line, old_values, new_values):
        # A positive NC token replaces the component; zero inherits its state.
        for i, value in enumerate(new_values):
            atom = (line, i + 1, 'value' if value > 0 else 'marker', True)
            if value > 0:
                self.state[i] = (atom,)
            else:
                self.state[i] += (atom,)
        for i in (0, 1):
            if old_values[i] == 0 and new_values[i] == 0:
                self.state[i] += tuple((l, c, 'derived', a) for l, c, _, a in self.state[2])

    def start(self, line):
        self.current = dict(line=line, roughness=tuple(self.state), stations=[])

    def stations(self, line):
        self.current['stations'].append(line)

    def finalize(self, *, wrong_target):
        if not wrong_target:
            self.current['roughness'] = tuple(self.state)
        # Native mutates Nchannel on each finalization, including section exits.
        self.state[2] = tuple((l, c, 'derived' if a else r, a) for l, c, r, a in self.state[2]) + (
            (self.current['line'], 7, 'derived', True),)

    def reconcile_owners(self, bindings):
        owners = {binding.line: binding.key[0] for binding in bindings}
        self.source.bindings = [replace(b, role='derived') if b.owner.canonical.key != owners[b.line] else b
                                for b in self.source.bindings]

    def save(self, record):
        owner = Ref(collection='swmm:transects', key=record.id)
        source = self.source
        cover(source, owner, record)
        seen = set()
        def bind(path, line, cols, role='value', active=True):
            key = path, line.number, tuple(cols), role, active
            if key in seen: return
            seen.add(key)
            source.add(owner, path, line, cols, role=role, contributes=active, overwrite=False)
        line = self.current['line']
        for name, col in (('id', 1), ('left_bank', 3), ('right_bank', 4),
                          ('meander_factor', 7), ('width_factor', 8), ('elevation_offset', 9)):
            bind((name,), line, (col,))
        bind(('stations',), line, (2,), 'retained', False)
        bind(('id',), line, (5, 6), 'retained', False)
        if len(line.values) > 10: bind(('id',), line, (10,), 'retained', False)
        for name, atoms in zip(('left', 'right', 'channel'), self.current['roughness']):
            for origin, col, role, active in atoms:
                bind(('roughness', name), origin, (col,), role, active)
                bind(('roughness',), origin, (col,), 'derived' if active else 'retained', active)
        index = 0
        for line in self.current['stations']:
            bind(('stations',), line, range(1, len(line.values)), 'derived')
            for col in range(1, len(line.values), 2):
                prefix = ('stations', index)
                bind(prefix, line, (col, col + 1), 'derived')
                bind(prefix + ('elevation',), line, (col,))
                bind(prefix + ('station',), line, (col + 1,))
                index += 1
