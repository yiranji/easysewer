"""Stateful LID_CONTROLS blocks and ordered LID_USAGE rows."""

from collections import OrderedDict
import re

from ...model import lid as l
from ...model.fields import validate_fields
from ...model.file_resources import FileUse
from ...model.identity import Ref, canonical_key
from ...model.values import FileReference
from ...schema import DecodedFeature, FeatureDescriptor
from ...schema.structured import EncodedRow, FeatureData, RecordEntry, SourceBinding
from ...validation import Diagnostic, Severity, SourceSpan, ValidationReport
from .formatting import optional_tail
from .geometry import finite_number, number_text
from .field_sources import FieldSources
from .lid_sources import control_sources, usage_sources
from ...schema.lid_fields import LID_FIELD_RULES
from ...validation._cooperative import checkpointed

FIELDS = {
    'SURFACE': ('storage_depth','vegetation_fraction','roughness','slope','side_slope'),
    'PAVEMENT': ('thickness','void_ratio','impervious_fraction','permeability','clogging_factor'),
    'SOIL': ('thickness','porosity','field_capacity','wilting_point','conductivity','conductivity_slope','suction'),
    'STORAGE': ('thickness','void_ratio','seepage_rate','clogging_factor'),
    'DRAIN': ('coefficient','exponent','offset','delay'),
    'DRAINMAT': ('thickness','void_fraction','roughness'),
}
OPTIONAL = {'PAVEMENT': ('regeneration_days','regeneration_fraction'), 'DRAIN': ('open_head','close_head')}


def _ref(namespace, key):
    return Ref(collection='swmm:'+namespace, key=key)


def _keyword(token):
    return next((word for word in (*l.LID_KINDS, 'SURFACE','SOIL','STORAGE','PAVEMENT','DRAINMAT','DRAIN','REMOVALS')
        if token.upper().startswith(word)), None)


