"""AQUIFERS, local GROUNDWATER assignments and shared-arithmetic GWF codec."""

from collections import OrderedDict
import re

from ...model import groundwater as g
from ...model.fields import validate_fields
from ...model.identity import Ref, canonical_key
from ...schema import DecodedFeature, FeatureDescriptor
from ...schema.structured import EncodedRow, FeatureData, RecordEntry, SourceBinding
from ...validation import Diagnostic, Severity, SourceSpan, ValidationReport
from .expressions import ExpressionCodec
from .formatting import optional_tail
from .geometry import finite_number, number_text
from .field_sources import FieldSources
from .groundwater_sources import groundwater_sources
from ...schema.groundwater_fields import GROUNDWATER_FIELD_RULES
from ...validation._cooperative import checkpointed

AQUIFER_FIELDS = ('porosity','wilting_point','field_capacity','conductivity','conductivity_slope',
    'tension_slope','upper_evaporation_fraction','lower_evaporation_depth','deep_seepage',
    'bottom_elevation','water_table_elevation','upper_moisture')
GROUNDWATER_FIELDS = ('surface_elevation','groundwater_coefficient','groundwater_exponent',
    'surface_water_coefficient','surface_water_exponent','interaction_coefficient','fixed_surface_depth')
OPTIONAL_FIELDS = ('threshold_elevation','bottom_elevation','water_table_elevation','upper_moisture')


def _ref(namespace,key):
    return Ref(collection='swmm:'+namespace,key=key)


def _kind(token):
    return 'LATERAL' if token.upper().startswith('LAT') else 'DEEP' if token.upper().startswith('DEEP') else None


class GroundwaterExpressionCodec(ExpressionCodec):
    def variable(self,resolved):
        return g.GroundwaterVariable(name=resolved)

    def format_variable(self,node):
        if type(node) is g.GroundwaterVariable:
            return node.name
        raise TypeError(f'No groundwater arithmetic writer for {type(node).__name__}')

    def resolve_variable(self,token):
        # Native findmatch uses the first full keyword prefix, including
        # KS before K. Names such as HgwSuffix are accepted by the solver.
        found=next((name for name in g.VARIABLE_DIMENSIONS if token.upper().startswith(name)),None)
        if found is None:
            raise ValueError(f'Unknown groundwater variable {token!r}')
        return found

    def parse(self,text,resolve=None):
        return super().parse(text,resolve or self.resolve_variable)


