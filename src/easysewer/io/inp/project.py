"""Lossless title prose, comment-carried annotations and independent map units."""

import base64
from dataclasses import fields, replace
import re

from ...model.fields import validate_fields
from ...model.identity import Ref, canonical_key
from ...model.project import (Backdrop, JsonAnnotation, MapExtent, MapSettings, ProjectTitle,
    TITLE_COLLECTION, METADATA_COLLECTION, MAP_COLLECTION, BACKDROP_COLLECTION,
    ObjectTag, TAG_TARGETS, TAGS_COLLECTION, tag_key)
from ...model.units import UnitTransform
from ...model.values import Point
from ...schema.registry import DecodedFeature, FeatureDescriptor
from ...schema.structured import EncodedRow, FeatureData, FeatureEncoding, OmittedRecord, RecordEntry, SourceBinding
from ...validation import Diagnostic, Severity, SourceSpan, ValidationReport
from .geometry import finite_number, number_text
from .options import parse_option
from ...schema.option_profile import OPTIONS_BY_FIELD
from ...schema.display_fields import MAP_FIELD_RULES
from ...schema.project_fields import PROJECT_FIELD_RULES, TAG_FIELD_RULES
from .field_sources import FieldSources
from ...validation._cooperative import checkpointed

_PREFIX = ";@easysewer.annotation/"
_MARKER = re.compile(r";@easysewer\.annotation/1 ([A-Za-z0-9_-]+) ([1-9][0-9]*)/([1-9][0-9]*) ([A-Za-z0-9+/=]+)\Z")


def _span(document, line):
    return SourceSpan(source=document.source, line=line.number, column=1, end_column=len(line.content)+1)


def _marker_rows(annotation):
    key = base64.urlsafe_b64encode(annotation.key.encode()).decode().rstrip("=")
    data = base64.b64encode(annotation.json_text.encode()).decode()
    chunks = tuple(data[i:i+600] for i in checkpointed(range(0, len(data), 600)))
    return tuple(f"{_PREFIX}1 {key} {i+1}/{len(chunks)} {chunk}" for i, chunk in checkpointed(enumerate(chunks)))


