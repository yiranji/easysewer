"""Explicit candidate Model JSON 1.0 field contracts.

Field names, variant tags and defaults are versioned here independently of the
Python dataclass layout. New fields require an intentional declaration/migration.
No runtime inspection or module discovery defines the serialized contract.
"""

import datetime
from functools import lru_cache
from ...model.identity import Ref
from ...model import values as v
from ...model import options as o
from ...model import geometry as g
from ...model import network as n
from ...model import resources as r
from ...model import surface as s
from ...model import climate as cl
from ...model import hydrology as h
from ...model import inflows as i
from ...model import controls as c
from ...model import report as rp, project as pr
from ...model.files import InterfaceFile
from ...model.rdii import UnitHydrograph, HydrographResponse, RdiiInflow
from ...model import quality as q
from ...model import groundwater as gw
from ...model import treatment as tr
from ...model import lid as li
from ...model.events import RoutingEvent, EventSchedule
from .types import JsonField, JsonType


def _type(key, cls, fields, *, bases=(), defaults=(), singleton=None, source_defaults=()):
    return JsonType(key=key, value_type=cls, fields=tuple(JsonField(name=name, attribute=name, shape=shape)
                    for name, shape in fields), bases=bases, defaults=defaults, singleton=singleton,
                    source_defaults=source_defaults)


