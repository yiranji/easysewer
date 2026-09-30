"""Project prose, informational annotations and map settings, without GUI state."""

from dataclasses import dataclass
from typing import Literal

from .fields import reference, validate_fields
from .identity import Ref, namespace_key, validate_identifier
from .store import CollectionSpec
from .values import FileReference, Point
from ..validation import Diagnostic


@dataclass(frozen=True, kw_only=True)
class ProjectTitle:
    lines: tuple[str, ...] = ()

    def validate_local(self):
        for index, line in enumerate(self.lines):
            if any(c in line for c in "\r\n\x00") or line.lstrip().startswith("["):
                yield Diagnostic(code="project.title_line", message="Title entries must be physical lines without section headers", field=f"lines[{index}]")


@dataclass(frozen=True, kw_only=True)
class ProjectAnnotation:
    key: str

    def __post_init__(self):
        namespace_key(self.key)
        if len(self.key) > 100:
            raise ValueError("Annotation key exceeds the portable INP marker limit")


@dataclass(frozen=True, kw_only=True)
class JsonAnnotation(ProjectAnnotation):
    """Non-executable, unit-neutral JSON; embedded strings are not model Ref values."""
    json_text: str

    def __post_init__(self):
        super().__post_init__()
        from ..io.json.document import parse
        parse(self.json_text)

    @classmethod
    def from_value(cls, key, value):
        from ..io.json import JsonDocument
        return cls(key=key, json_text=JsonDocument.from_data(value).text)

    @property
    def value(self):
        from ..io.json.document import parse
        return parse(self.json_text)


@dataclass(frozen=True, kw_only=True)
class MapExtent:
    lower_left: Point
    upper_right: Point


@dataclass(frozen=True, kw_only=True)
class MapSettings:
    extent: MapExtent | None = None
    units: Literal["FEET", "METERS", "DEGREES", "NONE"] | None = None
    units_precedence: Literal['MAP', 'BACKDROP'] = 'MAP'

    def validate_local(self):
        if self.extent is not None:
            a, b = self.extent.lower_left, self.extent.upper_right
            if a.x >= b.x or a.y >= b.y:
                yield Diagnostic(code='map.extent', field='extent',
                    message='Map extent requires lower-left coordinates below upper-right coordinates')


@dataclass(frozen=True, kw_only=True)
class Backdrop:
    file: FileReference | None = None
    extent: MapExtent | None = None
    clear_file: bool = False
    units: Literal['FEET', 'METERS', 'DEGREES', 'NONE'] | None = None
    legacy_offset: Point | None = None
    legacy_scaling: Point | None = None

    def validate_local(self):
        if self.file is not None and self.file.direction != "input":
            yield Diagnostic(code="map.backdrop_direction", message="Backdrop image is an input resource", field="file")
        if self.file is not None and self.clear_file:
            yield Diagnostic(code='map.backdrop_file_conflict', field='clear_file',
                message='A backdrop cannot both name a file and explicitly clear it')


@dataclass(frozen=True, kw_only=True)
class EffectiveMapSettings:
    """Resolved explicit declarations; no ambient GUI preferences or auto bounds."""
    extent: MapExtent | None
    units: Literal['FEET', 'METERS', 'DEGREES', 'NONE'] | None
    units_source: Literal['MAP', 'BACKDROP'] | None


def resolve_map(settings: MapSettings, backdrop: Backdrop) -> EffectiveMapSettings:
    source = None
    if settings.units is not None:
        source = 'MAP'
    if backdrop.units is not None and (source is None or settings.units_precedence == 'BACKDROP'):
        source = 'BACKDROP'
    units = settings.units if source == 'MAP' else backdrop.units if source == 'BACKDROP' else None
    return EffectiveMapSettings(extent=settings.extent, units=units, units_source=source)


