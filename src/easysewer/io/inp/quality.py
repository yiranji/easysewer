"""Pollutant and land-use domain codecs, including multi-pair relation rows."""

from collections import OrderedDict

from ...model import quality as q
from ...model.fields import validate_fields
from ...model.identity import Ref, canonical_key
from ...schema import DecodedFeature, FeatureDescriptor
from ...schema.structured import EncodedRow, FeatureData, RecordEntry, SourceBinding
from ...validation import Diagnostic, Severity, SourceSpan, ValidationReport
from .formatting import optional_tail
from .geometry import finite_number, number_text
from .field_sources import FieldSources
from .inflow_sources import inflow_sources
from ...schema.inflow_fields import POLLUTANT_FIELD_RULES
from .landuse_sources import landuse_sources
from ...schema.landuse_fields import LANDUSE_FIELD_RULES
from ...validation._cooperative import checkpointed


_SECTIONS = {'POLLUTANTS':'pollutants', 'LANDUSES':'landuses', 'COVERAGES':'coverages',
             'LOADINGS':'loadings', 'BUILDUP':'buildup', 'WASHOFF':'washoff'}


def _ref(collection, key):
    return Ref(collection='swmm:'+collection, key=key)


def _key(section, row):
    key = row.id if isinstance(row, (q.Pollutant,q.LandUse)) else q.relation_key(row)
    return section, *(canonical_key((key,)) if isinstance(key,str) else canonical_key(key))


def _word(value, choices):
    found = next((choice for choice in choices if value.upper().startswith(choice)), None)
    if found is None:
        raise ValueError(f'Unknown keyword {value!r}; expected {choices}')
    return found


def _n(value):
    return number_text(value) if value is not None else None