@lru_cache(maxsize=1)
def builtin_types():
    return (
        _type('swmm:map.label', pr.MapLabel, (
            ('position', ('object', 'core:point')), ('text', ('string',)),
            ('anchor', ('union', ('object', 'core:ref'), ('null',))),
            ('font_name', ('string',)), ('font_size', ('integer',)),
            ('bold', ('boolean',)), ('italic', ('boolean',)),
        ), defaults=(('anchor', None), ('font_name', 'Arial'), ('font_size', 10), ('bold', False), ('italic', False))),
        _type('swmm:map.labels', pr.MapLabels, (
            ('entries', ('array', ('object', 'swmm:map.label'))),
        ), defaults=(('entries', ()),)),
        _type('swmm:project.profile', pr.ProfilePlot, (
            ('name', ('string',)), ('links', ('array', ('object', 'core:ref'))),
        )),
        _type('swmm:project.tag', pr.ObjectTag, (
            ('target', ('object', 'core:ref')), ('text', ('string',)),
        )),
        _type('swmm:events.period', RoutingEvent, (
            ('start', ('object', 'core:datetime')), ('end', ('object', 'core:datetime')),
        )),
        _type('swmm:events.schedule', EventSchedule, (
            ('periods', ('array', ('object', 'swmm:events.period'))),
        ), defaults=(('periods', ()),)),
        _type('swmm:lid.surface', li.LidSurface, tuple((name,('number',)) for name in
            ('storage_depth','vegetation_fraction','roughness','slope','side_slope'))),
        _type('swmm:lid.pavement', li.LidPavement, (
            ('thickness',('number',)), ('void_ratio',('number',)), ('impervious_fraction',('number',)),
            ('permeability',('number',)), ('clogging_factor',('number',)),
            ('regeneration_days',('union',('number',),('null',))), ('regeneration_fraction',('union',('number',),('null',))),
        ), defaults=(('regeneration_days',None),('regeneration_fraction',None))),
        _type('swmm:lid.soil', li.LidSoil, tuple((name,('number',)) for name in
            ('thickness','porosity','field_capacity','wilting_point','conductivity','conductivity_slope','suction'))),
        _type('swmm:lid.storage', li.LidStorage, (
            ('thickness',('number',)), ('void_ratio',('number',)), ('seepage_rate',('number',)), ('clogging_factor',('number',)),
            ('covered',('union',('boolean',),('null',))),
        ), defaults=(('covered',None),)),
        _type('swmm:lid.drain', li.LidDrain, (
            ('coefficient',('number',)), ('exponent',('number',)), ('offset',('number',)), ('delay',('number',)),
            ('open_head',('union',('number',),('null',))), ('close_head',('union',('number',),('null',))),
            ('curve',('union',('object','core:ref'),('null',))),
        ), defaults=(('open_head',None),('close_head',None),('curve',None))),
        _type('swmm:lid.drain_mat', li.LidDrainMat, tuple((name,('number',)) for name in ('thickness','void_fraction','roughness'))),
        _type('swmm:lid.removal', li.LidRemoval, (('pollutant',('object','core:ref')),('percent',('number',)))),
        _type('swmm:lid.control', li.LidControl, (
            ('id',('string',)), ('kind',('literal','BC','RG','GR','IT','PP','RB','RD','VS')),
            ('surface',('union',('object','swmm:lid.surface'),('null',))),
            ('pavement',('union',('object','swmm:lid.pavement'),('null',))),
            ('soil',('union',('object','swmm:lid.soil'),('null',))),
            ('storage',('union',('object','swmm:lid.storage'),('null',))),
            ('drain',('union',('object','swmm:lid.drain'),('null',))),
            ('drain_mat',('union',('object','swmm:lid.drain_mat'),('null',))),
            ('removals',('array',('object','swmm:lid.removal'))),
        ), bases=('swmm:network.entity',), defaults=(('surface',None),('pavement',None),('soil',None),('storage',None),('drain',None),('drain_mat',None),('removals',()))),
        _type('swmm:lid.usage', li.LidUsage, (
            ('record_id',('string',)), ('subcatchment',('object','core:ref')), ('control',('object','core:ref')),
            ('number',('integer',)), ('area',('number',)), ('width',('number',)), ('initial_saturation',('number',)),
            ('from_impervious',('number',)), ('to_pervious',('boolean',)),
            ('report_file',('union',('object','core:file'),('null',))), ('drain_to',('union',('object','core:ref'),('null',))),
            ('from_pervious',('union',('number',),('null',))),
        ), defaults=(('report_file',None),('drain_to',None),('from_pervious',None))),
        _type('swmm:lid.disabled_usage', li.DisabledLidUsage, (
            ('record_id',('string',)), ('subcatchment',('object','core:ref')), ('control',('object','core:ref')),
            ('parameters',('array',('string',))),
        )),
        _type('swmm:treatment.expression', tr.Treatment, (
            ('node',('object','core:ref')), ('pollutant',('object','core:ref')),
            ('kind',('literal','C','R')), ('expression',('object','core:expression')),
        )),
        _type('swmm:treatment.process_variable', tr.TreatmentProcessVariable, (
            ('name',('literal','HRT','DT','FLOW','DEPTH','AREA')),
        ), bases=('core:expression',)),
        _type('swmm:treatment.concentration', tr.TreatmentConcentration, (
            ('pollutant',('object','core:ref')),
        ), bases=('core:expression',)),
        _type('swmm:treatment.removal', tr.TreatmentRemoval, (
            ('pollutant',('object','core:ref')),
        ), bases=('core:expression',)),
        _type('swmm:groundwater.aquifer', gw.Aquifer, (
            ('id',('string',)), ('porosity',('number',)), ('wilting_point',('number',)), ('field_capacity',('number',)),
            ('conductivity',('number',)), ('conductivity_slope',('number',)), ('tension_slope',('number',)),
            ('upper_evaporation_fraction',('number',)), ('lower_evaporation_depth',('number',)), ('deep_seepage',('number',)),
            ('bottom_elevation',('number',)), ('water_table_elevation',('number',)), ('upper_moisture',('number',)),
            ('evaporation_pattern',('union',('object','core:ref'),('null',))),
        ), bases=('swmm:network.entity',), defaults=(('evaporation_pattern',None),)),
        _type('swmm:groundwater.binding', gw.Groundwater, (
            ('subcatchment',('object','core:ref')), ('aquifer',('object','core:ref')), ('node',('object','core:ref')),
            ('surface_elevation',('number',)), ('groundwater_coefficient',('number',)), ('groundwater_exponent',('number',)),
            ('surface_water_coefficient',('number',)), ('surface_water_exponent',('number',)), ('interaction_coefficient',('number',)),
            ('fixed_surface_depth',('number',)), ('threshold_elevation',('union',('number',),('null',))),
            ('bottom_elevation',('union',('number',),('null',))), ('water_table_elevation',('union',('number',),('null',))),
            ('upper_moisture',('union',('number',),('null',))),
        ), defaults=(('threshold_elevation',None),('bottom_elevation',None),('water_table_elevation',None),('upper_moisture',None))),
        _type('swmm:groundwater.variable', gw.GroundwaterVariable, (
            ('name',('literal','HGW','HSW','HCB','HGS','KS','K','THETA','PHI','FI','FU','A')),
        ), bases=('core:expression',)),
        _type('swmm:groundwater.expression', gw.GroundwaterExpression, (
            ('subcatchment',('object','core:ref')), ('kind',('literal','LATERAL','DEEP')), ('expression',('object','core:expression')),
        )),
        _type('swmm:quality.pollutant', q.Pollutant, (
            ('id',('string',)), ('units',('literal','MG/L','UG/L','#/L')),
            ('rainfall_concentration',('number',)), ('groundwater_concentration',('number',)),
            ('rdii_concentration',('number',)), ('decay_rate',('number',)),
            ('snow_only',('union',('boolean',),('null',))),
            ('co_pollutant',('union',('object','core:ref'),('null',))),
            ('co_fraction',('union',('number',),('null',))),
            ('dwf_concentration',('union',('number',),('null',))),
            ('initial_concentration',('union',('number',),('null',))),
        ), bases=('swmm:network.entity',), defaults=(('snow_only',None),('co_pollutant',None),('co_fraction',None),('dwf_concentration',None),('initial_concentration',None))),
        _type('swmm:quality.landuse', q.LandUse, (
            ('id',('string',)), ('sweep_interval',('union',('number',),('null',))),
            ('sweep_availability',('union',('number',),('null',))), ('days_since_sweeping',('union',('number',),('null',))),
        ), bases=('swmm:network.entity',), defaults=(('sweep_interval',None),('sweep_availability',None),('days_since_sweeping',None))),
        _type('swmm:quality.coverage', q.Coverage, (
            ('subcatchment',('object','core:ref')), ('landuse',('object','core:ref')), ('percent',('number',)),
        )),
        _type('swmm:quality.initial_loading', q.InitialLoading, (
            ('subcatchment',('object','core:ref')), ('pollutant',('object','core:ref')), ('mass_per_area',('number',)),
        )),
        _type('swmm:quality.no_buildup', q.NoBuildup, (), bases=('swmm:quality.buildup_function',)),
        _type('swmm:quality.power_buildup', q.PowerBuildup, (
            ('maximum',('number',)), ('coefficient',('number',)), ('exponent',('number',)),
        ), bases=('swmm:quality.buildup_function',)),
        _type('swmm:quality.exponential_buildup', q.ExponentialBuildup, (
            ('maximum',('number',)), ('rate',('number',)), ('unused_parameter',('number',)),
        ), bases=('swmm:quality.buildup_function',), defaults=(('unused_parameter',0.),)),
        _type('swmm:quality.saturation_buildup', q.SaturationBuildup, (
            ('maximum',('number',)), ('half_saturation_days',('number',)), ('unused_parameter',('number',)),
        ), bases=('swmm:quality.buildup_function',), defaults=(('unused_parameter',0.),)),
        _type('swmm:quality.external_buildup', q.ExternalBuildup, (
            ('maximum',('number',)), ('scale_factor',('number',)), ('series',('object','core:ref')),
        ), bases=('swmm:quality.buildup_function',)),
        _type('swmm:quality.buildup', q.Buildup, (
            ('landuse',('object','core:ref')), ('pollutant',('object','core:ref')),
            ('function',('object','swmm:quality.buildup_function')), ('normalizer',('literal','AREA','CURBLENGTH')),
        ), defaults=(('normalizer','AREA'),)),
        _type('swmm:quality.no_washoff', q.NoWashoff, (), bases=('swmm:quality.washoff_function',)),
        _type('swmm:quality.exponential_washoff', q.ExponentialWashoff, (
            ('coefficient',('number',)), ('exponent',('number',)),
        ), bases=('swmm:quality.washoff_function',)),
        _type('swmm:quality.rating_washoff', q.RatingWashoff, (
            ('coefficient',('number',)), ('exponent',('number',)),
        ), bases=('swmm:quality.washoff_function',)),
        _type('swmm:quality.event_mean_concentration', q.EventMeanConcentration, (
            ('concentration',('number',)), ('unused_exponent',('number',)),
        ), bases=('swmm:quality.washoff_function',), defaults=(('unused_exponent',0.),)),
        _type('swmm:quality.washoff', q.Washoff, (
            ('landuse',('object','core:ref')), ('pollutant',('object','core:ref')),
            ('function',('object','swmm:quality.washoff_function')),
            ('sweeping_removal',('union',('number',),('null',))), ('bmp_removal',('union',('number',),('null',))),
        ), defaults=(('sweeping_removal',None),('bmp_removal',None))),
        _type('swmm:inflows.concentration', i.ConcentrationInflow, (
            ('node',('object','core:ref')), ('constituent',('object','core:ref')),
            ('series',('union',('object','core:ref'),('null',))), ('scale_factor',('union',('number',),('null',))),
            ('baseline',('union',('number',),('null',))), ('pattern',('union',('object','core:ref'),('null',))),
        ), bases=('swmm:inflows.external_inflow',), defaults=(('series',None),('scale_factor',None),('baseline',None),('pattern',None))),
        _type('swmm:inflows.mass', i.MassInflow, (
            ('node',('object','core:ref')), ('constituent',('object','core:ref')),
            ('series',('union',('object','core:ref'),('null',))), ('scale_factor',('union',('number',),('null',))),
            ('baseline',('union',('number',),('null',))), ('pattern',('union',('object','core:ref'),('null',))),
            ('mass_factor',('union',('number',),('null',))),
        ), bases=('swmm:inflows.external_inflow',), defaults=(('series',None),('scale_factor',None),('baseline',None),('pattern',None),('mass_factor',None))),
        _type('swmm:inflows.dry_weather_concentration', i.DryWeatherConcentration, (
            ('node',('object','core:ref')), ('constituent',('object','core:ref')), ('baseline',('number',)),
            ('patterns',('array',('union',('object','core:ref'),('null',)))),
        ), bases=('swmm:inflows.dry_weather_inflow',), defaults=(('patterns',()),)),
        _type('swmm:rdii.response', HydrographResponse, (
            ('month', ('literal', 'ALL', 'JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC')),
            ('response', ('literal', 'SHORT', 'MEDIUM', 'LONG')), ('fraction', ('number',)),
            ('time_to_peak', ('number',)), ('recession_ratio', ('number',)),
            ('maximum_abstraction', ('union', ('number',), ('null',))),
            ('recovery_rate', ('union', ('number',), ('null',))),
            ('initial_abstraction', ('union', ('number',), ('null',))),
        ), defaults=(('maximum_abstraction', None), ('recovery_rate', None), ('initial_abstraction', None))),
        _type('swmm:rdii.group', UnitHydrograph, (
            ('id', ('string',)), ('rain_gage', ('union', ('object', 'core:ref'), ('null',))),
            ('prior_rain_gages', ('array', ('object', 'core:ref'))),
            ('responses', ('array', ('object', 'swmm:rdii.response'))),
        ), bases=('swmm:network.entity',), defaults=(('rain_gage', None), ('prior_rain_gages', ()), ('responses', ()))),
        _type('swmm:rdii.inflow', RdiiInflow, (
            ('node', ('object', 'core:ref')), ('hydrograph', ('object', 'core:ref')), ('sewer_area', ('number',)),
        )),
        _type('core:ref', Ref, (('collection', ('string',)), ('key', ('union', ('string',), ('array', ('string',)))))),
        _type('swmm:files.interface', InterfaceFile, (
            ('kind', ('literal', 'RAINFALL', 'RUNOFF', 'HOTSTART', 'RDII', 'INFLOWS', 'OUTFLOWS')),
            ('mode', ('literal', 'USE', 'SAVE')), ('file', ('object', 'core:file')),
        )),
        _type('swmm:report.selection', rp.ReportSelection, (
            ('mode', ('literal', 'ALL', 'NONE', 'SELECTED')),
            ('members', ('array', ('object', 'core:ref'))),
        ), defaults=(('members', ()),)),
        _type('swmm:report.options', rp.ReportOptions, (
            ('disabled', ('union', ('boolean',), ('null',))),
            ('input', ('union', ('boolean',), ('null',))),
            ('continuity', ('union', ('boolean',), ('null',))),
            ('flow_stats', ('union', ('boolean',), ('null',))),
            ('controls', ('union', ('boolean',), ('null',))),
            ('averages', ('union', ('boolean',), ('null',))),
            ('subcatchments', ('union', ('object', 'swmm:report.selection'), ('null',))),
            ('nodes', ('union', ('object', 'swmm:report.selection'), ('null',))),
            ('links', ('union', ('object', 'swmm:report.selection'), ('null',))),
        ), defaults=(('disabled', None), ('input', None), ('continuity', None), ('flow_stats', None),
                     ('controls', None), ('averages', None), ('subcatchments', None), ('nodes', None), ('links', None))),
        _type('swmm:project.title', pr.ProjectTitle, (('lines', ('array', ('string',))),), defaults=(('lines', ()),)),
        _type('easysewer:project.json_annotation', pr.JsonAnnotation, (
            ('key', ('string',)), ('json_text', ('string',)),
        ), bases=('easysewer:project.annotation',)),
        _type('swmm:map.extent', pr.MapExtent, (
            ('lower_left', ('object', 'core:point')), ('upper_right', ('object', 'core:point')),
        )),
        _type('swmm:map.settings', pr.MapSettings, (
            ('extent', ('union', ('object', 'swmm:map.extent'), ('null',))),
            ('units', ('union', ('literal', 'FEET', 'METERS', 'DEGREES', 'NONE'), ('null',))),
            ('units_precedence', ('literal', 'MAP', 'BACKDROP')),
        ), defaults=(('extent', None), ('units', None), ('units_precedence', 'MAP')),
            source_defaults=('units_precedence',)),
        _type('swmm:map.backdrop', pr.Backdrop, (
            ('file', ('union', ('object', 'core:file'), ('null',))),
            ('extent', ('union', ('object', 'swmm:map.extent'), ('null',))),
            ('clear_file', ('boolean',)),
            ('units', ('union', ('literal', 'FEET', 'METERS', 'DEGREES', 'NONE'), ('null',))),
            ('legacy_offset', ('union', ('object', 'core:point'), ('null',))),
            ('legacy_scaling', ('union', ('object', 'core:point'), ('null',))),
        ), defaults=(('file', None), ('extent', None), ('clear_file', False), ('units', None),
                     ('legacy_offset', None), ('legacy_scaling', None)),
            source_defaults=('clear_file', 'units', 'legacy_offset', 'legacy_scaling')),
        _type('swmm:flow', i.FlowConstituent, (), singleton=i.FLOW),
        _type('swmm:offset.node_invert', v.Offset, (), singleton=v.Offset.NODE_INVERT),
        _type('core:point', v.Point, (
            ('x', ('number',)),
            ('y', ('number',)),
        )),
        _type('core:file', v.FileReference, (
            ('path', ('string',)),
            ('base_directory', ('union', ('string',), ('null',))),
            ('flavor', ('string',)),
            ('direction', ('string',)),
        ), defaults=(('base_directory', None), ('flavor', 'native'), ('direction', 'input'))),
        _type('swmm:options.day_time', o.DayTime, (
            ('clock', ('object', 'core:time')),
            ('day_offset', ('integer',)),
        ), defaults=(('clock', datetime.time(0, 0)), ('day_offset', 0))),
        _type('swmm:options.month_day', o.MonthDay, (
            ('month', ('integer',)),
            ('day', ('integer',)),
        )),
        _type('swmm:options.options', o.Options, (
            ('flow_units', ('union', ('literal', 'CFS', 'GPM', 'MGD', 'CMS', 'LPS', 'MLD'), ('null',))),
            ('infiltration', ('union', ('literal', 'HORTON', 'MODIFIED_HORTON', 'GREEN_AMPT', 'MODIFIED_GREEN_AMPT', 'CURVE_NUMBER'), ('null',))),
            ('flow_routing', ('union', ('literal', 'STEADY', 'KINWAVE', 'DYNWAVE'), ('null',))),
            ('link_offsets', ('union', ('literal', 'DEPTH', 'ELEVATION'), ('null',))),
            ('force_main_equation', ('union', ('literal', 'H-W', 'D-W'), ('null',))),
            ('ignore_rainfall', ('union', ('boolean',), ('null',))),
            ('ignore_snowmelt', ('union', ('boolean',), ('null',))),
            ('ignore_groundwater', ('union', ('boolean',), ('null',))),
            ('ignore_rdii', ('union', ('boolean',), ('null',))),
            ('ignore_routing', ('union', ('boolean',), ('null',))),
            ('ignore_quality', ('union', ('boolean',), ('null',))),
            ('allow_ponding', ('union', ('boolean',), ('null',))),
            ('skip_steady_state', ('union', ('boolean',), ('null',))),
            ('sys_flow_tol', ('union', ('number',), ('null',))),
            ('lat_flow_tol', ('union', ('number',), ('null',))),
            ('start_date', ('union', ('object', 'core:date'), ('null',))),
            ('start_time', ('union', ('object', 'core:time'), ('object', 'swmm:options.day_time'), ('null',))),
            ('end_date', ('union', ('object', 'core:date'), ('null',))),
            ('end_time', ('union', ('object', 'core:time'), ('object', 'swmm:options.day_time'), ('null',))),
            ('report_start_date', ('union', ('object', 'core:date'), ('null',))),
            ('report_start_time', ('union', ('object', 'core:time'), ('object', 'swmm:options.day_time'), ('null',))),
            ('sweep_start', ('union', ('object', 'swmm:options.month_day'), ('null',))),
            ('sweep_end', ('union', ('object', 'swmm:options.month_day'), ('null',))),
            ('dry_days', ('union', ('number',), ('null',))),
            ('report_step', ('union', ('object', 'core:duration'), ('null',))),
            ('wet_step', ('union', ('object', 'core:duration'), ('null',))),
            ('dry_step', ('union', ('object', 'core:duration'), ('null',))),
            ('routing_step', ('union', ('object', 'core:duration'), ('null',))),
            ('rule_step', ('union', ('object', 'core:duration'), ('null',))),
            ('lengthening_step', ('union', ('object', 'core:duration'), ('null',))),
            ('variable_step', ('union', ('number',), ('null',))),
            ('minimum_step', ('union', ('object', 'core:duration'), ('null',))),
            ('inertial_damping', ('union', ('literal', 'NONE', 'PARTIAL', 'FULL'), ('null',))),
            ('normal_flow_limited', ('union', ('literal', 'SLOPE', 'FROUDE', 'BOTH', 'NONE'), ('null',))),
            ('surcharge_method', ('union', ('literal', 'EXTRAN', 'SLOT'), ('null',))),
            ('min_surface_area', ('union', ('number',), ('null',))),
            ('min_slope', ('union', ('number',), ('null',))),
            ('max_trials', ('union', ('integer',), ('null',))),
            ('head_tolerance', ('union', ('number',), ('null',))),
            ('threads', ('union', ('integer',), ('null',))),
            ('slope_weighting', ('union', ('boolean',), ('null',))),
            ('compatibility', ('union', ('literal', 3, 4, 5), ('null',))),
            ('temp_directory', ('union', ('object', 'core:file'), ('null',))),
        ), defaults=(('flow_units', None), ('infiltration', None), ('flow_routing', None), ('link_offsets', None), ('force_main_equation', None), ('ignore_rainfall', None), ('ignore_snowmelt', None), ('ignore_groundwater', None), ('ignore_rdii', None), ('ignore_routing', None), ('ignore_quality', None), ('allow_ponding', None), ('skip_steady_state', None), ('sys_flow_tol', None), ('lat_flow_tol', None), ('start_date', None), ('start_time', None), ('end_date', None), ('end_time', None), ('report_start_date', None), ('report_start_time', None), ('sweep_start', None), ('sweep_end', None), ('dry_days', None), ('report_step', None), ('wet_step', None), ('dry_step', None), ('routing_step', None), ('rule_step', None), ('lengthening_step', None), ('variable_step', None), ('minimum_step', None), ('inertial_damping', None), ('normal_flow_limited', None), ('surcharge_method', None), ('min_surface_area', None), ('min_slope', None), ('max_trials', None), ('head_tolerance', None), ('threads', None), ('slope_weighting', None), ('compatibility', None), ('temp_directory', None))),
        _type('swmm:geometry.circular', g.Circular, (
            ('diameter', ('number',)),
        ), bases=('swmm:geometry.geometry',)),
        _type('swmm:geometry.force_main', g.ForceMain, (
            ('diameter', ('number',)),
            ('roughness', ('number',)),
        ), bases=('swmm:geometry.geometry',)),
        _type('swmm:geometry.filled_circular', g.FilledCircular, (
            ('diameter', ('number',)),
            ('filled_depth', ('number',)),
        ), bases=('swmm:geometry.geometry',)),
        _type('swmm:geometry.rect_closed', g.RectClosed, (
            ('full_depth', ('number',)),
            ('width', ('number',)),
        ), bases=('swmm:geometry.geometry',)),
        _type('swmm:geometry.rect_open', g.RectOpen, (
            ('full_depth', ('number',)),
            ('width', ('number',)),
            ('ignored_sides', ('union', ('number',), ('null',))),
        ), bases=('swmm:geometry.geometry',), defaults=(('ignored_sides', None),)),
        _type('swmm:geometry.trapezoidal', g.Trapezoidal, (
            ('full_depth', ('number',)),
            ('bottom_width', ('number',)),
            ('left_slope', ('number',)),
            ('right_slope', ('number',)),
        ), bases=('swmm:geometry.geometry',)),
        _type('swmm:geometry.triangular', g.Triangular, (
            ('full_depth', ('number',)),
            ('top_width', ('number',)),
        ), bases=('swmm:geometry.geometry',)),
        _type('swmm:geometry.horizontal_ellipse', g.HorizontalEllipse, (
            ('full_depth', ('union', ('number',), ('null',))),
            ('width', ('union', ('number',), ('null',))),
            ('size_code', ('union', ('integer',), ('null',))),
        ), bases=('swmm:geometry.standard_size', 'swmm:geometry.geometry'), defaults=(('full_depth', None), ('width', None), ('size_code', None))),
        _type('swmm:geometry.vertical_ellipse', g.VerticalEllipse, (
            ('full_depth', ('union', ('number',), ('null',))),
            ('width', ('union', ('number',), ('null',))),
            ('size_code', ('union', ('integer',), ('null',))),
        ), bases=('swmm:geometry.standard_size', 'swmm:geometry.geometry'), defaults=(('full_depth', None), ('width', None), ('size_code', None))),
        _type('swmm:geometry.arch', g.Arch, (
            ('full_depth', ('union', ('number',), ('null',))),
            ('width', ('union', ('number',), ('null',))),
            ('size_code', ('union', ('integer',), ('null',))),
        ), bases=('swmm:geometry.standard_size', 'swmm:geometry.geometry'), defaults=(('full_depth', None), ('width', None), ('size_code', None))),
        _type('swmm:geometry.parabolic', g.Parabolic, (
            ('full_depth', ('number',)),
            ('top_width', ('number',)),
        ), bases=('swmm:geometry.geometry',)),
        _type('swmm:geometry.power', g.Power, (
            ('full_depth', ('number',)),
            ('top_width', ('number',)),
            ('exponent', ('number',)),
        ), bases=('swmm:geometry.geometry',)),
        _type('swmm:geometry.rect_triangular', g.RectTriangular, (
            ('full_depth', ('number',)),
            ('top_width', ('number',)),
            ('triangle_depth', ('number',)),
        ), bases=('swmm:geometry.geometry',)),
        _type('swmm:geometry.rect_round', g.RectRound, (
            ('full_depth', ('number',)),
            ('top_width', ('number',)),
            ('bottom_radius', ('number',)),
        ), bases=('swmm:geometry.geometry',)),
        _type('swmm:geometry.modified_basket_handle', g.ModifiedBasketHandle, (
            ('full_depth', ('number',)),
            ('bottom_width', ('number',)),
            ('top_radius', ('number',)),
        ), bases=('swmm:geometry.geometry',)),
        _type('swmm:geometry.egg', g.Egg, (
            ('full_depth', ('number',)),
        ), bases=('swmm:geometry.depth_shape', 'swmm:geometry.geometry')),
        _type('swmm:geometry.horseshoe', g.Horseshoe, (
            ('full_depth', ('number',)),
        ), bases=('swmm:geometry.depth_shape', 'swmm:geometry.geometry')),
        _type('swmm:geometry.gothic', g.Gothic, (
            ('full_depth', ('number',)),
        ), bases=('swmm:geometry.depth_shape', 'swmm:geometry.geometry')),
        _type('swmm:geometry.catenary', g.Catenary, (
            ('full_depth', ('number',)),
        ), bases=('swmm:geometry.depth_shape', 'swmm:geometry.geometry')),
        _type('swmm:geometry.semi_elliptical', g.SemiElliptical, (
            ('full_depth', ('number',)),
        ), bases=('swmm:geometry.depth_shape', 'swmm:geometry.geometry')),
        _type('swmm:geometry.basket_handle', g.BasketHandle, (
            ('full_depth', ('number',)),
        ), bases=('swmm:geometry.depth_shape', 'swmm:geometry.geometry')),
        _type('swmm:geometry.semi_circular', g.SemiCircular, (
            ('full_depth', ('number',)),
        ), bases=('swmm:geometry.depth_shape', 'swmm:geometry.geometry')),
        _type('swmm:geometry.custom', g.Custom, (
            ('full_depth', ('number',)),
            ('curve', ('object', 'core:ref')),
        ), bases=('swmm:geometry.geometry',)),
        _type('swmm:geometry.irregular', g.Irregular, (
            ('transect', ('object', 'core:ref')),
        ), bases=('swmm:geometry.geometry',)),
        _type('swmm:geometry.street', g.Street, (
            ('street', ('object', 'core:ref')),
        ), bases=('swmm:geometry.geometry',)),
        _type('swmm:geometry.dummy', g.Dummy, (
        ), bases=('swmm:geometry.geometry',)),
        _type('swmm:geometry.cross_section', g.CrossSection, (
            ('geometry', ('object', 'swmm:geometry.geometry')),
            ('barrels', ('union', ('integer',), ('null',))),
            ('culvert', ('union', ('integer',), ('null',))),
        ), defaults=(('barrels', None), ('culvert', None))),
        _type('swmm:network.junction', n.Junction, (
            ('id', ('string',)),
            ('elevation', ('number',)),
            ('position', ('union', ('object', 'core:point'), ('null',))),
            ('max_depth', ('union', ('number',), ('null',))),
            ('initial_depth', ('union', ('number',), ('null',))),
            ('surcharge_depth', ('union', ('number',), ('null',))),
            ('ponded_area', ('union', ('number',), ('null',))),
        ), bases=('swmm:network.node', 'swmm:network.entity'), defaults=(('position', None), ('max_depth', None), ('initial_depth', None), ('surcharge_depth', None), ('ponded_area', None))),
        _type('swmm:network.free_boundary', n.FreeBoundary, (
        ), bases=('swmm:network.boundary',)),
        _type('swmm:network.normal_boundary', n.NormalBoundary, (
        ), bases=('swmm:network.boundary',)),
        _type('swmm:network.fixed_boundary', n.FixedBoundary, (
            ('stage', ('number',)),
        ), bases=('swmm:network.boundary',)),
        _type('swmm:network.tidal_boundary', n.TidalBoundary, (
            ('curve', ('object', 'core:ref')),
        ), bases=('swmm:network.boundary',)),
        _type('swmm:network.series_boundary', n.SeriesBoundary, (
            ('series', ('object', 'core:ref')),
        ), bases=('swmm:network.boundary',)),
        _type('swmm:network.outfall', n.Outfall, (
            ('id', ('string',)),
            ('elevation', ('number',)),
            ('position', ('union', ('object', 'core:point'), ('null',))),
            ('boundary', ('object', 'swmm:network.boundary')),
            ('gated', ('union', ('boolean',), ('null',))),
            ('route_to', ('union', ('object', 'core:ref'), ('null',))),
        ), bases=('swmm:network.node', 'swmm:network.entity'), defaults=(('position', None), ('gated', None), ('route_to', None))),
        _type('swmm:network.functional_storage', n.FunctionalStorage, (
            ('coefficient', ('number',)),
            ('exponent', ('number',)),
            ('constant', ('number',)),
        ), bases=('swmm:network.storage_shape',)),
        _type('swmm:network.tabular_storage', n.TabularStorage, (
            ('curve', ('object', 'core:ref')),
        ), bases=('swmm:network.storage_shape',)),
        _type('swmm:network.cylindrical_storage', n.CylindricalStorage, (
            ('major_axis', ('number',)),
            ('minor_axis', ('number',)),
        ), bases=('swmm:network.storage_shape',)),
        _type('swmm:network.conical_storage', n.ConicalStorage, (
            ('base_major_axis', ('number',)),
            ('base_minor_axis', ('number',)),
            ('side_slope', ('number',)),
        ), bases=('swmm:network.storage_shape',)),
        _type('swmm:network.paraboloid_storage', n.ParaboloidStorage, (
            ('top_major_axis', ('number',)),
            ('top_minor_axis', ('number',)),
            ('full_height', ('number',)),
        ), bases=('swmm:network.storage_shape',)),
        _type('swmm:network.pyramidal_storage', n.PyramidalStorage, (
            ('base_length', ('number',)),
            ('base_width', ('number',)),
            ('side_slope', ('number',)),
        ), bases=('swmm:network.storage_shape',)),
        _type('swmm:network.constant_seepage', n.ConstantSeepage, (
            ('conductivity', ('number',)),
        ), bases=('swmm:network.storage_seepage',)),
        _type('swmm:network.seepage', n.Seepage, (
            ('suction', ('number',)),
            ('conductivity', ('number',)),
            ('initial_deficit', ('number',)),
        ), bases=('swmm:network.storage_seepage',)),
        _type('swmm:network.storage', n.Storage, (
            ('id', ('string',)),
            ('elevation', ('number',)),
            ('position', ('union', ('object', 'core:point'), ('null',))),
            ('max_depth', ('number',)),
            ('initial_depth', ('number',)),
            ('shape', ('object', 'swmm:network.storage_shape')),
            ('surcharge_depth', ('union', ('number',), ('null',))),
            ('evaporation_fraction', ('union', ('number',), ('null',))),
            ('seepage', ('union', ('object', 'swmm:network.storage_seepage'), ('null',))),
            ('polygon', ('array', ('object', 'core:point'))),
        ), bases=('swmm:network.node', 'swmm:network.entity'), defaults=(('position', None), ('surcharge_depth', None), ('evaporation_fraction', None), ('seepage', None), ('polygon', ())),
            source_defaults=('polygon',)),
        _type('swmm:network.overflow_divider', n.OverflowDivider, (
        ), bases=('swmm:network.divider_law',)),
        _type('swmm:network.cutoff_divider', n.CutoffDivider, (
            ('cutoff_flow', ('number',)),
        ), bases=('swmm:network.divider_law',)),
        _type('swmm:network.tabular_divider', n.TabularDivider, (
            ('curve', ('object', 'core:ref')),
        ), bases=('swmm:network.divider_law',)),
        _type('swmm:network.weir_divider', n.WeirDivider, (
            ('minimum_flow', ('number',)),
            ('height', ('number',)),
            ('coefficient', ('number',)),
        ), bases=('swmm:network.divider_law',)),
        _type('swmm:network.divider', n.Divider, (
            ('id', ('string',)),
            ('elevation', ('number',)),
            ('position', ('union', ('object', 'core:point'), ('null',))),
            ('diverted_link', ('union', ('object', 'core:ref'), ('null',))),
            ('law', ('object', 'swmm:network.divider_law')),
            ('max_depth', ('union', ('number',), ('null',))),
            ('initial_depth', ('union', ('number',), ('null',))),
            ('surcharge_depth', ('union', ('number',), ('null',))),
            ('ponded_area', ('union', ('number',), ('null',))),
        ), bases=('swmm:network.node', 'swmm:network.entity'), defaults=(('position', None), ('diverted_link', None), ('max_depth', None), ('initial_depth', None), ('surcharge_depth', None), ('ponded_area', None))),
        _type('swmm:network.conduit_losses', n.ConduitLosses, (
            ('entry', ('number',)),
            ('exit', ('number',)),
            ('average', ('number',)),
            ('flap_gate', ('union', ('boolean',), ('null',))),
            ('seepage', ('union', ('number',), ('null',))),
        ), defaults=(('flap_gate', None), ('seepage', None))),
        _type('swmm:network.conduit', n.Conduit, (
            ('id', ('string',)),
            ('inlet', ('object', 'core:ref')),
            ('outlet', ('object', 'core:ref')),
            ('vertices', ('array', ('object', 'core:point'))),
            ('length', ('number',)),
            ('roughness', ('number',)),
            ('inlet_offset', ('union', ('number',), ('object', 'swmm:offset.node_invert'), ('null',))),
            ('outlet_offset', ('union', ('number',), ('object', 'swmm:offset.node_invert'), ('null',))),
            ('initial_flow', ('union', ('number',), ('null',))),
            ('maximum_flow', ('union', ('number',), ('null',))),
            ('section', ('union', ('object', 'swmm:geometry.cross_section'), ('null',))),
            ('losses', ('union', ('object', 'swmm:network.conduit_losses'), ('null',))),
        ), bases=('swmm:network.link', 'swmm:network.entity'), defaults=(('vertices', ()), ('inlet_offset', None), ('outlet_offset', None), ('initial_flow', None), ('maximum_flow', None), ('section', None), ('losses', None))),
        _type('swmm:network.pump', n.Pump, (
            ('id', ('string',)),
            ('inlet', ('object', 'core:ref')),
            ('outlet', ('object', 'core:ref')),
            ('vertices', ('array', ('object', 'core:point'))),
            ('curve', ('union', ('object', 'core:ref'), ('null',))),
            ('initially_on', ('union', ('boolean',), ('null',))),
            ('startup_depth', ('union', ('number',), ('null',))),
            ('shutoff_depth', ('union', ('number',), ('null',))),
        ), bases=('swmm:network.link', 'swmm:network.entity'), defaults=(('vertices', ()), ('curve', None), ('initially_on', None), ('startup_depth', None), ('shutoff_depth', None))),
        _type('swmm:network.orifice', n.Orifice, (
            ('id', ('string',)),
            ('inlet', ('object', 'core:ref')),
            ('outlet', ('object', 'core:ref')),
            ('vertices', ('array', ('object', 'core:point'))),
            ('orientation', ('literal', 'SIDE', 'BOTTOM')),
            ('offset', ('union', ('number',), ('object', 'swmm:offset.node_invert'), ('null',))),
            ('coefficient', ('number',)),
            ('gated', ('union', ('boolean',), ('null',))),
            ('opening_time', ('union', ('object', 'core:duration'), ('null',))),
            ('section', ('union', ('object', 'swmm:geometry.cross_section'), ('null',))),
        ), bases=('swmm:network.link', 'swmm:network.entity'), defaults=(('vertices', ()), ('offset', None), ('gated', None), ('opening_time', None), ('section', None))),
        _type('swmm:network.weir', n.Weir, (
            ('id', ('string',)),
            ('inlet', ('object', 'core:ref')),
            ('outlet', ('object', 'core:ref')),
            ('vertices', ('array', ('object', 'core:point'))),
            ('weir_type', ('literal', 'TRANSVERSE', 'SIDEFLOW', 'V-NOTCH', 'TRAPEZOIDAL', 'ROADWAY')),
            ('crest_height', ('union', ('number',), ('object', 'swmm:offset.node_invert'), ('null',))),
            ('coefficient', ('number',)),
            ('gated', ('union', ('boolean',), ('null',))),
            ('end_contractions', ('union', ('number',), ('null',))),
            ('end_coefficient', ('union', ('number',), ('null',))),
            ('can_surcharge', ('union', ('boolean',), ('null',))),
            ('road_width', ('union', ('number',), ('null',))),
            ('road_surface', ('union', ('literal', 'PAVED', 'GRAVEL'), ('null',))),
            ('coefficient_curve', ('union', ('object', 'core:ref'), ('null',))),
            ('section', ('union', ('object', 'swmm:geometry.cross_section'), ('null',))),
        ), bases=('swmm:network.link', 'swmm:network.entity'), defaults=(('vertices', ()), ('crest_height', None), ('gated', None), ('end_contractions', None), ('end_coefficient', None), ('can_surcharge', None), ('road_width', None), ('road_surface', None), ('coefficient_curve', None), ('section', None))),
        _type('swmm:network.functional_rating', n.FunctionalRating, (
            ('basis', ('literal', 'DEPTH', 'HEAD')),
            ('coefficient', ('number',)),
            ('exponent', ('number',)),
        ), bases=('swmm:network.outlet_rating',)),
        _type('swmm:network.tabular_rating', n.TabularRating, (
            ('basis', ('literal', 'DEPTH', 'HEAD')),
            ('curve', ('object', 'core:ref')),
        ), bases=('swmm:network.outlet_rating',)),
        _type('swmm:network.outlet', n.Outlet, (
            ('id', ('string',)),
            ('inlet', ('object', 'core:ref')),
            ('outlet', ('object', 'core:ref')),
            ('vertices', ('array', ('object', 'core:point'))),
            ('offset', ('union', ('number',), ('object', 'swmm:offset.node_invert'), ('null',))),
            ('rating', ('object', 'swmm:network.outlet_rating')),
            ('gated', ('union', ('boolean',), ('null',))),
        ), bases=('swmm:network.link', 'swmm:network.entity'), defaults=(('vertices', ()), ('offset', None), ('gated', None))),
        _type('swmm:resources.curve_point', r.CurvePoint, (
            ('x', ('number',)),
            ('y', ('number',)),
        )),
        _type('swmm:resources.curve', r.Curve, (
            ('id', ('string',)),
            ('kind', ('literal', 'STORAGE', 'SHAPE', 'DIVERSION', 'TIDAL', 'PUMP1', 'PUMP2', 'PUMP3', 'PUMP4', 'PUMP5', 'RATING', 'CONTROL', 'WEIR')),
            ('points', ('array', ('object', 'swmm:resources.curve_point'))),
        ), bases=('swmm:resources.resource',), defaults=(('points', ()),)),
        _type('swmm:resources.series_point', r.SeriesPoint, (
            ('time', ('union', ('object', 'core:datetime'), ('object', 'core:duration'))),
            ('value', ('number',)),
        )),
        _type('swmm:resources.inline_time_series', r.InlineTimeSeries, (
            ('id', ('string',)),
            ('points', ('array', ('object', 'swmm:resources.series_point'))),
        ), bases=('swmm:resources.time_series', 'swmm:resources.resource')),
        _type('swmm:resources.file_time_series', r.FileTimeSeries, (
            ('id', ('string',)),
            ('file', ('object', 'core:file')),
        ), bases=('swmm:resources.time_series', 'swmm:resources.resource')),
        _type('swmm:resources.pattern', r.Pattern, (
            ('id', ('string',)),
            ('kind', ('literal', 'MONTHLY', 'DAILY', 'HOURLY', 'WEEKEND')),
            ('factors', ('array', ('number',))),
        ), bases=('swmm:resources.resource',), defaults=(('factors', ()),)),
        _type('swmm:surface.transect_roughness', s.TransectRoughness, (
            ('left', ('number',)),
            ('right', ('number',)),
            ('channel', ('number',)),
        )),
        _type('swmm:surface.transect_point', s.TransectPoint, (
            ('elevation', ('number',)),
            ('station', ('number',)),
        )),
        _type('swmm:surface.transect', s.Transect, (
            ('id', ('string',)),
            ('roughness', ('object', 'swmm:surface.transect_roughness')),
            ('left_bank', ('number',)),
            ('right_bank', ('number',)),
            ('stations', ('array', ('object', 'swmm:surface.transect_point'))),
            ('meander_factor', ('number',)),
            ('width_factor', ('number',)),
            ('elevation_offset', ('number',)),
        ), bases=('swmm:resources.resource',), defaults=(('meander_factor', 0), ('width_factor', 0), ('elevation_offset', 0))),
        _type('swmm:surface.street_section', s.StreetSection, (
            ('id', ('string',)),
            ('crown_width', ('number',)),
            ('curb_height', ('number',)),
            ('cross_slope', ('number',)),
            ('road_roughness', ('number',)),
            ('gutter_depression', ('union', ('number',), ('null',))),
            ('gutter_width', ('union', ('number',), ('null',))),
            ('sides', ('union', ('literal', 1, 2), ('null',))),
            ('backing_width', ('union', ('number',), ('null',))),
            ('backing_slope', ('union', ('number',), ('null',))),
            ('backing_roughness', ('union', ('number',), ('null',))),
        ), bases=('swmm:resources.resource',), defaults=(('gutter_depression', None), ('gutter_width', None), ('sides', None), ('backing_width', None), ('backing_slope', None), ('backing_roughness', None))),
        _type('swmm:surface.standard_grate', s.StandardGrate, (
            ('kind', ('literal', 'P_BAR-50', 'P_BAR-50X100', 'P_BAR-30', 'CURVED_VANE', 'TILT_BAR-45', 'TILT_BAR-30', 'RETICULINE')),
        ), bases=('swmm:surface.grate_type',)),
        _type('swmm:surface.generic_grate', s.GenericGrate, (
            ('open_fraction', ('number',)),
            ('splash_velocity', ('union', ('number',), ('null',))),
        ), bases=('swmm:surface.grate_type',), defaults=(('splash_velocity', None),)),
        _type('swmm:surface.grate_inlet', s.GrateInlet, (
            ('kind', ('literal', 'GRATE', 'DROP_GRATE')),
            ('length', ('number',)),
            ('width', ('number',)),
            ('grate', ('object', 'swmm:surface.grate_type')),
        ), bases=('swmm:surface.inlet_structure',)),
        _type('swmm:surface.curb_inlet', s.CurbInlet, (
            ('kind', ('literal', 'CURB', 'DROP_CURB')),
            ('length', ('number',)),
            ('height', ('number',)),
            ('throat', ('union', ('literal', 'HORIZONTAL', 'INCLINED', 'VERTICAL'), ('null',))),
        ), bases=('swmm:surface.inlet_structure',), defaults=(('throat', None),)),
        _type('swmm:surface.combination_inlet', s.CombinationInlet, (
            ('grate', ('object', 'swmm:surface.grate_inlet')),
            ('curb', ('object', 'swmm:surface.curb_inlet')),
        ), bases=('swmm:surface.inlet_structure',)),
        _type('swmm:surface.slotted_inlet', s.SlottedInlet, (
            ('length', ('number',)),
            ('width', ('number',)),
        ), bases=('swmm:surface.inlet_structure',)),
        _type('swmm:surface.custom_inlet', s.CustomInlet, (
            ('curve', ('object', 'core:ref')),
        ), bases=('swmm:surface.inlet_structure',)),
        _type('swmm:surface.inlet_design', s.InletDesign, (
            ('id', ('string',)),
            ('design', ('object', 'swmm:surface.inlet_structure')),
        ), bases=('swmm:resources.resource',)),
        _type('swmm:surface.inlet_usage', s.InletUsage, (
            ('link', ('object', 'core:ref')),
            ('inlet', ('object', 'core:ref')),
            ('node', ('object', 'core:ref')),
            ('count', ('union', ('integer',), ('null',))),
            ('percent_clogged', ('union', ('number',), ('null',))),
            ('maximum_flow', ('union', ('number',), ('null',))),
            ('local_depression', ('union', ('number',), ('null',))),
            ('local_width', ('union', ('number',), ('null',))),
            ('placement', ('union', ('literal', 'AUTOMATIC', 'ON_GRADE', 'ON_SAG'), ('null',))),
        ), defaults=(('count', None), ('percent_clogged', None), ('maximum_flow', None), ('local_depression', None), ('local_width', None), ('placement', None))),
        _type('swmm:climate.monthly_factors', cl.MonthlyFactors, (
            ('values', ('array', ('number',))),
        ), bases=('swmm:climate.monthly_values',)),
        _type('swmm:climate.monthly_wind_speeds', cl.MonthlyWindSpeeds, (
            ('values', ('array', ('number',))),
        ), bases=('swmm:climate.monthly_values',)),
        _type('swmm:climate.monthly_evaporation', cl.MonthlyEvaporation, (
            ('values', ('array', ('number',))),
        ), bases=('swmm:climate.monthly_values',)),
        _type('swmm:climate.monthly_temperature_changes', cl.MonthlyTemperatureChanges, (
            ('values', ('array', ('number',))),
        ), bases=('swmm:climate.monthly_values',)),
        _type('swmm:climate.file_temperature', cl.FileTemperature, (
        )),
        _type('swmm:climate.series_temperature', cl.SeriesTemperature, (
            ('series', ('object', 'core:ref')),
        )),
        _type('swmm:climate.climate_file', cl.ClimateFile, (
            ('file', ('object', 'core:file')),
            ('start_date', ('union', ('object', 'core:date'), ('null',))),
            ('units', ('union', ('literal', 'C10', 'C', 'F'), ('null',))),
        ), defaults=(('start_date', None), ('units', None))),
        _type('swmm:climate.file_wind', cl.FileWind, (
        )),
        _type('swmm:climate.snowmelt', cl.Snowmelt, (
            ('snowfall_temperature', ('number',)),
            ('antecedent_weight', ('number',)),
            ('negative_melt_ratio', ('number',)),
            ('elevation', ('number',)),
            ('latitude', ('number',)),
            ('solar_time_correction', ('number',)),
        )),
        _type('swmm:climate.areal_depletion', cl.ArealDepletion, (
            ('fractions', ('array', ('number',))),
        )),
        _type('swmm:climate.constant_evaporation', cl.ConstantEvaporation, (
            ('rate', ('number',)),
        )),
        _type('swmm:climate.series_evaporation', cl.SeriesEvaporation, (
            ('series', ('object', 'core:ref')),
        )),
        _type('swmm:climate.temperature_evaporation', cl.TemperatureEvaporation, (
        )),
        _type('swmm:climate.file_evaporation', cl.FileEvaporation, (
            ('pan_coefficients', ('union', ('object', 'swmm:climate.monthly_factors'), ('null',))),
        ), defaults=(('pan_coefficients', None),)),
        _type('swmm:climate.evaporation', cl.Evaporation, (
            ('source', ('union', ('object', 'swmm:climate.constant_evaporation'), ('object', 'swmm:climate.monthly_evaporation'), ('object', 'swmm:climate.series_evaporation'), ('object', 'swmm:climate.temperature_evaporation'), ('object', 'swmm:climate.file_evaporation'), ('null',))),
            ('recovery_pattern', ('union', ('object', 'core:ref'), ('null',))),
            ('dry_only', ('union', ('boolean',), ('null',))),
        ), defaults=(('source', None), ('recovery_pattern', None), ('dry_only', None))),
        _type('swmm:climate.climate_adjustments', cl.ClimateAdjustments, (
            ('temperature', ('union', ('object', 'swmm:climate.monthly_temperature_changes'), ('null',))),
            ('evaporation', ('union', ('object', 'swmm:climate.monthly_evaporation'), ('null',))),
            ('rainfall', ('union', ('object', 'swmm:climate.monthly_factors'), ('null',))),
            ('conductivity', ('union', ('object', 'swmm:climate.monthly_factors'), ('null',))),
        ), defaults=(('temperature', None), ('evaporation', None), ('rainfall', None), ('conductivity', None))),
        _type('swmm:climate.subcatchment_adjustments', cl.SubcatchmentAdjustments, (
            ('subcatchment', ('object', 'core:ref')),
            ('infiltration', ('union', ('object', 'core:ref'), ('null',))),
            ('depression_storage', ('union', ('object', 'core:ref'), ('null',))),
            ('pervious_roughness', ('union', ('object', 'core:ref'), ('null',))),
        ), defaults=(('infiltration', None), ('depression_storage', None), ('pervious_roughness', None))),
        _type('swmm:climate.climate', cl.Climate, (
            ('temperature', ('union', ('object', 'swmm:climate.file_temperature'), ('object', 'swmm:climate.series_temperature'), ('null',))),
            ('file', ('union', ('object', 'swmm:climate.climate_file'), ('null',))),
            ('wind', ('union', ('object', 'swmm:climate.monthly_wind_speeds'), ('object', 'swmm:climate.file_wind'), ('null',))),
            ('snowmelt', ('union', ('object', 'swmm:climate.snowmelt'), ('null',))),
            ('impervious_depletion', ('union', ('object', 'swmm:climate.areal_depletion'), ('null',))),
            ('pervious_depletion', ('union', ('object', 'swmm:climate.areal_depletion'), ('null',))),
            ('evaporation', ('union', ('object', 'swmm:climate.evaporation'), ('null',))),
            ('adjustments', ('union', ('object', 'swmm:climate.climate_adjustments'), ('null',))),
        ), defaults=(('temperature', None), ('file', None), ('wind', None), ('snowmelt', None), ('impervious_depletion', None), ('pervious_depletion', None), ('evaporation', None), ('adjustments', None))),
        _type('swmm:hydrology.series_rainfall', h.SeriesRainfall, (
            ('series', ('object', 'core:ref')),
        )),
        _type('swmm:hydrology.file_rainfall', h.FileRainfall, (
            ('file', ('object', 'core:file')),
            ('station', ('string',)),
            ('units', ('literal', 'IN', 'MM')),
            ('start_date', ('union', ('object', 'core:date'), ('null',))),
        ), defaults=(('start_date', None),)),
        _type('swmm:hydrology.rain_gage', h.RainGage, (
            ('id', ('string',)),
            ('form', ('literal', 'INTENSITY', 'VOLUME', 'CUMULATIVE')),
            ('interval', ('object', 'core:duration')),
            ('snow_factor', ('number',)),
            ('source', ('union', ('object', 'swmm:hydrology.series_rainfall'), ('object', 'swmm:hydrology.file_rainfall'))),
            ('position', ('union', ('object', 'core:point'), ('null',))),
        ), bases=('swmm:network.entity',), defaults=(('position', None),)),
        _type('swmm:hydrology.horton', h.Horton, (
            ('maximum_rate', ('number',)),
            ('minimum_rate', ('number',)),
            ('decay', ('number',)),
            ('drying_time', ('number',)),
            ('maximum_volume', ('union', ('number',), ('null',))),
        ), defaults=(('maximum_volume', None),)),
        _type('swmm:hydrology.green_ampt', h.GreenAmpt, (
            ('suction', ('number',)),
            ('conductivity', ('number',)),
            ('initial_deficit', ('number',)),
        )),
        _type('swmm:hydrology.curve_number', h.CurveNumber, (
            ('curve_number', ('number',)),
            ('drying_time', ('number',)),
        )),
        _type('swmm:hydrology.infiltration', h.Infiltration, (
            ('parameters', ('union', ('object', 'swmm:hydrology.horton'), ('object', 'swmm:hydrology.green_ampt'), ('object', 'swmm:hydrology.curve_number'))),
            ('method', ('union', ('literal', 'HORTON', 'MODIFIED_HORTON', 'GREEN_AMPT', 'MODIFIED_GREEN_AMPT', 'CURVE_NUMBER'), ('null',))),
        ), defaults=(('method', None),)),
        _type('swmm:hydrology.subareas', h.Subareas, (
            ('impervious_roughness', ('number',)),
            ('pervious_roughness', ('number',)),
            ('impervious_storage', ('number',)),
            ('pervious_storage', ('number',)),
            ('zero_storage_percent', ('number',)),
            ('route_to', ('literal', 'OUTLET', 'IMPERVIOUS', 'PERVIOUS')),
            ('routed_percent', ('union', ('number',), ('null',))),
        ), defaults=(('route_to', 'OUTLET'), ('routed_percent', None))),
        _type('swmm:hydrology.subcatchment', h.Subcatchment, (
            ('id', ('string',)),
            ('rain_gage', ('object', 'core:ref')),
            ('outlet', ('object', 'core:ref')),
            ('area', ('number',)),
            ('impervious_percent', ('number',)),
            ('width', ('number',)),
            ('slope', ('number',)),
            ('curb_length', ('number',)),
            ('snowpack', ('union', ('object', 'core:ref'), ('null',))),
            ('subareas', ('union', ('object', 'swmm:hydrology.subareas'), ('null',))),
            ('infiltration', ('union', ('object', 'swmm:hydrology.infiltration'), ('null',))),
            ('polygon', ('array', ('object', 'core:point'))),
        ), bases=('swmm:network.entity',), defaults=(('snowpack', None), ('subareas', None), ('infiltration', None), ('polygon', ()))),
        _type('swmm:hydrology.plowable_snow', h.PlowableSnow, (
            ('minimum_melt', ('number',)),
            ('maximum_melt', ('number',)),
            ('base_temperature', ('number',)),
            ('free_water_fraction', ('number',)),
            ('initial_snow', ('number',)),
            ('initial_free_water', ('number',)),
            ('fraction', ('number',)),
        ), bases=('swmm:hydrology.snow_surface',)),
        _type('swmm:hydrology.depletable_snow', h.DepletableSnow, (
            ('minimum_melt', ('number',)),
            ('maximum_melt', ('number',)),
            ('base_temperature', ('number',)),
            ('free_water_fraction', ('number',)),
            ('initial_snow', ('number',)),
            ('initial_free_water', ('number',)),
            ('full_cover_depth', ('number',)),
        ), bases=('swmm:hydrology.snow_surface',)),
        _type('swmm:hydrology.snow_removal', h.SnowRemoval, (
            ('threshold', ('number',)),
            ('out_of_system', ('number',)),
            ('to_impervious', ('number',)),
            ('to_pervious', ('number',)),
            ('immediate_melt', ('number',)),
            ('to_subcatchment', ('union', ('number',), ('null',))),
            ('destination', ('union', ('object', 'core:ref'), ('null',))),
        ), defaults=(('to_subcatchment', None), ('destination', None))),
        _type('swmm:hydrology.snowpack', h.Snowpack, (
            ('id', ('string',)),
            ('plowable', ('union', ('object', 'swmm:hydrology.plowable_snow'), ('null',))),
            ('impervious', ('union', ('object', 'swmm:hydrology.depletable_snow'), ('null',))),
            ('pervious', ('union', ('object', 'swmm:hydrology.depletable_snow'), ('null',))),
            ('removal', ('union', ('object', 'swmm:hydrology.snow_removal'), ('null',))),
        ), bases=('swmm:network.entity',), defaults=(('plowable', None), ('impervious', None), ('pervious', None), ('removal', None))),
        _type('swmm:inflows.flow_inflow', i.FlowInflow, (
            ('node', ('object', 'core:ref')),
            ('constituent', ('literal', i.FLOW)),
            ('series', ('union', ('object', 'core:ref'), ('null',))),
            ('scale_factor', ('union', ('number',), ('null',))),
            ('baseline', ('union', ('number',), ('null',))),
            ('pattern', ('union', ('object', 'core:ref'), ('null',))),
        ), bases=('swmm:inflows.external_inflow',), defaults=(('constituent', i.FLOW), ('series', None), ('scale_factor', None), ('baseline', None), ('pattern', None))),
        _type('swmm:inflows.dry_weather_flow', i.DryWeatherFlow, (
            ('node', ('object', 'core:ref')),
            ('constituent', ('literal', i.FLOW)),
            ('baseline', ('number',)),
            ('patterns', ('array', ('union', ('object', 'core:ref'), ('null',)))),
        ), bases=('swmm:inflows.dry_weather_inflow',), defaults=(('constituent', i.FLOW), ('patterns', ()))),
        _type('swmm:controls.attribute', c.Attribute, (
            ('object_type', ('string',)),
            ('attribute', ('string',)),
            ('target', ('union', ('object', 'core:ref'), ('null',))),
            ('history_hours', ('union', ('integer',), ('null',))),
        ), bases=('swmm:controls.operand',), defaults=(('target', None), ('history_hours', None))),
        _type('swmm:controls.named_operand', c.NamedOperand, (
            ('reference', ('object', 'core:ref')),
        ), bases=('swmm:controls.operand',)),
        _type('swmm:controls.constant', c.Constant, (
            ('value', ('union', ('number',), ('object', 'core:duration'), ('object', 'core:date'), ('object', 'swmm:options.month_day'), ('string',))),
        ), bases=('swmm:controls.operand',)),
        _type('swmm:controls.expression_number', c.ExpressionNumber, (
            ('value', ('number',)),
        ), bases=('swmm:controls.expression_node','core:expression')),
        _type('swmm:controls.expression_variable', c.ExpressionVariable, (
            ('reference', ('object', 'core:ref')),
        ), bases=('swmm:controls.expression_node','core:expression')),
        _type('swmm:controls.unary_expression', c.UnaryExpression, (
            ('operator', ('literal', '+', '-')),
            ('operand', ('object', 'core:expression')),
        ), bases=('swmm:controls.expression_node','core:expression')),
        _type('swmm:controls.binary_expression', c.BinaryExpression, (
            ('operator', ('literal', '+', '-', '*', '/', '^')),
            ('left', ('object', 'core:expression')),
            ('right', ('object', 'core:expression')),
        ), bases=('swmm:controls.expression_node','core:expression')),
        _type('swmm:controls.function_expression', c.FunctionExpression, (
            ('function', ('string',)),
            ('argument', ('object', 'core:expression')),
        ), bases=('swmm:controls.expression_node','core:expression')),
        _type('swmm:controls.condition', c.Condition, (
            ('left', ('object', 'swmm:controls.operand')),
            ('relation', ('literal', '=', '<>', '<', '<=', '>', '>=')),
            ('right', ('object', 'swmm:controls.operand')),
            ('conjunction', ('literal', 'IF', 'AND', 'OR')),
        ), defaults=(('conjunction', 'IF'),)),
        _type('swmm:controls.status_setting', c.StatusSetting, (
            ('value', ('literal', 'ON', 'OFF', 'OPEN', 'CLOSED')),
        ), bases=('swmm:controls.setting',)),
        _type('swmm:controls.numeric_setting', c.NumericSetting, (
            ('value', ('number',)),
        ), bases=('swmm:controls.setting',)),
        _type('swmm:controls.curve_setting', c.CurveSetting, (
            ('curve', ('object', 'core:ref')),
        ), bases=('swmm:controls.setting',)),
        _type('swmm:controls.series_setting', c.SeriesSetting, (
            ('series', ('object', 'core:ref')),
        ), bases=('swmm:controls.setting',)),
        _type('swmm:controls.pid_setting', c.PIDSetting, (
            ('gain', ('number',)),
            ('integral_time', ('object', 'core:duration')),
            ('derivative_time', ('object', 'core:duration')),
        ), bases=('swmm:controls.setting',)),
        _type('swmm:controls.action', c.Action, (
            ('target', ('object', 'core:ref')),
            ('object_type', ('literal', 'CONDUIT', 'PUMP', 'ORIFICE', 'WEIR', 'OUTLET')),
            ('setting', ('object', 'swmm:controls.setting')),
        )),
        _type('swmm:controls.control_variable', c.ControlVariable, (
            ('id', ('string',)),
            ('value', ('object', 'swmm:controls.attribute')),
        ), bases=('swmm:controls.control_statement', 'swmm:network.entity')),
        _type('swmm:controls.control_expression', c.ControlExpression, (
            ('id', ('string',)),
            ('expression', ('object', 'swmm:controls.expression_node')),
        ), bases=('swmm:controls.control_statement', 'swmm:network.entity')),
        _type('swmm:controls.control_rule', c.ControlRule, (
            ('id', ('string',)),
            ('conditions', ('array', ('object', 'swmm:controls.condition'))),
            ('then_actions', ('array', ('object', 'swmm:controls.action'))),
            ('else_actions', ('array', ('object', 'swmm:controls.action'))),
            ('priority', ('union', ('number',), ('null',))),
        ), bases=('swmm:controls.control_statement', 'swmm:network.entity'), defaults=(('else_actions', ()), ('priority', None))),
    )