class GroundwaterCodec:
    descriptor = FeatureDescriptor(key='swmm:groundwater',sections=frozenset({'AQUIFERS','GROUNDWATER','GWF'}),
        requires=('swmm:hydrology','swmm:network'))
    collections = g.GROUNDWATER_COLLECTIONS
    unit_transforms = g.GROUNDWATER_TRANSFORMS
    field_rules = GROUNDWATER_FIELD_RULES

    def __init__(self):
        self.expressions=GroundwaterExpressionCodec()

    def decode(self,document,profile):
        records,bindings,issues=[],[],[]
        source_fields=FieldSources()
        lexical={d.span.line for d in checkpointed(document.report.errors) if d.span}
        from .options import OptionsCodec
        from ...model.units import UnitContext
        options=OptionsCodec().decode(document,profile).value.records
        units=(options[0].value.flow_units if options else None) or profile.option_default('flow_units')
        native_length=UnitContext().convert(1.,dimension='length',to=UnitContext(flow_units=units),rules=profile.unit_rules)
        def issue(line,message,severity=Severity.ERROR,code='groundwater.invalid_input'):
            issues.append(Diagnostic(code=code,message=message,severity=severity,section=line.section,
                object_id=line.values[0] if line.values else None,
                span=SourceSpan(source=document.source,line=line.number,column=1,end_column=len(line.content)+1)))
        for section,namespace in checkpointed((('AQUIFERS','aquifers'),('GROUNDWATER','groundwater'),('GWF','gwf'))):
            groups=OrderedDict()
            for line in checkpointed(document.records(section)):
                v=line.values
                if section=='GWF' and (len(v)<3 or _kind(v[1]) is None):
                    issue(line,'GWF requires subcatchment, LATERAL/DEEP and arithmetic expression')
                    continue
                key=(section,canonical_key(v[0]),_kind(v[1])) if section=='GWF' else (section,canonical_key(v[0]))
                groups.setdefault(key,[]).append(line)
            for key,lines in checkpointed(groups.items()):
                if any(line.number in lexical for line in checkpointed(lines)):
                    continue
                try:
                    entries=[]
                    if section=='AQUIFERS' and len(lines)>1:
                        line=lines[1]
                        raise ValueError('Repeated named aquifers are not valid native input')
                    for line in checkpointed(lines):
                        v=line.values
                        spans=None
                        if section=='AQUIFERS':
                            if len(v)<13: raise ValueError('AQUIFERS requires a name and twelve numeric parameters')
                            row=g.Aquifer(id=v[0],**dict(zip(AQUIFER_FIELDS,map(finite_number,v[1:13]))),
                                evaporation_pattern=_ref('patterns',v[13]) if len(v)>13 else None)
                            count=14
                        elif section=='GROUNDWATER':
                            if len(v)<11: raise ValueError('Fixed native GROUNDWATER needs at least eleven fields, including the threshold value or *')
                            optional={}
                            for name,value in checkpointed(zip(OPTIONAL_FIELDS,v[10:14])):
                                numeric=finite_number(value) if not value.startswith('*') else None
                                divisor=1. if name=='upper_moisture' else native_length
                                if numeric is not None and numeric/divisor==-1.e10:
                                    numeric=None
                                    issue(line,'Native numeric MISSING sentinel is represented as absent and normalizes to *',Severity.INFO,'groundwater.native_missing')
                                optional[name]=numeric
                            if any(value.startswith('*') and value!='*' for value in checkpointed(v[10:14])):
                                issue(line,'Native treats any optional token beginning with * as missing',Severity.INFO,'groundwater.native_placeholder')
                            row=g.Groundwater(subcatchment=_ref('subcatchments',v[0]),aquifer=_ref('aquifers',v[1]),node=_ref('nodes',v[2]),
                                **dict(zip(GROUNDWATER_FIELDS,map(finite_number,v[3:10]))),**optional)
                            count=14
                        else:
                            def variable(token):
                                name=self.expressions.resolve_variable(token)
                                if token.upper()!=name:
                                    issue(line,f'Native variable prefix {token!r} resolves to {name}',Severity.INFO,'groundwater.native_variable')
                                return name
                            expression,spans=self.expressions._parse_with_spans(' '.join(v[2:]),variable)
                            row=g.GroundwaterExpression(subcatchment=_ref('subcatchments',v[0]),kind=_kind(v[1]),expression=expression)
                            count=len(v)
                            if v[1].upper()!=row.kind:
                                issue(line,'Native kind prefix is canonicalized on normalization',Severity.INFO,'groundwater.native_keyword')
                        if len(v)>count:
                            issue(line,'Native ignores trailing columns; normalization removes them',Severity.WARNING,'groundwater.ignored_columns')
                        ValidationReport(diagnostics=tuple(validate_fields(row))).raise_for_errors()
                        entries.append((line,row,spans))
                    if len(lines)>1:
                        issue(line,'The final assignment takes effect',Severity.INFO,'groundwater.repeated_assignment')
                    records.append(RecordEntry(collection='swmm:'+namespace,value=row))
                    bindings.extend(SourceBinding(line=line.number,key=key) for line in checkpointed(lines))
                    owner=_ref(namespace,(row.subcatchment.key,row.kind) if section=='GWF' else row.id if section=='AQUIFERS' else row.subcatchment.key)
                    groundwater_sources(source_fields,owner,row,entries,section,AQUIFER_FIELDS,GROUNDWATER_FIELDS,OPTIONAL_FIELDS)
                except (ValueError,TypeError,OverflowError) as error:
                    issue(line,str(error))
        originals={_ref('aquifers',r.value.id).canonical if r.collection=='swmm:aquifers' else
                   _ref('groundwater',r.value.subcatchment.key).canonical if r.collection=='swmm:groundwater' else
                   _ref('gwf',(r.value.subcatchment.key,r.value.kind)).canonical:r.value for r in checkpointed(records)}
        return DecodedFeature(value=FeatureData(records=tuple(records),bindings=tuple(bindings),**source_fields.finish(originals)),
            claimed_lines=frozenset(b.line for b in checkpointed(bindings)),report=ValidationReport(diagnostics=tuple(issues)))

    def encode(self,store,profile):
        for id,row in checkpointed(store.collection('swmm:aquifers').items()):
            values=(id,*(number_text(getattr(row,name)) for name in checkpointed(AQUIFER_FIELDS)))
            if row.evaporation_pattern is not None: values=(*values,row.evaporation_pattern.key)
            yield EncodedRow(key=('AQUIFERS',canonical_key(id)),section='AQUIFERS',values=values,owners=(_ref('aquifers',id),))
        for id,row in checkpointed(store.collection('swmm:groundwater').items()):
            tail=optional_tail(tuple(number_text(getattr(row,name)) if getattr(row,name) is not None else None for name in checkpointed(OPTIONAL_FIELDS)),('*',)*4)
            values=(row.subcatchment.key,row.aquifer.key,row.node.key,*(number_text(getattr(row,name)) for name in checkpointed(GROUNDWATER_FIELDS)),*(tail or ('*',)))
            yield EncodedRow(key=('GROUNDWATER',canonical_key(id)),section='GROUNDWATER',values=values,owners=(_ref('groundwater',id),))
        for key,row in checkpointed(store.collection('swmm:gwf').items()):
            yield EncodedRow(key=('GWF',canonical_key(key[0]),row.kind),section='GWF',
                values=(row.subcatchment.key,row.kind,self.expressions.format(row.expression)),owners=(_ref('gwf',key),))

    def validate(self,store,profile):
        yield from g.validate_groundwater(store,profile)

    def resource_uses(self,store,profile):
        yield from g.groundwater_resource_uses(store)

    def validate_document(self,document,profile):
        names={section:{canonical_key(line.values[0]) for line in document.records(section)}
            for section in ('AQUIFERS','SUBCATCHMENTS','JUNCTIONS','OUTFALLS','STORAGE','DIVIDERS')}
        nodes=set().union(*(names[key] for key in ('JUNCTIONS','OUTFALLS','STORAGE','DIVIDERS')))
        for line in document.records('GROUNDWATER'):
            if len(line.values)<3: continue
            for section,value,ids in (('AQUIFERS',line.values[1],names['AQUIFERS']),('nodes',line.values[2],nodes)):
                if canonical_key(value) not in ids:
                    yield Diagnostic(code='groundwater.source_reference',section='GROUNDWATER',object_id=line.values[0],
                        message=f'Native parses earlier overwritten {section} reference {value!r}; normalize obsolete assignments explicitly',
                        span=SourceSpan(source=document.source,line=line.number,column=1,end_column=len(line.content)+1))
        for line in document.records('GWF'):
            span=SourceSpan(source=document.source,line=line.number,column=1,end_column=len(line.content)+1)
            if len(line.content.encode(document.encoding))>=1023:
                yield Diagnostic(code='groundwater.native_line_capacity',section='GWF',span=span,
                    message='GWF line exceeds the fixed engine input buffer; keep the full expression within one native input line')
            # Native math lexer copies individual symbols/numbers into char[255].
            expression=' '.join(line.values[2:])
            tokens=re.finditer(r'[A-Za-z_][A-Za-z_0-9]*|(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?',expression)
            if any(len(token[0])>=255 for token in tokens):
                yield Diagnostic(code='groundwater.native_token_capacity',section='GWF',span=span,
                    message='Arithmetic token exceeds the native 254-character payload capacity; explicitly normalize or edit it before running')
