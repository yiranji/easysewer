"""Ordered GUI map labels and named profile plots, independent of hydraulics."""
import re
from dataclasses import fields

from ...model.fields import validate_fields
from ...model.identity import Ref, canonical_key
from ...model.project import MapLabel, MapLabels, ProfilePlot, LABELS_COLLECTION, PROFILES_COLLECTION
from ...model.units import UnitTransform
from ...model.values import Point
from ...schema import DecodedFeature, FeatureDescriptor
from ...schema.structured import EncodedRow, FeatureData, FeatureEncoding, OmittedRecord, RecordEntry, SourceBinding
from ...validation import Diagnostic, Severity, SourceSpan, ValidationReport
from .geometry import finite_number, number_text
from .field_sources import FieldSources
from ...schema.display_fields import LABEL_FIELD_RULES
from ...schema.project_fields import PROFILE_FIELD_RULES
from ...validation._cooperative import checkpointed


def _span(document, line):
    return SourceSpan(source=document.source, line=line.number, column=1, end_column=len(line.content) + 1)


def _integer(token):
    if not re.fullmatch(r'[+-]?[0-9]+', token):
        raise ValueError('GUI font fields require integer values')
    value = int(token)
    if not -2147483648 <= value <= 2147483647:
        raise ValueError('GUI font fields require signed 32-bit integers')
    return value