class ProjectCodec:
    field_rules = PROJECT_FIELD_RULES
    descriptor = FeatureDescriptor(key="easysewer:project", sections=frozenset({"TITLE"}), raw_sections=frozenset({"TITLE"}),
                                   ordered_sections=frozenset({"TITLE"}), atomic_write=True)
    collections = (TITLE_COLLECTION, METADATA_COLLECTION)
    unit_transforms = (UnitTransform(value_type=JsonAnnotation, convert=lambda value, context: value),)

    def decode(self, document, profile):
        lines = tuple(line for section in checkpointed(document.sections) if section.name == "TITLE" for line in checkpointed(section.lines))
        groups, issues, records, bindings, claimed = {}, [], [], [], set()
        source_fields = FieldSources()
        lexical = {d.span.line for d in checkpointed(document.report.errors) if d.span}
        for line in checkpointed(lines):
            if line.number in lexical:
                continue
            match = _MARKER.fullmatch(line.content.strip())
            if match:
                try:
                    groups.setdefault(match[1], []).append((line, int(match[2]), int(match[3]), match[4]))
                except ValueError as error:
                    issues.append(Diagnostic(code="project.invalid_annotation", message=str(error), span=_span(document, line)))
            elif line.content.strip().startswith(_PREFIX):
                version = line.content.strip()[len(_PREFIX):].split(maxsplit=1)
                known = bool(version and version[0] == '1')
                issues.append(Diagnostic(code="project.invalid_annotation" if known else "project.unknown_annotation",
                    severity=Severity.ERROR if known else Severity.WARNING,
                    message="Malformed version 1 annotation marker" if known else "Unknown annotation marker retained as non-executable title text",
                    span=_span(document, line)))
        for group, parts in checkpointed(groups.items()):
            if not parts:
                continue
            try:
                key = base64.urlsafe_b64decode(group + "=" * (-len(group) % 4)).decode("utf-8")
                if base64.urlsafe_b64encode(key.encode()).decode().rstrip("=") != group:
                    raise ValueError("Annotation key requires canonical base64url encoding")
                total = parts[0][2]
                if total != len(parts) or any(count != total for _, _, count, _ in checkpointed(parts)) or tuple(index for _, index, _, _ in checkpointed(parts)) != tuple(range(1, len(parts)+1)):
                    raise ValueError("Annotation chunks must be complete, unique and ordered")
                text = base64.b64decode("".join(chunk for _, _, _, chunk in checkpointed(parts)), validate=True).decode("utf-8")
                annotation = JsonAnnotation(key=key, json_text=text)
                canonical_rows = _marker_rows(annotation)
                if len(canonical_rows) != len(parts):
                    raise ValueError("Noncanonical annotation chunk sizes; retain source rather than invent bindings")
                records.append(RecordEntry(collection="easysewer:metadata", value=annotation))
                owner = Ref(collection='easysewer:metadata', key=key)
                source_fields.cover(owner, ('key',), ('json_text',))
                for line, index, _, _ in checkpointed(parts):
                    bindings.append(SourceBinding(line=line.number, key=("annotation", key, str(index-1))))
                    claimed.add(line.number)
                    for name in checkpointed(('key', 'json_text')):
                        source_fields.add_line(owner, (name,), line, role='derived', overwrite=False)
            except (ValueError, UnicodeError) as error:
                issues.append(Diagnostic(code="project.invalid_annotation", message=str(error), span=_span(document, parts[0][0])))
        prose = tuple(line for line in checkpointed(lines) if line.number not in claimed and line.number not in lexical)
        if prose:
            records.append(RecordEntry(collection="swmm:title", value=ProjectTitle(lines=tuple(line.content for line in checkpointed(prose)))))
            bindings.extend(SourceBinding(line=line.number, key=("title", str(index))) for index, line in checkpointed(enumerate(prose)))
            owner = Ref(collection='swmm:title', key='text')
            source_fields.cover(owner, ('lines',))
            if any(line.number in lexical for line in checkpointed(lines)):
                source_fields.block_value(owner, ('lines',))
            for index, line in checkpointed(enumerate(prose)):
                source_fields.cover(owner, ('lines', index))
                source_fields.add_line(owner, ('lines',), line, overwrite=False)
                source_fields.add_line(owner, ('lines', index), line)
        owners = {Ref(collection=r.collection, key='text' if r.collection == 'swmm:title' else r.value.key).canonical: r.value for r in checkpointed(records)}
        return DecodedFeature(value=FeatureData(records=tuple(records), bindings=tuple(bindings),
            **source_fields.finish(owners)),
            claimed_lines=frozenset(b.line for b in checkpointed(bindings)), report=ValidationReport(diagnostics=tuple(issues)))

    def encode(self, store, profile):
        rows, omitted = [], []
        for title in checkpointed(store.collection("swmm:title").values()):
            owner = Ref(collection="swmm:title", key="text")
            rows.extend(EncodedRow(key=("title", str(index)), section="TITLE", values=(), raw_text=line, owners=(owner,))
                        for index, line in checkpointed(enumerate(title.lines)))
            if not title.lines:
                omitted.append(OmittedRecord(owner=owner, reason="Empty project title"))
        for annotation in checkpointed(store.collection("easysewer:metadata").values()):
            if type(annotation) is not JsonAnnotation:
                raise ValueError("Unregistered project annotation writer")
            owner = Ref(collection="easysewer:metadata", key=annotation.key)
            rows.extend(EncodedRow(key=("annotation", annotation.key, str(index)), section="TITLE", values=(), raw_text=line, owners=(owner,))
                        for index, line in checkpointed(enumerate(_marker_rows(annotation))))
        return FeatureEncoding(rows=tuple(rows), omitted=tuple(omitted))

    def validate(self, store, profile):
        title = store.collection("swmm:title").get("text", ProjectTitle())
        for index, line in enumerate(title.lines):
            if _MARKER.fullmatch(line.strip()):
                yield Diagnostic(code="project.reserved_annotation", field=f"title.lines[{index}]",
                    message="Version 1 annotation markers belong to metadata, not prose; use set_annotation")

    def validate_run(self, store, profile):
        title = store.collection("swmm:title").get("text", ProjectTitle())
        meaningful = tuple(line for line in title.lines if line.split(";", 1)[0].strip())
        if len(meaningful) > 3:
            yield Diagnostic(code="project.native_title_limit", severity=Severity.WARNING,
                message="SWMM 5.2.4 reports only the first three non-comment title lines; all project prose is retained")

    def validate_document(self, document, profile):
        for section in document.find_sections("TITLE"):
            for line in section.lines:
                encoding = 'utf-8' if document.encoding == 'utf-8-sig' else document.encoding
                if len(line.content.encode(encoding)) >= 1023:
                    yield Diagnostic(code="project.native_line_limit", span=_span(document, line),
                        message="Title line exceeds the fixed engine's physical input buffer; split the prose into shorter lines")