class LidCodec:
    descriptor = FeatureDescriptor(key='swmm:lid', sections=frozenset({'LID_CONTROLS','LID_USAGE'}),
        requires=('swmm:hydrology','swmm:network','swmm:quality','swmm:resources'), atomic_write=True,
        ordered_sections=frozenset({'LID_USAGE'}))
    collections = l.LID_COLLECTIONS
    unit_transforms = l.LID_TRANSFORMS
    pollutant_unit_transforms = l.LID_POLLUTANT_TRANSFORMS
    field_rules = LID_FIELD_RULES

    def decode(self, document, profile):
        groups, records, bindings, issues = OrderedDict(), [], [], []
        source_fields = FieldSources()
        lexical = {d.span.line for d in checkpointed(document.report.errors) if d.span}
        subcatchments = {canonical_key(line.values[0]) for line in checkpointed(document.records('SUBCATCHMENTS'))}
        def issue(line, message, code='lid.invalid_input', severity=Severity.ERROR):
            issues.append(Diagnostic(code=code, message=message, severity=severity, section=line.section,
                object_id=line.values[0], span=SourceSpan(source=document.source, line=line.number, column=1, end_column=len(line.content)+1)))
        for line in checkpointed(document.records('LID_CONTROLS')):
            groups.setdefault(canonical_key(line.values[0]), []).append(line)
        for key, lines in checkpointed(groups.items()):
            if any(line.number in lexical for line in checkpointed(lines)):
                continue
            data, removals, seen = {'id': lines[0].values[0]}, OrderedDict(), set()
            try:
                for line in checkpointed(lines):
                    v = line.values
                    if len(v) < 2 or (kind := _keyword(v[1])) is None:
                        raise ValueError('Unknown LID type/layer or incomplete declaration')
                    if v[1].upper() != kind:
                        issue(line, f'Native keyword prefix resolves to {kind}', 'lid.native_keyword', Severity.INFO)
                    if kind in l.LID_KINDS:
                        data['kind'] = kind
                        count = 2
                    elif kind == 'REMOVALS':
                        if len(v) < 4 or len(v) % 2:
                            raise ValueError('REMOVALS requires complete pollutant/percentage pairs')
                        for i in checkpointed(range(2, len(v), 2)):
                            value = l.LidRemoval(pollutant=_ref('pollutants',v[i]), percent=finite_number(v[i+1]))
                            ValidationReport(diagnostics=tuple(validate_fields(value))).raise_for_errors()
                            removals[canonical_key(v[i])] = value
                        count = len(v)
                    else:
                        names = FIELDS[kind]
                        count = 2 + len(names)
                        if len(v) < count:
                            raise ValueError(f'{kind} requires {len(names)} numeric values')
                        # Native checks the token count but skips all numbers
                        # when DRAINMAT precedes GR or belongs to another type.
                        if kind == 'DRAINMAT' and data.get('kind') != 'GR':
                            issue(line, 'Native ignores DRAINMAT unless a preceding type declaration is GR', 'lid.ignored_drain_mat', Severity.WARNING)
                            continue
                        args = dict(zip(names, map(finite_number, v[2:count])))
                        for i, name in checkpointed(enumerate(OPTIONAL.get(kind, ()))):
                            if len(v) > count+i:
                                args[name] = finite_number(v[count+i])
                        count += len(OPTIONAL.get(kind, ()))
                        if kind == 'STORAGE':
                            if len(v) > 6:
                                args['covered'] = v[6].upper().startswith('YES')
                                if v[6].upper() not in ('YES','NO'):
                                    issue(line, 'Native covered is true only for the YES prefix', 'lid.native_covered', Severity.WARNING)
                            count = 7
                        if kind == 'DRAIN':
                            if len(v) > 8:
                                args['curve'] = _ref('curves',v[8])
                            count = 9
                        name, cls = l.LAYERS[kind]
                        value = cls(**args)
                        ValidationReport(diagnostics=tuple(validate_fields(value))).raise_for_errors()
                        data[name] = value
                    if kind in seen and kind != 'REMOVALS':
                        issue(line, 'The final type/layer assignment takes effect', 'lid.repeated_assignment', Severity.INFO)
                    seen.add(kind)
                    if len(v) > count:
                        issue(line, 'Native ignores trailing columns; normalization removes them', 'lid.ignored_columns', Severity.WARNING)
                row = l.LidControl(**data, removals=tuple(removals.values()))
                ValidationReport(diagnostics=tuple(validate_fields(row))).raise_for_errors()
                records.append(RecordEntry(collection='swmm:lid_controls', value=row))
                # All source lines belong to the indivisible layered control.
                # A changed block is rewritten in canonical type-before-layer order.
                bindings.extend(SourceBinding(line=line.number, key=('control',key,'type')) for line in checkpointed(lines))
                control_sources(source_fields, row, lines, _keyword, FIELDS, OPTIONAL)
            except (ValueError, TypeError, OverflowError) as error:
                issue(line, str(error))
        for index, line in checkpointed(enumerate(document.records('LID_USAGE'), 1)):
            if line.number in lexical:
                continue
            try:
                v = line.values
                if len(v) < 8:
                    raise ValueError('LID_USAGE requires eight fields')
                match = re.match(r'^[+-]?\d+', v[2], flags=re.ASCII)
                count = int(match[0]) if match else 0
                if v[2] != str(count):
                    issue(line, f'Native integer prefix resolves replicate count to {count}', 'lid.native_count', Severity.WARNING)
                if not 0 <= count <= 2_147_483_647:
                    raise ValueError('Replicate count is outside the native signed integer range')
                args = dict(record_id=f'lid-usage-{index}', subcatchment=_ref('subcatchments',v[0]), control=_ref('lid_controls',v[1]))
                if count == 0:
                    # addLidUnit is never called; neither output path nor drain
                    # target is read. Do not invent a live file consumer.
                    args['parameters'] = tuple(v[3:])
                    issue(line, 'Zero replicates disables this row; native ignores its remaining fields', 'lid.disabled_usage', Severity.INFO)
                else:
                    args['number'] = count
                    args.update(zip(('area','width','initial_saturation','from_impervious'), map(finite_number, v[3:7])))
                    flag = finite_number(v[7])
                    if flag < 0:
                        raise ValueError('ToPerv must be nonnegative')
                    args['to_pervious'] = flag > 0
                    if flag not in (0,1):
                        issue(line, 'Any positive native ToPerv value means true', 'lid.native_flag', Severity.INFO)
                    if len(v) > 8 and v[8] != '*':
                        args['report_file'] = FileReference(path=v[8], direction='output')
                    if len(v) > 9 and v[9] != '*':
                        args['drain_to'] = _ref('subcatchments' if canonical_key(v[9]) in subcatchments else 'nodes',v[9])
                    if len(v) > 10:
                        args['from_pervious'] = finite_number(v[10])
                if count and len(v) > 11:
                    issue(line, 'Native ignores trailing usage columns', 'lid.ignored_columns', Severity.WARNING)
                row = (l.LidUsage if count else l.DisabledLidUsage)(**args)
                ValidationReport(diagnostics=tuple(validate_fields(row))).raise_for_errors()
                records.append(RecordEntry(collection='swmm:lid_usage',value=row))
                bindings.append(SourceBinding(line=line.number,key=('usage',canonical_key(row.record_id))))
                usage_sources(source_fields, row, line)
            except (ValueError, TypeError, OverflowError) as error:
                issue(line, str(error))
        originals = {_ref('lid_controls', entry.value.id).canonical if entry.collection == 'swmm:lid_controls' else
                     _ref('lid_usage', entry.value.record_id).canonical: entry.value for entry in checkpointed(records)}
        return DecodedFeature(value=FeatureData(records=tuple(records),bindings=tuple(bindings),**source_fields.finish(originals)),
            claimed_lines=frozenset(b.line for b in checkpointed(bindings)), report=ValidationReport(diagnostics=tuple(issues)))

    def encode(self, store, profile):
        for id, row in checkpointed(store.collection('swmm:lid_controls').items()):
            owner = (_ref('lid_controls',id),)
            yield EncodedRow(key=('control',canonical_key(id),'type'),section='LID_CONTROLS',values=(id,row.kind),owners=owner)
            for kind, (name, _) in checkpointed(l.LAYERS.items()):
                layer = getattr(row,name)
                if layer is None:
                    continue
                values = (id,kind,*(number_text(getattr(layer,field)) for field in checkpointed(FIELDS[kind])))
                tail = [number_text(getattr(layer,name)) if getattr(layer,name) is not None else None for name in checkpointed(OPTIONAL.get(kind,()))]
                if kind == 'DRAIN':
                    tail.append(layer.curve.key if layer.curve else None)
                if kind == 'STORAGE':
                    tail = [('YES' if layer.covered else 'NO') if layer.covered is not None else None]
                yield EncodedRow(key=('control',canonical_key(id),kind),section='LID_CONTROLS',values=(*values,*optional_tail(tail)),owners=owner)
            for removal in checkpointed(row.removals):
                yield EncodedRow(key=('control',canonical_key(id),'removal',canonical_key(removal.pollutant.key)),section='LID_CONTROLS',
                    values=(id,'REMOVALS',removal.pollutant.key,number_text(removal.percent)),owners=owner)
        for id, row in checkpointed(store.collection('swmm:lid_usage').items()):
            if type(row) is l.DisabledLidUsage:
                yield EncodedRow(key=('usage',canonical_key(id)),section='LID_USAGE',
                    values=(row.subcatchment.key,row.control.key,'0',*row.parameters),owners=(_ref('lid_usage',id),))
                continue
            values = (row.subcatchment.key,row.control.key,str(row.number),number_text(row.area),number_text(row.width),
                number_text(row.initial_saturation),number_text(row.from_impervious),str(int(row.to_pervious)))
            tail = optional_tail((row.report_file.path if row.report_file else None, row.drain_to.key if row.drain_to else None,
                number_text(row.from_pervious) if row.from_pervious is not None else None), ('*','*','0'))
            yield EncodedRow(key=('usage',canonical_key(id)),section='LID_USAGE',values=(*values,*tail),owners=(_ref('lid_usage',id),))

    def validate(self, store, profile):
        yield from l.validate_lids(store,profile)

    def resource_uses(self, store, profile):
        yield from l.lid_resource_uses(store)

    def validate_run(self, store, profile):
        from ...model.options import get_options
        active = [row for row in store.collection('swmm:lid_usage').values()
            if ValidationReport(diagnostics=tuple(validate_fields(row))).is_valid and row.number > 0]
        if not active:
            return
        used = {row.kind for row in store.collection('swmm:files').values() if row.mode == 'USE'}
        if 'HOTSTART' in used:
            yield Diagnostic(code='lid.hotstart_state', severity=Severity.WARNING,
                message='SWMM 5.2.4 HOTSTART has no LID layer, clogging or drain state; each deployment restarts from InitSat')
        if 'RUNOFF' in used and not get_options(store).ignore_rainfall:
            yield Diagnostic(code='lid.runoff_state', severity=Severity.WARNING,
                message='USE RUNOFF replays aggregate catchment results; LID layer states and detailed performance are not recomputed')
            controls = store.collection('swmm:lid_controls')
            catchments = store.collection('swmm:subcatchments')
            for row in active:
                if not store.contains(row.control) or not store.contains(row.subcatchment):
                    continue
                control, catchment = controls[row.control.key], catchments[row.subcatchment.key]
                if not ValidationReport(diagnostics=tuple(validate_fields(control))+tuple(validate_fields(catchment))).is_valid:
                    continue
                drains = control.kind == 'GR' or control.drain is not None and control.drain.coefficient > 0
                distinct = row.drain_to is not None and row.drain_to.canonical != catchment.outlet.canonical
                quality = bool(store.collection('swmm:pollutants')) and not get_options(store).ignore_quality and any(r.percent for r in control.removals)
                if drains and (distinct or quality):
                    yield Diagnostic(code='lid.runoff_drain_semantics', object_id=row.record_id,
                        message='RUNOFF stores aggregate drain flow without its destination/removal fractions; replay cannot preserve this LID routing or treatment')

    def file_uses(self, store, profile):
        for row in checkpointed(store.collection('swmm:lid_usage').values()):
            if isinstance(row,l.LidUsage) and row.report_file:
                yield FileUse(owner=_ref('lid_usage',row.record_id),path=('report_file',),file=row.report_file,
                    role='swmm:lid-detail',format='swmm:lid-report',base='working_directory',access='write',active=row.number > 0)

    def validate_document(self, document, profile):
        # Obsolete assignments are still parsed by native, and file opening
        # happens during input reading even when rainfall is disabled.
        names = {namespace:{canonical_key(line.values[0]) for section in sections for line in document.records(section)}
            for namespace, sections in (('nodes',('JUNCTIONS','OUTFALLS','DIVIDERS','STORAGE')),('subcatchments',('SUBCATCHMENTS',)),
                ('curves',('CURVES',)),('pollutants',('POLLUTANTS',)))}
        for line in document.records('LID_CONTROLS'):
            v = line.values
            if len(v) < 2:
                continue
            kind = _keyword(v[1])
            refs = [('curves',v[8])] if kind == 'DRAIN' and len(v) > 8 else [('pollutants',name) for name in v[2::2]] if kind == 'REMOVALS' else []
            if any(canonical_key(name) not in names[namespace] for namespace,name in refs):
                yield Diagnostic(code='lid.source_reference',section=line.section,message='An earlier source assignment references a removed resource; normalize before running',
                    span=SourceSpan(source=document.source,line=line.number,column=1,end_column=len(line.content)+1))