class LabelsCodec:
    descriptor = FeatureDescriptor(key='swmm:labels', sections=frozenset({'LABELS'}),
        ordered_sections=frozenset({'LABELS'}), atomic_write=True)
    collections = (LABELS_COLLECTION,)
    field_rules = LABEL_FIELD_RULES
    # Font points and map coordinates do not follow the model's flow-unit system.
    unit_transforms = (UnitTransform(value_type=MapLabel, convert=lambda value, context: value),)

    def decode(self, document, profile):
        entries, bindings, issues = [], [], []
        source_fields = FieldSources()
        owner = Ref(collection='swmm:labels', key='layer')
        lexical = {d.span.line for d in checkpointed(document.report.errors) if d.span}
        for line in checkpointed(document.records('LABELS')):
            if line.number in lexical:
                source_fields.block_value(owner, ('entries',))
                continue
            def issue(code, message, severity=Severity.ERROR):
                issues.append(Diagnostic(code=code, message=message, severity=severity,
                    section='LABELS', span=_span(document, line)))
            try:
                tokens = line.values
                if len(tokens) < 3:
                    raise ValueError('LABELS requires X, Y and text')
                args = dict(position=Point(x=finite_number(tokens[0]), y=finite_number(tokens[1])), text=tokens[2])
                if len(tokens) >= 4 and tokens[3]:
                    args['anchor'] = Ref(collection='swmm:nodes', key=tokens[3])
                if len(tokens) >= 5:
                    args['font_name'] = tokens[4]
                if len(tokens) >= 6:
                    args['font_size'] = _integer(tokens[5])
                for index, name in checkpointed(((6, 'bold'), (7, 'italic'))):
                    if len(tokens) <= index:
                        continue
                    token = tokens[index].upper()
                    if token in ('YES', 'NO'):
                        args[name] = token == 'YES'
                        issue('label.manual_boolean',
                            'The manual uses YES/NO but GUI 5.2.4 requires integers; normalize to write 0/1', Severity.WARNING)
                    else:
                        number = _integer(token)
                        args[name] = number == 1
                        if number not in (0, 1):
                            issue('label.gui_boolean', 'GUI treats only 1 as true; normalization writes 0/1', Severity.INFO)
                value = MapLabel(**args)
                ValidationReport(diagnostics=tuple(validate_fields(value))).raise_for_errors()
                bindings.append(SourceBinding(line=line.number, key=('LABELS', str(len(entries)))))
                index = len(entries)
                prefix = ('entries', index)
                source_fields.cover(owner, ('entries',), prefix,
                    *(prefix + (f.name,) for f in checkpointed(fields(MapLabel))))
                source_fields.add(owner, ('entries',), line, range(min(8, len(tokens))), overwrite=False)
                source_fields.add(owner, prefix, line, range(min(8, len(tokens))))
                def bind(path, indexes, *, role='value'):
                    full_path = prefix + path
                    source_fields.cover(owner, full_path)
                    source_fields.add(owner, full_path, line, indexes, role=role)
                bind(('position',), (0, 1))
                bind(('position', 'x'), (0,))
                bind(('position', 'y'), (1,))
                bind(('text',), (2,))
                for token_index, name in checkpointed(((3, 'anchor'), (4, 'font_name'), (5, 'font_size'), (6, 'bold'), (7, 'italic'))):
                    if len(tokens) > token_index:
                        bind((name,), (token_index,), role='marker' if name == 'anchor' and not tokens[3] else 'value')
                if value.anchor is not None:
                    bind(('anchor', 'key'), (3,))
                    bind(('anchor', 'collection'), (3,), role='derived')
                entries.append(value)
                if len(tokens) > 8:
                    issue('label.ignored_columns', 'GUI ignores fields after italic; normalization removes them', Severity.WARNING)
            except (ValueError, TypeError, OverflowError) as error:
                source_fields.block_value(owner, ('entries',))
                issue('label.invalid_input', str(error))
        records = (RecordEntry(collection='swmm:labels', value=MapLabels(entries=tuple(entries))),) if entries else ()
        return DecodedFeature(value=FeatureData(records=records, bindings=tuple(bindings),
            **source_fields.finish({owner.canonical: records[0].value} if records else {})),
            claimed_lines=frozenset(b.line for b in checkpointed(bindings)), report=ValidationReport(diagnostics=tuple(issues)))

    def encode(self, store, profile):
        layer = store.collection('swmm:labels').get('layer')
        if layer is None:
            return FeatureEncoding()
        if type(layer) is not MapLabels or any(type(row) is not MapLabel for row in checkpointed(layer.entries)):
            raise ValueError('Unregistered map label writer')
        owner = Ref(collection='swmm:labels', key='layer')
        if not layer.entries:
            return FeatureEncoding(omitted=(OmittedRecord(owner=owner, reason='No map labels'),))
        return FeatureEncoding(rows=tuple(EncodedRow(key=('LABELS', str(index)), section='LABELS',
            values=(number_text(row.position.x), number_text(row.position.y), row.text,
                    row.anchor.key if row.anchor else '', row.font_name, str(row.font_size),
                    '1' if row.bold else '0', '1' if row.italic else '0'), owners=(owner,))
            for index, row in checkpointed(enumerate(layer.entries))))

    def validate(self, store, profile):
        layer = store.collection('swmm:labels').get('layer', MapLabels())
        if not ValidationReport(diagnostics=tuple(validate_fields(layer))).is_valid:
            return
        if type(layer) is not MapLabels or any(type(row) is not MapLabel for row in layer.entries):
            yield Diagnostic(code='label.unsupported_variant', section='LABELS',
                message='Map label extensions require an explicitly registered writer')
            return
        for index, row in enumerate(layer.entries):
            if row.anchor is not None and not store.contains(row.anchor):
                if store.contains(Ref(collection='swmm:subcatchments', key=row.anchor.key)):
                    yield Diagnostic(code='label.anchor_node_only', section='LABELS', field=f'entries[{index}].anchor',
                        message='GUI 5.2.4 anchors labels only to nodes; the same-named subcatchment cannot supply this anchor')