class TagsCodec:
    field_rules = TAG_FIELD_RULES
    descriptor = FeatureDescriptor(key='swmm:tags', sections=frozenset({'TAGS'}),
        ordered_sections=frozenset({'TAGS'}))
    collections = (TAGS_COLLECTION,)
    keywords = ('Gage', 'Subcatch', 'Node', 'Link')

    def decode(self, document, profile):
        rows, bindings, issues = {}, [], []
        source_fields = FieldSources()
        unknown_target = False
        lexical = {d.span.line for d in checkpointed(document.report.errors) if d.span}
        for line in checkpointed(document.records('TAGS')):
            owner = None
            if len(line.values) >= 2:
                index = next((i for i, word in checkpointed(enumerate(self.keywords))
                    if line.values[0].upper().startswith(word[:4].upper())), None)
                if index is not None:
                    try:
                        owner = Ref(collection='swmm:tags', key=(TAG_TARGETS[index], line.values[1]))
                    except ValueError:
                        pass
            if line.number in lexical:
                if owner is None: unknown_target = True
                else: source_fields.block(owner, ())
                continue
            def issue(code, message, severity=Severity.ERROR):
                issues.append(Diagnostic(code=code, message=message, severity=severity,
                    section='TAGS', span=_span(document, line)))
            try:
                if len(line.values) < 3:
                    raise ValueError('TAGS requires object class, object ID and tag text')
                # GUI 5.2.4 FindKeyWord compares the first four keyword letters.
                token = line.values[0].upper()
                index = next((i for i, word in checkpointed(enumerate(self.keywords))
                    if token.startswith(word[:4].upper())), None)
                if index is None:
                    issue('tag.unknown_class', 'Unknown tag object class remains source-owned', Severity.WARNING)
                    continue
                row = ObjectTag(target=Ref(collection=TAG_TARGETS[index], key=line.values[1]), text=line.values[2])
                ValidationReport(diagnostics=tuple(validate_fields(row))).raise_for_errors()
                key = canonical_key(tag_key(row))
                if key in rows:
                    issue('tag.repeated_assignment', 'The final tag assignment for this object takes effect', Severity.INFO)
                rows[key] = row
                bindings.append(SourceBinding(line=line.number, key=('TAGS', *key)))
                owner = Ref(collection='swmm:tags', key=key)
                paths = {('target',): (0, 1), ('target', 'collection'): (0,),
                         ('target', 'key'): (1,), ('text',): (2,)}
                source_fields.cover(owner, *paths)
                for path, tokens in checkpointed(paths.items()):
                    source_fields.add(owner, path, line, tokens,
                        role='derived' if path == ('target', 'collection') else 'value')
                if token != self.keywords[index].upper():
                    issue('tag.gui_keyword', 'GUI tag class prefix is normalized to its full keyword', Severity.INFO)
                if len(line.values) > 3:
                    issue('tag.ignored_columns', 'GUI ignores fields after the tag text; normalization removes them', Severity.WARNING)
            except (ValueError, TypeError) as error:
                if owner is not None: source_fields.block(owner, ())
                issue('tag.invalid_input', str(error))
        owners = {Ref(collection='swmm:tags', key=key).canonical: row for key, row in checkpointed(rows.items())}
        if unknown_target:
            for owner in checkpointed(owners): source_fields.block(owner, ())
        return DecodedFeature(value=FeatureData(
            records=tuple(RecordEntry(collection='swmm:tags', value=row) for row in checkpointed(rows.values())),
            bindings=tuple(bindings), **source_fields.finish(owners)), claimed_lines=frozenset(b.line for b in checkpointed(bindings)),
            report=ValidationReport(diagnostics=tuple(issues)))

    def encode(self, store, profile):
        for key, row in checkpointed(store.collection('swmm:tags').items()):
            if type(row) is not ObjectTag:
                raise ValueError('Unregistered object tag writer')
            yield EncodedRow(key=('TAGS', *canonical_key(key)), section='TAGS',
                values=(self.keywords[TAG_TARGETS.index(row.target.collection)], row.target.key, row.text),
                owners=(Ref(collection='swmm:tags', key=key),))

    def validate(self, store, profile):
        return ()