@dataclass(frozen=True, kw_only=True)
class MapLabel:
    position: Point
    text: str
    anchor: Ref | None = reference('swmm:nodes', None)
    # GUI 5.2.4 defaults; source omission remains in InpDocument.
    font_name: str = 'Arial'
    font_size: int = 10
    bold: bool = False
    italic: bool = False

    def validate_local(self):
        for name in ('text', 'font_name'):
            if any(c in getattr(self, name) for c in '\r\n\x00;"'):
                yield Diagnostic(code='label.text', field=name,
                    message='Label/font text cannot contain quotes, semicolons, NUL or newlines')
        if not -2147483648 <= self.font_size <= 2147483647:
            yield Diagnostic(code='label.font_size', field='font_size',
                message='GUI font size requires a signed 32-bit integer')
        if self.anchor is not None:
            try:
                validate_identifier(self.anchor.key)
            except ValueError as error:
                yield Diagnostic(code='label.anchor', field='anchor', message=str(error))


@dataclass(frozen=True, kw_only=True)
class MapLabels:
    # Labels have no names/IDs. The layer owns positions, not persistent item IDs.
    entries: tuple[MapLabel, ...] = ()


@dataclass(frozen=True, kw_only=True)
class ProfilePlot:
    name: str
    links: tuple[Ref, ...]

    def validate_local(self):
        if not self.name.strip() or any(c in self.name for c in '\r\n\x00;"'):
            yield Diagnostic(code='profile.name', field='name',
                message='Profile name must have non-whitespace text without quotes, semicolons, NUL or newlines')
        if not self.links:
            yield Diagnostic(code='profile.empty', field='links',
                message='A profile requires at least one link; remove the profile to omit it')
        for index, target in enumerate(self.links):
            if target.collection != 'swmm:links':
                yield Diagnostic(code='profile.link', field=f'links[{index}]',
                    message='Profile entries must reference links')
            else:
                try:
                    validate_identifier(target.key)
                except ValueError as error:
                    yield Diagnostic(code='profile.link', field=f'links[{index}]', message=str(error))


LABELS_COLLECTION = CollectionSpec(key='swmm:labels', record_type=MapLabels,
    key_of=lambda _: 'layer', validate=validate_fields)
PROFILES_COLLECTION = CollectionSpec(key='swmm:profiles', record_type=ProfilePlot,
    key_of=lambda row: row.name, identity_field='name', validate=validate_fields)


TAG_TARGETS = ('swmm:raingages', 'swmm:subcatchments', 'swmm:nodes', 'swmm:links')


@dataclass(frozen=True, kw_only=True)
class ObjectTag:
    """One effective GUI tag for an object; the referenced object owns its identity."""
    target: Ref
    text: str

    def validate_local(self):
        if self.target.collection not in TAG_TARGETS or not isinstance(self.target.key, str):
            yield Diagnostic(code='tag.target', field='target',
                message='Tags reference one rain gage, subcatchment, node or link')
        else:
            try:
                validate_identifier(self.target.key)
            except ValueError as error:
                yield Diagnostic(code='tag.target', field='target', message=str(error))
        if any(c in self.text for c in '\r\n\x00;"'):
            yield Diagnostic(code='tag.text', field='text',
                message='Tag text cannot contain quotes, semicolons, NUL or newlines')


def tag_key(row):
    if not isinstance(row.target, Ref) or not isinstance(row.target.key, str):
        raise ValueError('Tag identity requires an object reference with a scalar ID')
    return row.target.collection, row.target.key


TAGS_COLLECTION = CollectionSpec(key='swmm:tags', record_type=ObjectTag,
    key_of=tag_key, validate=validate_fields)
TITLE_COLLECTION = CollectionSpec(key="swmm:title", record_type=ProjectTitle, key_of=lambda _: "text", validate=validate_fields)
METADATA_COLLECTION = CollectionSpec(key="easysewer:metadata", record_type=ProjectAnnotation, key_of=lambda r: r.key,
                                     identity_field="key", validate=validate_fields)
MAP_COLLECTION = CollectionSpec(key="swmm:map", record_type=MapSettings, key_of=lambda _: "settings", validate=validate_fields)
BACKDROP_COLLECTION = CollectionSpec(key="swmm:backdrop", record_type=Backdrop, key_of=lambda _: "image", validate=validate_fields)
