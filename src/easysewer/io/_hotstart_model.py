"""Conservative adapter from owned Model records to native hotstart layout."""

from ..model.identity import canonical_key
from ..model.network import Junction, Outfall, Storage, Divider, Conduit, Pump, Orifice, Weir, Outlet
from .hotstart import HotstartLayout, StateObject, CatchmentLayout, StatePollutant
from ..model.quality import Pollutant, LandUse
from ..model.groundwater import Groundwater
from ..validation._cooperative import checkpointed


class LayoutUnavailable(ValueError):
    """The current Model cannot establish all state layout dependencies."""


_NODES = {'JUNCTIONS': Junction, 'OUTFALLS': Outfall, 'STORAGE': Storage, 'DIVIDERS': Divider}
_LINKS = {'CONDUITS': Conduit, 'PUMPS': Pump, 'ORIFICES': Orifice, 'WEIRS': Weir, 'OUTLETS': Outlet}
_KINDS = {'STORAGE': 'STORAGE', **{key: key[:-1] for key in checkpointed((*_NODES, *_LINKS)) if key != 'STORAGE'}}


def model_layout(model, *, normalize=False):
    if type(normalize) is not bool:
        raise TypeError('normalize must be boolean')
    if model.profile.engine_version != '5.2.4':
        raise LayoutUnavailable('Automatic hotstart layout currently requires the fixed 5.2.4 profile')
    if model._store.opaque_constraints:
        raise LayoutUnavailable('Unstructured Model records may change state layout; supply an independently established layout to HotstartData')
    if any(type(row) is not Groundwater for row in checkpointed(model.groundwater.values())):
        raise LayoutUnavailable('Groundwater variant needs its own state adapter')
    document = model.to_document(normalize=normalize)
    nodes, links, catchments, pollutants, landuses = [], [], [], [], []
    groundwater_ids=set()
    for line in checkpointed(document.lines):
        if line.kind not in ('data', 'raw'):
            continue
        section = line.section
        if section == 'GROUNDWATER':
            if not line.values or line.values[0] not in model.groundwater:
                raise LayoutUnavailable('Exported groundwater has no owned state binding')
            groundwater_ids.add(canonical_key(line.values[0]))
        if section == 'POLLUTANTS':
            if not line.values or line.values[0] not in model.pollutants or type(model.pollutants[line.values[0]]) is not Pollutant:
                raise LayoutUnavailable('Exported pollutant has no owned state identity')
            pollutants.append(StatePollutant(id=line.values[0],units=model.pollutants[line.values[0]].units))
        elif section == 'LANDUSES':
            if not line.values or line.values[0] not in model.landuses or type(model.landuses[line.values[0]]) is not LandUse:
                raise LayoutUnavailable('Exported land use has no owned state identity')
            landuses.append(line.values[0])
        elif section in _NODES or section in _LINKS:
            collection, expected, target = (model.nodes, _NODES[section], nodes) if section in _NODES else (model.links, _LINKS[section], links)
            if not line.values or line.values[0] not in collection or type(collection[line.values[0]]) is not expected:
                raise LayoutUnavailable('Exported object does not match the owned native state type')
            target.append(StateObject(id=line.values[0], kind=_KINDS[section]))
        elif section == 'SUBCATCHMENTS':
            if not line.values or line.values[0] not in model.subcatchments:
                raise LayoutUnavailable('Exported catchment has no owned state description')
            row = model.subcatchments[line.values[0]]
            if row.infiltration is None:
                raise LayoutUnavailable('Missing catchment infiltration parameters prevent deriving its state method')
            method = row.infiltration.method or model.options.infiltration or model.profile.option_default('infiltration')
            groundwater=model.groundwater[line.values[0]] if line.values[0] in model.groundwater else None
            catchments.append(CatchmentLayout(id=line.values[0], infiltration=method,
                groundwater=groundwater.aquifer.key if groundwater else None, snowpack=row.snowpack.key if row.snowpack else None))
    for actual, collection in checkpointed(((nodes, model.nodes), (links, model.links), (catchments, model.subcatchments))):
        if len(actual) != len(collection) or {canonical_key(v.id) for v in checkpointed(actual)} != {canonical_key(v) for v in checkpointed(collection)}:
            raise LayoutUnavailable('Exported object identities do not exactly cover the Model collection')
    if groundwater_ids!={canonical_key(key) for key in checkpointed(model.groundwater)}:
        raise LayoutUnavailable('Exported groundwater bindings do not exactly cover the Model collection')
    for actual, collection in checkpointed(((tuple(p.id for p in checkpointed(pollutants)),model.pollutants),(tuple(landuses),model.landuses))):
        if len(actual)!=len(collection) or {canonical_key(v) for v in checkpointed(actual)}!={canonical_key(v) for v in checkpointed(collection)}:
            raise LayoutUnavailable('Exported quality identities do not exactly cover the Model collection')
    return HotstartLayout(flow_units=model.units.flow_units, nodes=tuple(nodes), links=tuple(links), subcatchments=tuple(catchments),pollutants=tuple(pollutants),landuses=tuple(landuses))