class MapCodec:
    field_rules = MAP_FIELD_RULES
    def file_uses(self, store, profile):
        from ...model.file_resources import FileUse
        value = store.collection("swmm:backdrop").get("image")
        if value is not None and value.file is not None:
            yield FileUse(owner=Ref(collection="swmm:backdrop", key="image"), path=("file",), file=value.file,
                role="swmm:backdrop", format="gui:image", required=False)

    descriptor = FeatureDescriptor(key="swmm:map", sections=frozenset({"MAP", "BACKDROP"}),
        ordered_sections=frozenset({'MAP', 'BACKDROP'}), atomic_write=True)
    collections = (MAP_COLLECTION, BACKDROP_COLLECTION)

    def decode(self, document, profile):
        values = {"MAP": {}, "BACKDROP": {}}
        bindings, issues = [], []
        source_fields = FieldSources()
        owners = {'MAP': Ref(collection='swmm:map', key='settings'),
                  'BACKDROP': Ref(collection='swmm:backdrop', key='image')}
        unit_lines = []
        def uncertain(section, keyword):
            paths = {'DIMENSIONS': ('extent',), 'UNITS': ('units',), 'FILE': ('file', 'clear_file'),
                     'OFFSET': ('legacy_offset',), 'SCALING': ('legacy_scaling',)}.get(keyword, ())
            source_fields.block(owners[section], *((name,) for name in checkpointed(paths)))
            if keyword == 'UNITS':
                source_fields.block(owners['MAP'], ('units_precedence',))
        last_units = None
        lexical = {d.span.line for d in checkpointed(document.report.errors) if d.span}
        # MAP and deprecated BACKDROP UNITS share a GUI variable; source order
        # matters even when declarations occur in repeated sections.
        for line in checkpointed(document.lines):
            if line.kind != 'data' or line.section not in values:
                continue
            section, field = line.section, None
            tokens = line.values
            keywords = ('DIMENSIONS', 'UNITS') if section == 'MAP' else ('FILE', 'DIMENSIONS', 'UNITS', 'OFFSET', 'SCALING')
            keyword = next((word for word in checkpointed(keywords) if tokens[0].upper().startswith(word[:4])), None) if tokens else None
            if line.number in lexical:
                uncertain(section, keyword)
                continue
            def issue(code, message, severity=Severity.ERROR):
                issues.append(Diagnostic(code=code, message=message, severity=severity,
                    section=section, field=field, span=_span(document, line)))
            try:
                if keyword is None:
                    issue('map.unknown_keyword', 'Unknown map/background setting remains source-owned', Severity.WARNING)
                    continue
                if keyword in ('FILE', 'UNITS') and len(tokens) < 2:
                    uncertain(section, keyword)
                    issue('map.ignored_line', 'GUI ignores this directive without a value; original line remains source-owned', Severity.WARNING)
                    continue
                if keyword == 'DIMENSIONS':
                    count, field = 5, 'extent'
                    if len(tokens) < count:
                        raise ValueError('DIMENSIONS requires four coordinates')
                    coordinates = tuple(finite_number(t) for t in checkpointed(tokens[1:5]))
                    value = MapExtent(lower_left=Point(x=coordinates[0], y=coordinates[1]),
                                      upper_right=Point(x=coordinates[2], y=coordinates[3]))
                    updates = {field: value}
                elif keyword == 'UNITS':
                    count, field = 2, 'units'
                    unit = next((u for u in checkpointed(('FEET', 'METERS', 'DEGREES', 'NONE')) if tokens[1].upper().startswith(u[0])), None)
                    if unit is None:
                        raise ValueError('Map unit must begin with F, M, D or N')
                    updates = {field: unit}
                    if tokens[1].upper() != unit:
                        issue('map.gui_unit', 'GUI unit prefix is normalized to its full name', Severity.INFO)
                elif keyword == 'FILE':
                    count, field = 2, 'file'
                    value = None
                    if tokens[1]:
                        value, _ = parse_option(OPTIONS_BY_FIELD['temp_directory'], tokens[1], source=document.source)
                        value = replace(value, direction='input')
                    updates = {'file': value, 'clear_file': not tokens[1]}
                else:
                    count, field = 3, 'legacy_offset' if keyword == 'OFFSET' else 'legacy_scaling'
                    if len(tokens) < count:
                        raise ValueError(f'{keyword} requires two values')
                    updates = {field: Point(x=finite_number(tokens[1]), y=finite_number(tokens[2]))}
                cls = MapSettings if section == 'MAP' else Backdrop
                ValidationReport(diagnostics=tuple(validate_fields(cls(**(values[section] | updates))))).raise_for_errors()
                if field in values[section]:
                    issue('map.repeated_assignment', 'The last assignment to this map/background field takes effect', Severity.INFO)
                values[section].update(updates)
                bindings.append(SourceBinding(line=line.number, key=(section, field)))
                owner = owners[section]
                def bind(path, indexes, *, role='value'):
                    source_fields.cover(owner, path)
                    source_fields.add(owner, path, line, indexes, role=role)
                if keyword == 'DIMENSIONS':
                    bind(('extent',), range(1, 5))
                    for name, start in checkpointed((('lower_left', 1), ('upper_right', 3))):
                        bind(('extent', name), (start, start + 1))
                        for axis, index in checkpointed((('x', start), ('y', start + 1))):
                            bind(('extent', name, axis), (index,))
                elif keyword == 'FILE':
                    bind(('file',), (1,), role='value' if value is not None else 'marker')
                    bind(('clear_file',), (1,), role='derived')
                    if value is not None:
                        for item in checkpointed(fields(value)):
                            bind(('file', item.name), (1,), role='value' if item.name == 'path' else 'derived')
                elif keyword == 'UNITS':
                    bind(('units',), (1,))
                    unit_lines.append((section, line))
                else:
                    bind((field,), (1, 2))
                    bind((field, 'x'), (1,))
                    bind((field, 'y'), (2,))
                if keyword == 'UNITS':
                    last_units = section
                if tokens[0].upper() != keyword:
                    issue('map.gui_keyword', 'GUI four-character keyword prefix is normalized', Severity.INFO)
                if len(tokens) > count:
                    issue('map.ignored_columns', 'GUI ignores trailing fields; normalization removes them', Severity.WARNING)
                if section == 'BACKDROP' and keyword in ('UNITS', 'OFFSET', 'SCALING'):
                    issue('map.legacy_directive', 'Deprecated background directive is retained as structured data', Severity.INFO)
            except (ValueError, TypeError) as error:
                uncertain(section, keyword)
                issue('map.invalid_input', str(error))
        if values['MAP'].get('units') is not None and values['BACKDROP'].get('units') is not None and last_units == 'BACKDROP':
            values['MAP']['units_precedence'] = 'BACKDROP'
        records = []
        if values["MAP"]:
            records.append(RecordEntry(collection="swmm:map", value=MapSettings(**values["MAP"])))
        if values["BACKDROP"]:
            records.append(RecordEntry(collection="swmm:backdrop", value=Backdrop(**values["BACKDROP"])))
        original_records = {}
        for section, cls in checkpointed((('MAP', MapSettings), ('BACKDROP', Backdrop))):
            if values[section]:
                original_records[owners[section].canonical] = cls(**values[section])
                source_fields.cover(owners[section], *((f.name,) for f in checkpointed(fields(cls))))
        last_by_section = {section: line.number for section, line in checkpointed(unit_lines)}
        if values['MAP']:
            for section, line in checkpointed(unit_lines):
                source_fields.add(owners['MAP'], ('units_precedence',), line, (1,), role='derived',
                    overwrite=False, contributes=line.number == last_by_section[section])
        return DecodedFeature(value=FeatureData(records=tuple(records), bindings=tuple(bindings),
                                               **source_fields.finish(original_records)),
            claimed_lines=frozenset(b.line for b in checkpointed(bindings)), report=ValidationReport(diagnostics=tuple(issues)))

    def encode(self, store, profile):
        rows, omitted = [], []
        settings = store.collection('swmm:map').get('settings', MapSettings())
        backdrop = store.collection('swmm:backdrop').get('image', Backdrop())
        order = [('MAP', 'swmm:map', 'settings'), ('BACKDROP', 'swmm:backdrop', 'image')]
        if settings.units is not None and backdrop.units is not None and settings.units_precedence == 'MAP':
            order.reverse()
        for section, namespace, key in checkpointed(order):
            for record in checkpointed(store.collection(namespace).values()):
                if type(record) is not (MapSettings if section == 'MAP' else Backdrop) or (
                    record.extent is not None and type(record.extent) is not MapExtent):
                    raise ValueError('Unregistered map/background writer')
                owner, start = Ref(collection=namespace, key=key), len(rows)
                if record.extent is not None:
                    a, b = record.extent.lower_left, record.extent.upper_right
                    rows.append(EncodedRow(key=(section, "extent"), section=section,
                        values=("DIMENSIONS", *(number_text(v) for v in checkpointed((a.x, a.y, b.x, b.y)))), owners=(owner,)))
                if record.units is not None:
                    rows.append(EncodedRow(key=(section, "units"), section=section, values=("UNITS", record.units), owners=(owner,)))
                if section == 'BACKDROP':
                    if record.file is not None or record.clear_file:
                        rows.append(EncodedRow(key=(section, 'file'), section=section,
                            values=('FILE', record.file.path if record.file is not None else ''), owners=(owner,)))
                    for field, keyword in checkpointed((('legacy_offset', 'OFFSET'), ('legacy_scaling', 'SCALING'))):
                        point = getattr(record, field)
                        if point is not None:
                            rows.append(EncodedRow(key=(section, field), section=section,
                                values=(keyword, number_text(point.x), number_text(point.y)), owners=(owner,)))
                if len(rows) == start:
                    omitted.append(OmittedRecord(owner=owner, reason="No explicit map/background settings"))
        return FeatureEncoding(rows=tuple(rows), omitted=tuple(omitted))

    def validate(self, store, profile):
        settings = store.collection('swmm:map').get('settings', MapSettings())
        backdrop = store.collection('swmm:backdrop').get('image', Backdrop())
        if not ValidationReport(diagnostics=(*validate_fields(settings), *validate_fields(backdrop))).is_valid:
            return
        for value, cls in ((settings, MapSettings), (backdrop, Backdrop)):
            if type(value) is not cls or (value.extent is not None and type(value.extent) is not MapExtent):
                yield Diagnostic(code='map.unsupported_variant', message='Map/background variants require an explicitly registered writer')
        if settings.units_precedence == 'BACKDROP' and (settings.units is None or backdrop.units is None):
            yield Diagnostic(code='map.unit_precedence', field='units_precedence',
                message='BACKDROP precedence requires explicit units in both MAP and BACKDROP')
