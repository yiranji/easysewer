import contextlib,hashlib,importlib.metadata,io,json,sys
from pathlib import Path
assert sys.platform=='emscripten',sys.platform
browser_manifest=json.loads(browser_manifest_json)
browser_site=Path(importlib.metadata.distribution('easysewer').locate_file(''))
browser_module_paths=[browser_site/name for name in browser_manifest['files']]
assert len(browser_module_paths)==browser_manifest['module_count']
for name,entry in browser_manifest['files'].items():
 assert hashlib.sha256((browser_site/name).read_bytes()).hexdigest()==entry['sha256'],name
sys.argv=['browser-qualification',str(browser_site)]
browser_output=io.StringIO()
with contextlib.redirect_stdout(browser_output):
 import importlib.abc,json,sys,subprocess
 from pathlib import Path
 from datetime import datetime,date,timedelta
 import platform
 platform.system()  # Host-only discovery may invoke Windows ver on Python3.10.
 class NoNative(importlib.abc.MetaPathFinder):
  def find_spec(self,name,path=None,target=None):
   if name=='ctypes' or name.startswith('easysewer.runtime._process'):raise AssertionError(name)
 sys.meta_path.insert(0,NoNative());sys.path.insert(0,sys.argv[1])
 def reject(*a,**k):raise AssertionError('process launch')
 subprocess.Popen=reject
 from easysewer.model import Model
 from easysewer.model.events import RoutingEvent
 from easysewer.io.inp import InpDocument
 from easysewer.scenario import ScenarioPatch
 from easysewer.model import events
 m=Model();m.update_options(start_date=date(2020,1,1),end_date=date(2020,1,2));m.update_events(periods=(RoutingEvent(start=datetime(2020,1,1),end=datetime(2020,1,2)),))
 from easysewer.model.network import Junction
 from easysewer.model.project import ObjectTag
 from easysewer.model import Ref
 m.nodes.add(Junction(id='J',elevation=0))
 m.tags.add(ObjectTag(target=Ref(collection='swmm:nodes',key='J'),text='pure tag'))
 from easysewer.model.project import MapLabel,ProfilePlot
 from easysewer.model import Point
 from easysewer.model.network import Conduit
 from easysewer.model.geometry import CrossSection,Circular
 m.nodes.add(Junction(id='K',elevation=0))
 m.links.add(Conduit(id='P',inlet=Ref(collection='swmm:nodes',key='J'),outlet=Ref(collection='swmm:nodes',key='K'),length=10,roughness=.01,section=CrossSection(geometry=Circular(diameter=1))))
 m.update_labels(entries=(MapLabel(position=Point(x=1,y=2),text='pure label'),))
 m.profiles.add(ProfilePlot(name='pure profile',links=(Ref(collection='swmm:links',key='P'),)))
 m.update_map(units='FEET')
 m.update_backdrop(units='METERS',clear_file=True,legacy_offset=Point(x=1,y=-2),legacy_scaling=Point(x=0,y=2))
 m.update_map(units_precedence='BACKDROP')
 from easysewer.model.network import Storage,FunctionalStorage
 m.nodes.replace('J',Storage(id='J',elevation=0,max_depth=5,initial_depth=0,shape=FunctionalStorage(coefficient=0,exponent=1,constant=100),polygon=(Point(x=1,y=2),Point(x=3,y=4))))
 m.links.update('P',vertices=(Point(x=8,y=9),))
 n=Model.from_document(m.to_document(),strict=True)
 r=Model.from_json_document(n.to_json_document(),strict=True)
 assert r.effective_map==n.effective_map==m.effective_map
 assert r.backdrop==n.backdrop==m.backdrop
 assert r.map==n.map==m.map
 assert r.nodes['J'].polygon==n.nodes['J'].polygon==m.nodes['J'].polygon
 assert r.links['P'].vertices==n.links['P'].vertices==m.links['P'].vertices
 assert r.events==n.events==m.events
 assert r.labels==n.labels==m.labels
 assert tuple(r.profiles.items())==tuple(n.profiles.items())==tuple(m.profiles.items())
 assert tuple(r.tags.items())==tuple(n.tags.items())==tuple(m.tags.items())
 assert r.validate().is_valid and 'swmm:events.period' in r.json_schema()['$defs']
 p=n.copy();p.nodes.rename('J','Upstream')
 z=Model.from_json_document(p.to_json_document(),strict=True)
 assert z.provenance(Ref(collection='swmm:nodes',key='Upstream')).original==Ref(collection='swmm:nodes',key='J')
 assert z.provenance(Ref(collection='swmm:nodes',key='Upstream')).kind=='inp'
 owner=Ref(collection='swmm:options',key='settings')
 info=z.inspect_field(owner,'rule_step')
 assert info.provenance.status=='omitted' and info.value is None
 assert info.semantics.effective.value==timedelta() and info.semantics.unit.value=='s'
 assert z.field_provenance(owner,'start_date').declarations[0].tokens[0].raw=='01/01/2020'
 map_owner=Ref(collection='swmm:map',key='settings')
 image_owner=Ref(collection='swmm:backdrop',key='image')
 label_owner=Ref(collection='swmm:labels',key='layer')
 priority=z.field_provenance(map_owner,'units_precedence')
 assert priority.status=='derived'
 assert priority.declarations[-1].source_owners==(image_owner.canonical,)
 assert z.inspect_field(label_owner,('entries',0,'position','x')).semantics.unit.value=='m'
 assert z.inspect_field(label_owner,('entries',0,'font_size')).semantics.unit.value=='pt'
 assert z.field_provenance(label_owner,('entries',0,'font_size')).status=='explicit'
 assert z.inspect_field(image_owner,('legacy_offset','x')).semantics.effective.status=='not_applicable'
 pipe=Ref(collection='swmm:links',key='P')
 node=Ref(collection='swmm:nodes',key='K')
 assert z.inspect_field(node,'max_depth').semantics.effective.value==1
 assert z.inspect_field(pipe,'inlet_offset').semantics.effective.value==0
 assert z.inspect_field(pipe,('vertices',0,'x')).semantics.unit.value=='m'
 assert z.field_provenance(pipe,('inlet','key')).status=='explicit'
 assert z.inspect_field(pipe,'losses').semantics.effective.value.flap_gate is False
 from easysewer.model.geometry import Arch,CrossSection
 z.links.update('P',section=CrossSection(geometry=Arch(size_code=3)))
 assert abs(z.inspect_field(pipe,('section','geometry','full_depth')).semantics.effective.value-15.5/12)<1e-12
 assert z.inspect_field(pipe,('section','barrels')).semantics.effective.value==1
 z=Model.from_document(z.to_document(),strict=True)
 assert z.field_provenance(pipe,('section','geometry','size_code')).status=='explicit'
 from easysewer.model.network import Outlet,FunctionalRating
 old=z.links['P']
 z.links.replace('P',Outlet(id='P',inlet=old.inlet,outlet=old.outlet,offset=.5,rating=FunctionalRating(basis='HEAD',coefficient=2,exponent=1.4)))
 z=Model.from_document(z.to_document(),strict=True)
 assert z.inspect_field(pipe,'offset').semantics.effective.value==.5
 assert z.inspect_field(pipe,('rating','coefficient')).semantics.unit.value=='CFS/ft^1.4'
 assert z.field_provenance(pipe,('rating','basis')).status=='explicit'
 y=Model.from_json_document(z.to_json_document(),strict=True)
 assert y.inspect_field(pipe,('rating','coefficient'))==z.inspect_field(pipe,('rating','coefficient'))
 storage=Ref(collection='swmm:nodes',key='Upstream')
 assert z.inspect_field(storage,'max_depth').semantics.effective.value==5
 assert z.inspect_field(storage,'evaporation_fraction').semantics.effective.value==0
 assert z.inspect_field(storage,('polygon',0,'x')).semantics.unit.value=='m'
 assert z.field_provenance(storage,('shape','coefficient')).status=='explicit'
 from easysewer.model.network import Outfall,FixedBoundary
 z.nodes.add(Outfall(id='Out',elevation=0,boundary=FixedBoundary(stage=2)))
 z=Model.from_document(z.to_document(),strict=True)
 out=Ref(collection='swmm:nodes',key='Out')
 assert z.inspect_field(out,('boundary','stage')).semantics.effective.value==2
 assert z.inspect_field(out,'gated').semantics.effective.value is False
 assert z.field_provenance(out,('boundary','stage')).status=='explicit'
 from easysewer.model.resources import Pattern,Curve,CurvePoint
 z.patterns.add(Pattern(id='Daily',kind='DAILY',factors=(0,-2)))
 z.curves.add(Curve(id='Area',kind='STORAGE',points=(CurvePoint(x=0,y=10),CurvePoint(x=2,y=20))))
 z=Model.from_document(z.to_document(),strict=True)
 pattern=Ref(collection='swmm:patterns',key='Daily');curve=Ref(collection='swmm:curves',key='Area')
 assert z.inspect_field(pattern,('factors',0)).semantics.effective.value==0
 assert z.inspect_field(pattern,'factors').semantics.effective.value==(0,-2,1,1,1,1,1)
 assert z.inspect_field(curve,('points',1,'y')).semantics.unit.value=='ft2'
 assert z.field_provenance(curve,('points',1,'y')).declarations[0].tokens[0].raw=='20'
 from easysewer.model import hydrology as h
 from easysewer.model.resources import InlineTimeSeries,SeriesPoint
 z.timeseries.add(InlineTimeSeries(id='Rain',points=(SeriesPoint(time=timedelta(),value=0),)))
 z.raingages.add(h.RainGage(id='R',form='INTENSITY',interval=timedelta(hours=1),snow_factor=1,source=h.SeriesRainfall(series=Ref(collection='swmm:timeseries',key='Rain'))))
 z.subcatchments.add(h.Subcatchment(id='S',rain_gage=Ref(collection='swmm:raingages',key='R'),outlet=Ref(collection='swmm:nodes',key='K'),area=1,impervious_percent=120,width=1,slope=1,curb_length=0,subareas=h.Subareas(impervious_roughness=.01,pervious_roughness=.1,impervious_storage=0,pervious_storage=0,zero_storage_percent=25,route_to='PERVIOUS'),infiltration=h.Infiltration(parameters=h.Horton(maximum_rate=3,minimum_rate=.2,decay=4,drying_time=0))))
 z=Model.from_document(z.to_document(),strict=True)
 gage=Ref(collection='swmm:raingages',key='R');catchment=Ref(collection='swmm:subcatchments',key='S')
 assert z.inspect_field(gage,'interval').semantics.unit.value=='s'
 assert z.inspect_field(catchment,'impervious_percent').semantics.effective.value==100
 assert z.inspect_field(catchment,('subareas','route_to')).semantics.effective.value=='OUTLET'
 assert z.inspect_field(catchment,('infiltration','parameters','maximum_volume')).semantics.effective.value==0
 assert z.inspect_field(catchment,('infiltration','parameters','drying_time')).semantics.effective.value==1e-6
 assert z.field_provenance(catchment,('infiltration','parameters','maximum_volume')).status=='omitted'
 from easysewer.model import climate as c
 z.snowpacks.add(h.Snowpack(id='Snow',pervious=h.DepletableSnow(minimum_melt=.001,maximum_melt=.004,base_temperature=32,free_water_fraction=.1,initial_snow=3,initial_free_water=.8,full_cover_depth=4)))
 z.subcatchments.update('S',snowpack=Ref(collection='swmm:snowpacks',key='Snow'))
 z.patterns.add(Pattern(id='Monthly',kind='MONTHLY',factors=(.5,2)))
 z.subcatchment_adjustments.add(c.SubcatchmentAdjustments(subcatchment=catchment,infiltration=Ref(collection='swmm:patterns',key='Monthly')))
 z=Model.from_document(z.to_document(),strict=True)
 snow=Ref(collection='swmm:snowpacks',key='Snow');adjust=Ref(collection='swmm:subcatchment_adjustments',key='S')
 assert z.inspect_field(snow,'plowable').semantics.effective.value.base_temperature==0
 assert abs(z.inspect_field(snow,('pervious','initial_free_water')).semantics.effective.value-.3)<1e-12
 assert z.inspect_field(adjust,'infiltration').semantics.effective.value.key=='Monthly'
 assert z.field_provenance(adjust,('subcatchment','key')).declarations[0].tokens[0].raw=='S'
 z.update_climate(adjustments=c.ClimateAdjustments(conductivity=c.MonthlyFactors(values=(0.,-1.,*(2.,)*10))))
 z=Model.from_document(z.to_document(),strict=True)
 climate=Ref(collection='swmm:climate',key='settings')
 assert z.inspect_field(climate,('adjustments','conductivity','values',1)).semantics.effective.value==1
 assert z.inspect_field(climate,'temperature').semantics.default.value.value==70
 assert z.field_provenance(climate,('adjustments','conductivity','values',1)).declarations[0].tokens[0].raw=='-1.0'
 from easysewer.model import quality as q, inflows as inf
 z.pollutants.add(q.Pollutant(id='Q',units='MG/L',rainfall_concentration=0,groundwater_concentration=0,rdii_concentration=0,decay_rate=-.1))
 z.inflows.add(inf.FlowInflow(node=Ref(collection='swmm:nodes',key='K')))
 z.inflows.add(inf.ConcentrationInflow(node=Ref(collection='swmm:nodes',key='K'),constituent=Ref(collection='swmm:pollutants',key='Q'),baseline=2))
 z.dwf.add(inf.DryWeatherFlow(node=Ref(collection='swmm:nodes',key='K'),baseline=.1,patterns=(Ref(collection='swmm:patterns',key='Monthly'),None)))
 z=Model.from_document(z.to_document(),strict=True)
 flow=Ref(collection='swmm:inflows',key=('K','FLOW'));dwf=Ref(collection='swmm:dwf',key=('K','FLOW'))
 assert z.inspect_field(flow,'baseline').semantics.effective.value==0
 assert z.inspect_field(flow,'scale_factor').semantics.effective.status=='not_applicable'
 assert z.inspect_field(dwf,('patterns',1)).semantics.effective.status=='not_applicable'
 assert z.field_provenance(flow,('node','key')).declarations[0].tokens[0].raw=='K'
 z.landuses.add(q.LandUse(id='Land'))
 z.coverages.add(q.Coverage(subcatchment=catchment,landuse=Ref(collection='swmm:landuses',key='Land'),percent=100))
 z.buildup.add(q.Buildup(landuse=Ref(collection='swmm:landuses',key='Land'),pollutant=Ref(collection='swmm:pollutants',key='Q'),function=q.SaturationBuildup(maximum=10,half_saturation_days=3,unused_parameter=.8)))
 z.washoff.add(q.Washoff(landuse=Ref(collection='swmm:landuses',key='Land'),pollutant=Ref(collection='swmm:pollutants',key='Q'),function=q.EventMeanConcentration(concentration=3,unused_exponent=.7)))
 z=Model.from_document(z.to_document(),strict=True)
 build=Ref(collection='swmm:buildup',key=('Land','Q'));wash=Ref(collection='swmm:washoff',key=('Land','Q'))
 assert z.inspect_field(build,('function','half_saturation_days')).semantics.effective.value==3
 assert z.inspect_field(build,('function','unused_parameter')).semantics.effective.status=='not_applicable'
 assert z.inspect_field(wash,'bmp_removal').semantics.effective.value==0
 assert z.inspect_field(wash,('function','unused_exponent')).semantics.effective.status=='not_applicable'
 assert z.field_provenance(build,('function','half_saturation_days')).declarations[0].tokens[0].raw=='3'
 from easysewer.model.rdii import UnitHydrograph,HydrographResponse,RdiiInflow
 z.hydrographs.add(UnitHydrograph(id='UH',rain_gage=gage,responses=(HydrographResponse(month='ALL',response='SHORT',fraction=.1,time_to_peak=.00055,recession_ratio=.99),)))
 z.rdii.add(RdiiInflow(node=Ref(collection='swmm:nodes',key='K'),hydrograph=Ref(collection='swmm:hydrographs',key='UH'),sewer_area=2))
 z=Model.from_document(z.to_document(),strict=True)
 uh=Ref(collection='swmm:hydrographs',key='UH');rdii=Ref(collection='swmm:rdii',key='K')
 assert z.inspect_field(uh,('responses',0,'maximum_abstraction')).semantics.effective.value==0
 assert '1/3 seconds' in z.inspect_field(uh,('responses',0,'time_to_peak')).semantics.effective.reason
 assert z.field_provenance(uh,('responses',0,'response')).declarations[0].tokens[0].raw=='SHORT'
 assert z.inspect_field(rdii,'sewer_area').semantics.unit.value=='acre'
 from easysewer.model import groundwater as gw
 from easysewer.io.inp.groundwater import GroundwaterExpressionCodec
 z.aquifers.add(gw.Aquifer(id='Aquifer',porosity=.45,wilting_point=.1,field_capacity=.25,conductivity=.2,conductivity_slope=10,tension_slope=15,upper_evaporation_fraction=.5,lower_evaporation_depth=5,deep_seepage=.001,bottom_elevation=-10,water_table_elevation=2,upper_moisture=.3))
 z.groundwater.add(gw.Groundwater(subcatchment=catchment,aquifer=Ref(collection='swmm:aquifers',key='Aquifer'),node=Ref(collection='swmm:nodes',key='K'),surface_elevation=20,groundwater_coefficient=.001,groundwater_exponent=1.2,surface_water_coefficient=.0001,surface_water_exponent=1.1,interaction_coefficient=.00001,fixed_surface_depth=0))
 z.gwf.add(gw.GroundwaterExpression(subcatchment=catchment,kind='LATERAL',expression=GroundwaterExpressionCodec().parse('.001 * HGW')))
 z=Model.from_document(z.to_document(),strict=True)
 groundwater=Ref(collection='swmm:groundwater',key='S');gwf=Ref(collection='swmm:gwf',key=('S','LATERAL'))
 assert z.inspect_field(groundwater,'bottom_elevation').semantics.effective.value==-10
 assert z.inspect_field(gwf,('expression','left','value')).semantics.unit.status=='unknown'
 assert z.inspect_field(gwf,('expression','right','name')).semantics.unit.value=='ft'
 assert z.field_provenance(gwf,('expression','right','name')).status=='derived'
 y=Model.from_json_document(z.to_json_document(),strict=True)
 assert z.inspect_field(groundwater,'bottom_elevation')==y.inspect_field(groundwater,'bottom_elevation')
 from easysewer.model import lid as lid
 z.lid_controls.add(lid.LidControl(id='L',kind='RB',storage=lid.LidStorage(thickness=12,void_ratio=.5,seepage_rate=.1,clogging_factor=10),drain=lid.LidDrain(coefficient=.2,exponent=.5,offset=1,delay=2)))
 z.lid_usage.add(lid.LidUsage(record_id='lid-usage-1',subcatchment=catchment,control=Ref(collection='swmm:lid_controls',key='L'),number=2,area=100,width=10,initial_saturation=50,from_impervious=20,to_pervious=True))
 z=Model.from_document(z.to_document(),strict=True)
 lid_control=Ref(collection='swmm:lid_controls',key='L');lid_usage=Ref(collection='swmm:lid_usage',key='lid-usage-1')
 assert z.inspect_field(lid_control,('storage','covered')).semantics.effective.value is False
 assert z.inspect_field(lid_control,('storage','void_ratio')).semantics.effective.value==.5
 assert z.inspect_field(lid_usage,'to_pervious').semantics.effective.value is False
 assert z.inspect_field(lid_usage,'from_pervious').semantics.effective.value==0
 assert z.field_provenance(lid_control,('storage','covered')).status=='omitted'
 assert z.field_provenance(lid_usage,'record_id').status=='derived'
 y=Model.from_json_document(z.to_json_document(),strict=True)
 assert z.inspect_field(lid_control,('storage','covered'))==y.inspect_field(lid_control,('storage','covered'))
 p=Model.from_document(InpDocument.from_text('[TRANSECTS]\nNC 0 0 .02\nX1 A 3 0 10 0 0 4 1 0\nGR 2 0 0 5 2 10\nNC 0 0 0\nX1 B 3 0 10 0 0 0 0 0\nGR 2 0 0 5 2 10\n[REPORT]\n[STREETS]\nRoad 10 .5 2 .016\n[INLETS]\nI GRATE 2 1 GENERIC .7\n'),strict=True)
 assert p.inspect_field(Ref(collection='swmm:transects',key='B'),('roughness','channel')).semantics.effective.value==.04
 assert p.inspect_field(Ref(collection='swmm:streets',key='Road'),'sides').semantics.effective.value==2
 assert p.inspect_field(Ref(collection='swmm:inlets',key='I'),('design','grate','splash_velocity')).semantics.effective.value==0
 q=Model.from_json_document(p.to_json_document(),strict=True)
 assert q.field_provenance(Ref(collection='swmm:transects',key='B'),('roughness','channel'))==p.field_provenance(Ref(collection='swmm:transects',key='B'),('roughness','channel'))
 p=Model.from_document(InpDocument.from_text(z.to_document().text+'[CONTROLS]\nVARIABLE DepthValue = NODE K DEPTH\nEXPRESSION E = (DepthValue + DepthValue) / 2\nRULE R\nIF E > 0\nTHEN OUTLET P SETTING = PID .1 1 .05\n'),strict=True)
 control=Ref(collection='swmm:controls',key=('RULE','R'));expression=Ref(collection='swmm:controls',key=('EXPRESSION','E'))
 assert p.inspect_field(control,'priority').semantics.effective.value==0
 assert p.inspect_field(control,('then_actions',0,'setting','integral_time')).semantics.effective.value==timedelta(minutes=1)
 assert p.inspect_field(control,('then_actions',0,'setting','integral_time')).semantics.unit.value=='s'
 assert p.field_provenance(expression,('expression','left','left','reference','key',1)).status=='derived'
 q=Model.from_json_document(p.to_json_document(),strict=True)
 assert q.inspect_field(control,'priority')==p.inspect_field(control,'priority')
 p=Model.from_document(InpDocument.from_text(z.to_document().text+'[TREATMENT]\nK Q R = .1 + .001 * DT + .001 * HRT\n'),strict=True)
 treatment=Ref(collection='swmm:treatment',key=('K','Q'))
 assert p.inspect_field(treatment,'expression').semantics.unit.value=='1'
 assert p.inspect_field(treatment,('expression','right','right','name')).semantics.unit.value=='hour'
 assert p.inspect_field(treatment,('expression','left','right','right','name')).semantics.unit.value=='s'
 assert p.field_provenance(treatment,('expression','left','right','right','name')).declarations[0].tokens[0].value=='DT'
 q=Model.from_json_document(p.to_json_document(),strict=True)
 assert q.inspect_field(treatment,'expression')==p.inspect_field(treatment,'expression')
 p=Model.from_document(InpDocument.from_text(z.to_document().text+'[TITLE]\nRaw title\n\n; comment\n[TAGS]\nNode K note\n[PROFILES]\nProfile P P\n'),strict=True)
 p.set_annotation('test:note',{'value':'informational'})
 p=Model.from_document(p.to_document(),strict=True)
 title=Ref(collection='swmm:title',key='text');annotation=Ref(collection='easysewer:metadata',key='test:note')
 assert p.inspect_field(title,'lines').semantics.unit.status=='not_applicable'
 assert p.field_provenance(title,('lines',1)).declarations[0].raw_text==''
 assert p.field_provenance(title,('lines',1)).declarations[0].tokens==()
 assert p.field_provenance(annotation,'json_text').status=='derived'
 q=Model.from_json_document(p.to_json_document(),strict=True)
 assert q.field_provenance(title,'lines')==p.field_provenance(title,'lines')
 assert q.inspect_field(annotation,'json_text')==p.inspect_field(annotation,'json_text')
 p=Model.from_document(InpDocument.from_text('[OPTIONS]\nSTART_DATE 01/01/2020\nSTART_TIME 25:00\nEND_DATE 01/03/2020\n[REPORT]\nINPUT YES\n[FILES]\nSAVE HOTSTART state.bin\n[EVENTS]\n01/02/2020 01:00 01/02/2020 02:00\n'),strict=True)
 assert p.inspect_field(Ref(collection='swmm:options',key='settings'),('start_time','day_offset')).semantics.effective.value==1
 assert p.inspect_field(Ref(collection='swmm:report',key='settings'),'continuity').semantics.effective.value is True
 assert p.field_provenance(Ref(collection='swmm:files',key=('HOTSTART','SAVE')),('file','direction')).status=='derived'
 assert p.field_provenance(Ref(collection='swmm:events',key='schedule'),('periods',0,'start')).status=='derived'
 q=Model.from_json_document(p.to_json_document(),strict=True)
 assert q.inspect_field(Ref(collection='swmm:report',key='settings'),'input')==p.inspect_field(Ref(collection='swmm:report',key='settings'),'input')
 p=Model.from_document(InpDocument.from_text('[OPTIONS]\nSTART_DATE 01/01/2020\nEND_DATE 01/03/2020\nSWEEP_START 03/01\nSWEEP_END 11/30\nTEMPDIR "scratch folder"\n'),strict=True)
 owner=Ref(collection='swmm:options',key='settings')
 paths=[('sweep_start','month'),('sweep_start','day'),('sweep_end','month'),('sweep_end','day'),*[('temp_directory',n) for n in ('path','base_directory','flavor','direction')]]
 facts=[p.inspect_field(owner,path) for path in paths]
 assert all(f.semantics.effective.status=='known' for f in facts)
 assert all(f.provenance.status in ('explicit','derived') for f in facts)
 q=Model.from_json_document(p.to_json_document(),strict=True)
 assert [q.inspect_field(owner,path) for path in paths]==facts
 from easysewer.validation import DiagnosticSubject
 from easysewer.runtime._result_codec import Codec
 text=chr(10).join(['[OPTIONS]','FLOW_ROUTING KINWAVE','START_DATE 01/01/2020','END_DATE 01/02/2020','[JUNCTIONS]','J 10 8 2','[OUTFALLS]','O 9 FREE','[ORIFICES]','P J O SIDE .2 .65','[XSECTIONS]','P CIRCULAR 1 0 0 0',''])
 p=Model.from_document(InpDocument.from_text(text,source='/original/model.inp'),strict=True)
 d=next(d for d in p.validate(for_run=True).errors if d.code=='regulator.requires_storage')
 assert d.subject==DiagnosticSubject(collection='swmm:links',key='P',path=('inlet','key'))
 assert d.span.line==10 and len(d.locations)==3
 assert Codec(None).decode(Codec(None).encode(d))==d
 q=Model.from_json_document(p.to_json_document())
 assert q.validate(for_run=True)==p.validate(for_run=True)
 p=Model.from_document(InpDocument.from_text(chr(10).join(['[OPTIONS]','START_DATE 01/01/2020','END_DATE 01/02/2020','[EVAPORATION]','CONSTANT -2',''])))
 d=next(d for d in p.validate(for_run=True).errors if d.code=='climate.negative_evaporation')
 assert d.subject==DiagnosticSubject(collection='swmm:climate',key='settings',path=('evaporation','source','rate'))
 assert d.span.line==5 and d.locations[0].status=='current'
 assert Codec(None).decode(Codec(None).encode(d))==d
 from easysewer.model.controls import ControlVariable,Attribute
 p=Model();p.nodes.add(Junction(id='J',elevation=0));p.controls.add(ControlVariable(id='LongVariable'*4,value=Attribute(object_type='NODE',attribute='DEPTH',target=Ref(collection='swmm:nodes',key='J'))))
 d=next(d for d in p.validate().errors if d.code=='control.symbol_length')
 assert d.subject==DiagnosticSubject(collection='swmm:controls',key=('VARIABLE','LongVariable'*4),path=('id',))
 assert Codec(None).decode(Codec(None).encode(d))==d
 from easysewer.validation import ValidationError
 p=Model.from_document(InpDocument.from_text(chr(10).join(['[OPTIONS]','FLOW_UNITS CFS','ROUTING_STEP 5','START_DATE 01/01/2020','END_DATE 01/02/2020','']),source='pure-boundary.inp'),strict=True)
 before=p.to_json_document().to_bytes()
 try:p.update_options(routing_step=timedelta(seconds=-1))
 except ValidationError as error:
  d=error.report.errors[0]
  assert d.subject==DiagnosticSubject(collection='swmm:options',key='settings',path=('routing_step',))
  assert d.locations[0].status=='changed' and d.span is None and d.locations[0].spans
  assert Codec(None).decode(Codec(None).encode(d))==d
 else:raise AssertionError('draft accepted')
 assert p.to_json_document().to_bytes()==before
 legacy=Codec(None,result_version='1.1');low=p.effective_options
 assert legacy.decode(legacy.encode(low))==low
 p=Model.from_document(InpDocument.from_text(chr(10).join(['[JUNCTIONS]','J 0','[FILES]','USE HOTSTART missing.hsf','']),source=str(Path('pure-files.inp').absolute())),strict=True)
 try:p.nodes.remove('J')
 except ValidationError as error:
  d=next(d for d in error.report.errors if d.code=='files.external_identity_dependency')
  assert d.subject==DiagnosticSubject(collection='swmm:files',key=('HOTSTART','USE'),path=('file','path'))
  assert d.locations[0].status=='current' and d.span.source==str(Path('pure-files.inp').absolute())
  assert Codec(None).decode(Codec(None).encode(d))==d
 else:raise AssertionError('external identity guard bypassed')
 from easysewer.io.json import JsonDocument
 p=Model.from_document(InpDocument.from_text(chr(10).join(['[JUNCTIONS]','J 0','[COORDINATES]','J 1 2 ignored','']),source='pure-parser.inp'))
 d=next(d for d in p.validate().diagnostics if d.code=='geometry.ignored_columns')
 assert d.subject==DiagnosticSubject(collection='swmm:nodes',key='J') and d.span.line==4
 payload=p.to_json_document().data
 for block in payload['collections']:
  if block['collection']=='swmm:nodes':block['records'][0]['value']['elevation']=True
 try:Model.from_json_document(JsonDocument.from_data(payload,source='pure-parser.json'))
 except ValidationError as error:
  d=error.report.errors[0]
  assert d.code=='json.field_type' and d.span.source=='pure-parser.json' and d.span.line>1
  assert d.subject==DiagnosticSubject(collection='swmm:nodes',key='J',path=('elevation',))
  assert Codec(None).decode(Codec(None).encode(d))==d
 else:raise AssertionError('invalid JSON accepted')
 p=Model.from_document(InpDocument.from_text(chr(10).join(['[OPTIONS]','NORMAL_FLOW_LIMITED NONE',''])),strict=True)
 assert p.options.normal_flow_limited=='NONE' and p.effective_options.values.normal_flow_limited=='NONE'
 q=Model.from_json_document(p.to_json_document(),strict=True)
 assert q.options.normal_flow_limited=='NONE'
 q.update_options(normal_flow_limited=None)
 assert q.effective_options.values.normal_flow_limited=='BOTH'
 assert 'NORMAL_FLOW_LIMITED' not in q.to_document().text
 assert q.copy(checkpoint=lambda:None).to_json_document().to_bytes()==q.to_json_document().to_bytes()
 assert q.validate(checkpoint=lambda:None)==q.validate()
 assert q.to_json_document(checkpoint=lambda:None).to_bytes()==q.to_json_document().to_bytes()
 doc=q.to_document(checkpoint=lambda:None)
 assert doc.to_bytes()==q.to_document().to_bytes()
 assert InpDocument.from_bytes(doc.to_bytes(),checkpoint=lambda:None).to_bytes()==doc.to_bytes()
 assert doc.apply(doc.patch(()),checkpoint=lambda:None).to_bytes()==doc.to_bytes()
 from io import BytesIO
 import struct
 from easysewer.io.output_metadata import OutputMetadata
 from easysewer.io._record_work import record_dumps
 from easysewer.validation._cooperative import checkpoint_scope
 header=struct.pack('<7i',516114522,52004,3,0,0,0,0)
 body=struct.pack('<3i',0,0,0)+struct.pack('<2i',1,0)*4+struct.pack('<di',43831.,60)
 raw=header+body+struct.pack('<6i',28,28,len(header+body),0,0,516114522)
 stream=BytesIO(raw);metadata=OutputMetadata.from_stream(stream,checkpoint=lambda:None)
 assert not stream.closed and metadata.periods==0 and metadata.flow_units=='CMS'
 assert OutputMetadata.from_stream(BytesIO(raw))==metadata
 with checkpoint_scope(lambda:None):
  assert record_dumps({'rows':list(range(600))})==json.dumps({'rows':list(range(600))},ensure_ascii=False,allow_nan=False,indent=2)
 from easysewer.runtime import DirectoryAdapter,DirectoryLimits,DirectoryEntry,DirectoryManifest,Runner
 from easysewer.io.interface_inspection import InterfaceInspection
 from easysewer.runtime._result_codec import Codec
 manifest=DirectoryManifest(entries=(DirectoryEntry(path='empty',kind='directory'),))
 codec=Codec(None,result_version='1.4');assert codec.decode(codec.encode(manifest))==manifest
 adapter=DirectoryAdapter(inspector=lambda *a,**k:InterfaceInspection(format=k['use'].format,status='validated'))
 Runner(directory_adapters={'example:tree':adapter})
 # Import every included module while rejecting ctypes and worker startup.
 import importlib
 for path in browser_module_paths:
  parts=path.relative_to(sys.argv[1]).with_suffix('').parts
  name='.'.join(parts[:-1] if parts[-1]=='__init__' else parts)
  importlib.import_module(name)
 package=Path(sys.argv[1]).resolve()
 for name,module in tuple(sys.modules.items()):
  if name.startswith('easysewer') and getattr(module,'__file__',None):assert Path(module.__file__).resolve().is_relative_to(package)
 import easysewer
 from easysewer import Model, NativeCapabilityError
 from easysewer.model import Model as TypedModel
 from easysewer.runtime._solver_api import SWMMSolverAPI,FlexiblePondingSolverAPI
 from easysewer.runtime._output_api import SWMMOutputAPI
 assert Model is TypedModel
 assert not hasattr(easysewer,'LegacyModel') and not hasattr(easysewer,'UrbanDrainageModel')
 assert easysewer.get_native_capabilities()=={'swmm_solver':False,'swmm_output':False,'flexible_ponding':False}
 for constructor in (SWMMSolverAPI,FlexiblePondingSolverAPI,SWMMOutputAPI):
  try:constructor()
  except NativeCapabilityError as error:assert 'pure Python build' in str(error)
  else:raise AssertionError('Native constructor accepted in pure wheel')
 assert 'ctypes' not in sys.modules
 print(json.dumps(dict(native_loaded=False,event_periods=len(r.events.periods),module=events.__file__,model_v2=True,native_capabilities_false=True,stub_constructors_refuse=True,all_modules_imported=len(browser_module_paths))))
browser_probe_result=json.loads(browser_output.getvalue().splitlines()[-1])
browser_probe_result.update(python=sys.version,platform=sys.platform,wheel_sha256=browser_manifest["sha256"],installed_module_bytes_verified=len(browser_module_paths))
assert importlib.util.find_spec('easysewer.compat') is None
browser_probe_result['v1_compatibility_absent'] = True
json.dumps(browser_probe_result)
