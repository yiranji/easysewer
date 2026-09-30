"""OUT variable keys are independent of native numbers and pollutant ordering."""

from dataclasses import dataclass
from ..model.identity import namespace_key
from ..model.units import UnitContext

COLLECTIONS=('swmm:subcatchments', 'swmm:nodes', 'swmm:links', 'swmm:system')
QUALITY_BASE={'swmm:subcatchments':8, 'swmm:nodes':6, 'swmm:links':5}


@dataclass(frozen=True, kw_only=True)
class OutputVariable:
    collection: str
    key: str
    code: int
    dimension: str | None
    description: str
    pollutant: bool = False
    units: tuple[tuple[str, str], ...] = ()
    semantics: str | None = None

    def __post_init__(self):
        namespace_key(self.key)
        if self.collection not in COLLECTIONS:raise ValueError('Unsupported OUT collection')
        if type(self.code) is not int or self.code<0 or type(self.pollutant) is not bool:
            raise ValueError('Invalid native variable code or pollutant flag')
        if self.pollutant and self.code!=QUALITY_BASE.get(self.collection):
            raise ValueError('Pollutant variables must start at the profile quality offset')
        if type(self.description) is not str or not self.description:raise ValueError('A variable needs a description')
        if self.dimension is not None and type(self.dimension) is not str:raise TypeError('Invalid dimension')
        if type(self.units) is not tuple or any(type(row) is not tuple or len(row)!=2 or row[0] not in ('US','SI') or type(row[1]) is not str or not row[1] for row in self.units):
            raise ValueError('Explicit units must be immutable US/SI pairs')
        if self.units and (len(self.units)!=2 or set(dict(self.units))!={'US','SI'}):raise ValueError('Explicit units must cover US and SI once')
        if self.semantics is not None and (type(self.semantics) is not str or not self.semantics):raise ValueError('Invalid variable semantics')

    def unit(self, flow_units, concentration_unit=None):
        if self.pollutant:return concentration_unit
        units=UnitContext(flow_units=flow_units)
        if self.units:return dict(self.units)[units.system]
        return units.unit(self.dimension) if self.dimension else None


@dataclass(frozen=True, kw_only=True)
class OutputVariables:
    entries: tuple[OutputVariable, ...] = ()

    def __post_init__(self):
        if type(self.entries) is not tuple or any(not isinstance(row, OutputVariable) for row in self.entries):
            raise TypeError('Expected immutable variable definitions')
        if any(row.key.startswith('swmm:unknown-') for row in self.entries):
            raise ValueError('Unknown-code keys are reserved for lossless fallback queries')
        for field in ('key','code'):
            if len({(row.collection,getattr(row,field)) for row in self.entries})!=len(self.entries):
                raise ValueError('Duplicate output variable '+field)

    def with_variables(self, *entries):
        return OutputVariables(entries=self.entries+tuple(entries))

    def available(self, metadata, collection):
        if collection not in COLLECTIONS:raise KeyError(collection)
        by_code={row.code:row for row in self.entries if row.collection==collection and not row.pollutant}
        quality=next((row for row in self.entries if row.collection==collection and row.pollutant),None)
        count=len(metadata.names('swmm:pollutants'))
        result=[];seen=set()
        for code in dict(metadata.variable_codes)[collection]:
            is_quality=quality is not None and quality.code<=code<quality.code+count
            if is_quality and code in by_code:raise ValueError('Extension code overlaps actual pollutant results')
            variable=quality if is_quality else by_code.get(code)
            if variable is None:
                variable=OutputVariable(collection=collection,key='swmm:unknown-'+str(code),code=code,
                    dimension=None,description='Unrecognized variable code '+str(code),semantics='unknown')
            if variable.key not in seen:result.append(variable);seen.add(variable.key)
        return tuple(result)


def swmm_output_variables():
    groups=(
        ('swmm:subcatchments', (
            ('rainfall','rain_intensity','Rainfall intensity'),('snow_depth','rain_depth','Snow water equivalent depth'),
            ('evaporation','evaporation','Evaporation loss rate'),('infiltration','rain_intensity','Infiltration loss rate'),
            ('runoff','flow','Runoff flow'),('groundwater_flow','flow','Groundwater flow to the drainage system'),
            ('groundwater_elevation','elevation','Groundwater table elevation'),('soil_moisture','ratio','Unsaturated-zone moisture fraction'))),
        ('swmm:nodes', (
            ('depth','depth','Water depth above invert'),('head','elevation','Hydraulic head'),
            ('volume','volume','Stored and ponded water volume'),('lateral_inflow','flow','Lateral inflow'),
            ('inflow','flow','Total inflow'),('overflow','flow','Overflow output channel; producer semantics apply'))),
        ('swmm:links', (
            ('flow','flow','Flow rate'),('depth','depth','Average link water depth'),('velocity','velocity','Flow velocity'),
            ('volume','volume','Stored water volume'),('capacity','ratio','Conduit area fraction or pump/regulator setting'))),
        ('swmm:system', (
            ('temperature','temperature','Air temperature'),('rainfall','rain_intensity','Area-weighted rainfall'),
            ('snow_depth','rain_depth','Area-weighted snow water equivalent'),('infiltration','rain_intensity','Area-weighted infiltration'),
            ('runoff','flow','Runoff flow'),('dry_weather_inflow','flow','Dry-weather inflow'),
            ('groundwater_inflow','flow','Groundwater inflow'),('rdii_inflow','flow','Rainfall-dependent inflow/infiltration'),
            ('external_inflow','flow','Direct external inflow'),('inflow','flow','Total lateral inflow'),
            ('flooding','flow','Flooding output channel; producer semantics apply'),('outflow','flow','Outfall outflow'),
            ('storage','volume','Total node and link water storage'),('evaporation','evaporation','Area-weighted actual evaporation'),
            ('potential_evaporation','evaporation','Potential evaporation'))))
    entries=[]
    for collection,rows in groups:
        for code,(key,dimension,description) in enumerate(rows):
            entries.append(OutputVariable(collection=collection,key='swmm:'+key,code=code,dimension=dimension,description=description))
        if collection in QUALITY_BASE:
            entries.append(OutputVariable(collection=collection,key='swmm:concentration',code=QUALITY_BASE[collection],
                dimension='concentration',description='Pollutant concentration',pollutant=True))
    return OutputVariables(entries=tuple(entries))