class ProfilesCodec:
    field_rules = PROFILE_FIELD_RULES
    descriptor = FeatureDescriptor(key='swmm:profiles', sections=frozenset({'PROFILES'}),
        ordered_sections=frozenset({'PROFILES'}), atomic_write=True)
    collections = (PROFILES_COLLECTION,)

    def decode(self, document, profile):
        groups, bindings, issues = {}, [], []
        source_fields = FieldSources()
        unknown_name = False
        lexical = {d.span.line for d in checkpointed(document.report.errors) if d.span}
        for line in checkpointed(document.records('PROFILES')):
            owner = None
            if line.values:
                try:
                    owner = Ref(collection='swmm:profiles', key=line.values[0])
                except ValueError:
                    pass
            if line.number in lexical:
                if owner is None: unknown_name = True
                else: source_fields.block(owner, ('links',))
                continue
            try:
                if len(line.values) < 2:
                    if owner is not None: source_fields.block(owner, ('links',))
                    issues.append(Diagnostic(code='profile.ignored_line', section='PROFILES', severity=Severity.WARNING,
                        message='GUI ignores profile lines without links; the original line remains source-owned', span=_span(document, line)))
                    continue
                name, *names = line.values
                row = ProfilePlot(name=name, links=tuple(Ref(collection='swmm:links', key=key) for key in checkpointed(names)))
                ValidationReport(diagnostics=tuple(validate_fields(row))).raise_for_errors()
                key = canonical_key(name)
                first = key not in groups
                if key not in groups:
                    groups[key] = (name, [])
                links = groups[key][1]
                # Source chunk sizes differ from the writer's five-link chunks.
                # Several source lines may bind to the same effective output row.
                bindings.append(SourceBinding(line=line.number, key=('PROFILES', key, str(len(links) // 5))))
                owner = Ref(collection='swmm:profiles', key=key)
                source_fields.cover(owner, ('name',), ('links',))
                source_fields.add(owner, ('name',), line, (0,), overwrite=False,
                                  role='value' if first else 'retained', contributes=first)
                source_fields.add(owner, ('links',), line, range(1, len(line.tokens)), overwrite=False)
                for offset in checkpointed(range(len(row.links))):
                    path = ('links', len(links) + offset)
                    source_fields.cover(owner, path, (*path, 'collection'), (*path, 'key'))
                    source_fields.add(owner, path, line, (offset + 1,))
                    source_fields.add(owner, (*path, 'key'), line, (offset + 1,))
                    source_fields.add(owner, (*path, 'collection'), line, (offset + 1,), role='derived')
                links.extend(row.links)
            except (ValueError, TypeError) as error:
                if owner is not None: source_fields.block(owner, ('links',))
                issues.append(Diagnostic(code='profile.invalid_input', section='PROFILES',
                    message=str(error), span=_span(document, line)))
        records = tuple(RecordEntry(collection='swmm:profiles', value=ProfilePlot(name=name, links=tuple(links)))
            for name, links in checkpointed(groups.values()))
        owners = {Ref(collection='swmm:profiles', key=r.value.name).canonical: r.value for r in checkpointed(records)}
        if unknown_name:
            for owner in checkpointed(owners): source_fields.block(owner, ('links',))
        return DecodedFeature(value=FeatureData(records=records, bindings=tuple(bindings), **source_fields.finish(owners)),
            claimed_lines=frozenset(b.line for b in checkpointed(bindings)), report=ValidationReport(diagnostics=tuple(issues)))

    def encode(self, store, profile):
        for name, row in checkpointed(store.collection('swmm:profiles').items()):
            if type(row) is not ProfilePlot:
                raise ValueError('Unregistered profile plot writer')
            for offset in checkpointed(range(0, len(row.links), 5)):
                yield EncodedRow(key=('PROFILES', canonical_key(name), str(offset // 5)), section='PROFILES',
                    values=(name, *(link.key for link in checkpointed(row.links[offset:offset + 5]))),
                    owners=(Ref(collection='swmm:profiles', key=name),))

    def validate(self, store, profile):
        for name, row in store.collection('swmm:profiles').items():
            if type(row) is not ProfilePlot:
                yield Diagnostic(code='profile.unsupported_variant', section='PROFILES', object_id=name,
                    message='Profile plot extensions require an explicitly registered writer')
                continue
            if not ValidationReport(diagnostics=tuple(validate_fields(row))).is_valid:
                continue
            possible_ends = None
            for index, target in enumerate(row.links):
                if not store.contains(target):
                    possible_ends = None
                    continue
                link = store.collection('swmm:links')[target.key]
                if not ValidationReport(diagnostics=tuple(validate_fields(link))).is_valid:
                    continue
                if not hasattr(link, 'inlet') or not hasattr(link, 'outlet'):
                    continue
                a, b = link.inlet.canonical, link.outlet.canonical
                ends = {a, b} if possible_ends is None else (
                    ({b} if a in possible_ends else set()) | ({a} if b in possible_ends else set()))
                if not ends:
                    yield Diagnostic(code='profile.disconnected', section='PROFILES', object_id=name,
                        field=f'links[{index}]', severity=Severity.WARNING,
                        message='Ordered profile links cannot form a continuous path in either direction; plotting may fail')
                possible_ends = ends or {a, b}