class QualityCodec:
    descriptor = FeatureDescriptor(key='swmm:quality', sections=frozenset(_SECTIONS), atomic_write=True)
    collections = q.QUALITY_COLLECTIONS
    unit_transforms = q.QUALITY_TRANSFORMS
    pollutant_unit_transforms = q.QUALITY_POLLUTANT_TRANSFORMS
    mutation_guards = (q.guard_pollutant_units,)
    field_rules = POLLUTANT_FIELD_RULES + LANDUSE_FIELD_RULES

    def decode(self, document, profile):
        records, bindings, issues = [], [], []
        source_fields = FieldSources()
        lexical = {d.span.line for d in checkpointed(document.report.errors) if d.span}
        def issue(line, message, severity=Severity.ERROR, code='quality.invalid_input'):
            issues.append(Diagnostic(code=code, message=message, severity=severity, section=line.section,
                object_id=line.values[0] if line.values else None,
                span=SourceSpan(source=document.source,line=line.number,column=1,end_column=len(line.content)+1)))
        def word(line, value, choices):
            result = _word(value, choices)
            if value.upper() != result:
                issue(line,'Native keyword prefix is canonicalized on normalization',Severity.INFO,'quality.native_keyword')
            return result
        def extra(line, count):
            if len(line.values)>count:
                issue(line,'Native ignores trailing columns; normalization removes them',Severity.WARNING,'quality.ignored_columns')
        for section in checkpointed(_SECTIONS):
            groups = OrderedDict()
            for line in checkpointed(document.records(section)):
                size = 2 if section in ('BUILDUP','WASHOFF') else 1
                if len(line.values)<size or section in ('BUILDUP','WASHOFF') and len(line.values)<3:
                    issue(line,'Incomplete relation has no native assignment',Severity.WARNING,'quality.no_assignment')
                    continue
                groups.setdefault(canonical_key(line.values[:size]),[]).append(line)
            for lines in checkpointed(groups.values()):
                if any(line.number in lexical for line in checkpointed(lines)):
                    continue
                values_by_key, group_bindings = OrderedDict(), []
                try:
                    if section in ('POLLUTANTS','LANDUSES') and len(lines)>1:
                        line=lines[1]
                        raise ValueError('Repeated named object definitions are not valid native input')
                    for line in checkpointed(lines):
                        values = line.values; rows = []
                        if section == 'POLLUTANTS':
                            if len(values)<6: raise ValueError('POLLUTANTS needs name, units, three concentrations and decay')
                            co = _ref('pollutants',values[7]) if len(values)>=9 and values[7]!='*' else None
                            rows.append(q.Pollutant(id=values[0],units=word(line,values[1],('MG/L','UG/L','#/L')),
                                rainfall_concentration=finite_number(values[2]),groundwater_concentration=finite_number(values[3]),
                                rdii_concentration=finite_number(values[4]),decay_rate=finite_number(values[5]),
                                snow_only=word(line,values[6],('NO','YES'))=='YES' if len(values)>6 else None,
                                co_pollutant=co,co_fraction=finite_number(values[8]) if co else None,
                                dwf_concentration=finite_number(values[9]) if len(values)>9 else None,
                                initial_concentration=finite_number(values[10]) if len(values)>10 else None))
                            if len(values)==8 or len(values)>=9 and values[7]=='*' and values[8] not in ('0','0.0'):
                                issue(line,'Native ignores an unpaired co-pollutant name or a fraction without a co-pollutant',Severity.WARNING,'quality.ignored_co_pollutant')
                            extra(line,11)
                        elif section == 'LANDUSES':
                            if 1<len(values)<4: raise ValueError('Street sweeping needs all three parameters or none')
                            rows.append(q.LandUse(id=values[0], **(dict(zip(('sweep_interval','sweep_availability','days_since_sweeping'),map(finite_number,values[1:4]))) if len(values)>1 else {})))
                            extra(line,4)
                        elif section in ('COVERAGES','LOADINGS'):
                            if len(values)<3 or len(values)%2!=1: raise ValueError('Expected complete name/value pairs after the subcatchment')
                            for index in checkpointed(range(1,len(values),2)):
                                args=dict(subcatchment=_ref('subcatchments',values[0]))
                                if section=='COVERAGES': rows.append(q.Coverage(**args,landuse=_ref('landuses',values[index]),percent=finite_number(values[index+1])))
                                else: rows.append(q.InitialLoading(**args,pollutant=_ref('pollutants',values[index]),mass_per_area=finite_number(values[index+1])))
                        elif section == 'BUILDUP':
                            kind=word(line,values[2],('NONE','POW','EXP','SAT','EXT')); normalizer='AREA'
                            if kind=='NONE': function=q.NoBuildup(); extra(line,3)
                            else:
                                if len(values)<7: raise ValueError('Active BUILDUP requires seven columns')
                                normalizer=word(line,values[6],('AREA','CURBLENGTH'))
                                a,b=map(finite_number,values[3:5])
                                if kind=='EXT': function=q.ExternalBuildup(maximum=a,scale_factor=b,series=_ref('timeseries',values[5]))
                                else:
                                    c=finite_number(values[5])
                                    if kind=='POW': function=q.PowerBuildup(maximum=a,coefficient=b,exponent=c)
                                    elif kind=='EXP': function=q.ExponentialBuildup(maximum=a,rate=b,unused_parameter=c)
                                    else: function=q.SaturationBuildup(maximum=a,half_saturation_days=c,unused_parameter=b)
                                extra(line,7)
                            rows.append(q.Buildup(landuse=_ref('landuses',values[0]),pollutant=_ref('pollutants',values[1]),function=function,normalizer=normalizer))
                        else:
                            kind=word(line,values[2],('NONE','EXP','RC','EMC')); tail={}
                            if kind=='NONE': function=q.NoWashoff(); extra(line,3)
                            else:
                                if len(values)<5: raise ValueError('Active WASHOFF requires at least five columns')
                                a,b=map(finite_number,values[3:5])
                                function=(q.ExponentialWashoff(coefficient=a,exponent=b) if kind=='EXP' else
                                    q.RatingWashoff(coefficient=a,exponent=b) if kind=='RC' else q.EventMeanConcentration(concentration=a,unused_exponent=b))
                                tail=dict(zip(('sweeping_removal','bmp_removal'),map(finite_number,values[5:7])))
                                extra(line,7)
                            rows.append(q.Washoff(landuse=_ref('landuses',values[0]),pollutant=_ref('pollutants',values[1]),function=function,**tail))
                        for row in checkpointed(rows):
                            ValidationReport(diagnostics=tuple(validate_fields(row))).raise_for_errors()
                            key=_key(section,row)
                            if key in values_by_key:
                                issue(line,'The final assignment for this relation takes effect',Severity.INFO,'quality.repeated_assignment')
                            values_by_key[key]=row
                        group_bindings.append(SourceBinding(line=line.number,key=_key(section,rows[0])))
                    records.extend(RecordEntry(collection='swmm:'+_SECTIONS[section],value=row) for row in checkpointed(values_by_key.values()))
                    if section == 'POLLUTANTS':
                        for row in checkpointed(values_by_key.values()):
                            inflow_sources(source_fields, _ref('pollutants', row.id), row, lines)
                    else:
                        landuse_sources(source_fields, section, values_by_key.values(), lines)
                    bindings.extend(group_bindings)
                except (ValueError,TypeError,OverflowError) as error:
                    issue(line,str(error))
        originals = {Ref(collection=r.collection, key=r.value.id if type(r.value) in (q.Pollutant, q.LandUse)
                         else q.relation_key(r.value)).canonical: r.value for r in checkpointed(records)}
        return DecodedFeature(value=FeatureData(records=tuple(records),bindings=tuple(bindings), **source_fields.finish(originals)),claimed_lines=frozenset(b.line for b in checkpointed(bindings)),report=ValidationReport(diagnostics=tuple(issues)))

    def encode(self, store, profile):
        for section, namespace in checkpointed(_SECTIONS.items()):
            for key,row in checkpointed(store.collection('swmm:'+namespace).items()):
                if section=='POLLUTANTS':
                    tail=optional_tail((None if row.snow_only is None else 'YES' if row.snow_only else 'NO',
                        row.co_pollutant.key if row.co_pollutant else None,_n(row.co_fraction),_n(row.dwf_concentration),_n(row.initial_concentration)),('NO','*','0','0','0'))
                    if row.co_pollutant is not None and len(tail)==2: tail=(*tail,'0')
                    values=(row.id,row.units,*map(number_text,(row.rainfall_concentration,row.groundwater_concentration,row.rdii_concentration,row.decay_rate)),*tail)
                elif section=='LANDUSES':
                    tail=(row.sweep_interval,row.sweep_availability,row.days_since_sweeping)
                    values=(row.id,*(number_text(v or 0) for v in checkpointed(tail))) if any(v is not None for v in checkpointed(tail)) else (row.id,)
                elif section=='COVERAGES': values=(row.subcatchment.key,row.landuse.key,number_text(row.percent))
                elif section=='LOADINGS': values=(row.subcatchment.key,row.pollutant.key,number_text(row.mass_per_area))
                elif section=='BUILDUP':
                    f=row.function
                    if type(f) is q.NoBuildup: tail=('NONE',)
                    else:
                        if type(f) is q.PowerBuildup: tail=('POW',_n(f.maximum),_n(f.coefficient),_n(f.exponent))
                        elif type(f) is q.ExponentialBuildup: tail=('EXP',_n(f.maximum),_n(f.rate),_n(f.unused_parameter))
                        elif type(f) is q.SaturationBuildup: tail=('SAT',_n(f.maximum),_n(f.unused_parameter),_n(f.half_saturation_days))
                        elif type(f) is q.ExternalBuildup: tail=('EXT',_n(f.maximum),_n(f.scale_factor),f.series.key)
                        else: raise TypeError(f'No BUILDUP writer for {type(f).__name__}')
                        tail=(*tail,row.normalizer)
                    values=(row.landuse.key,row.pollutant.key,*tail)
                else:
                    f=row.function
                    if type(f) is q.NoWashoff: tail=('NONE',)
                    else:
                        if type(f) is q.ExponentialWashoff: tail=('EXP',_n(f.coefficient),_n(f.exponent))
                        elif type(f) is q.RatingWashoff: tail=('RC',_n(f.coefficient),_n(f.exponent))
                        elif type(f) is q.EventMeanConcentration: tail=('EMC',_n(f.concentration),_n(f.unused_exponent))
                        else: raise TypeError(f'No WASHOFF writer for {type(f).__name__}')
                        tail=(*tail,*optional_tail((_n(row.sweeping_removal),_n(row.bmp_removal)),('0','0')))
                    values=(row.landuse.key,row.pollutant.key,*tail)
                yield EncodedRow(key=_key(section,row),section=section,values=values,owners=(Ref(collection='swmm:'+namespace,key=key),))

    def validate(self, store, profile):
        yield from q.validate_quality(store)

    def resource_uses(self, store, profile):
        yield from q.quality_resource_uses(store)

    def validate_document(self, document, profile):
        names={section:{canonical_key(line.values[0]) for line in document.records(section)} for section in ('POLLUTANTS','LANDUSES','SUBCATCHMENTS','TIMESERIES')}
        used_gages={canonical_key(line.values[1]) for line in document.records('SUBCATCHMENTS') if len(line.values)>=2}
        used_gages.update(canonical_key(line.values[1]) for line in document.records('HYDROGRAPHS') if len(line.values)==2)
        rain_series={canonical_key(line.values[5]) for line in document.records('RAINGAGES')
            if len(line.values)>=6 and line.values[4].upper()=='TIMESERIES' and canonical_key(line.values[0]) in used_gages}
        for section in ('BUILDUP','WASHOFF','COVERAGES','LOADINGS','POLLUTANTS'):
            for line in document.records(section):
                v=line.values; refs=[]
                if section in ('BUILDUP','WASHOFF') and len(v)>=3:
                    refs=[('LANDUSES',v[0]),('POLLUTANTS',v[1])]
                    if section=='BUILDUP' and len(v)>=7 and v[2].upper().startswith('EXT'):
                        refs.append(('TIMESERIES',v[5]))
                        if canonical_key(v[5]) in rain_series:
                            yield Diagnostic(code='quality.native_rainfall_series',section=section,object_id=v[0],
                                message='Even an overridden EXT buildup assignment marks an active rain series as non-rainfall; normalize obsolete assignments or use separate data',
                                span=SourceSpan(source=document.source,line=line.number,column=1,end_column=len(line.content)+1))
                elif section in ('COVERAGES','LOADINGS') and len(v)>=3:
                    refs=[('SUBCATCHMENTS',v[0]),*((('LANDUSES' if section=='COVERAGES' else 'POLLUTANTS'),name) for name in v[1::2])]
                elif section=='POLLUTANTS' and len(v)>=9 and v[7]!='*': refs=[('POLLUTANTS',v[7])]
                for target,key in refs:
                    if canonical_key(key) not in names[target]:
                        yield Diagnostic(code='quality.source_reference',section=section,object_id=v[0],
                            message=f'Native parses missing {target} reference {key!r}, including overridden assignments',
                            span=SourceSpan(source=document.source,line=line.number,column=1,end_column=len(line.content)+1))
