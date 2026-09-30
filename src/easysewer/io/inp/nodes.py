"""Storage and divider positional syntax, separate from graph ownership."""

from ...model import network as n
from ...model.identity import Ref
from .formatting import optional_tail
from .geometry import UnsupportedGeometry, finite_number, number_text


_STORAGE = {
    "FUNCTIONAL": (n.FunctionalStorage, ("coefficient", "exponent", "constant")),
    "CYLINDRICAL": (n.CylindricalStorage, ("major_axis", "minor_axis")),
    "CONICAL": (n.ConicalStorage, ("base_major_axis", "base_minor_axis", "side_slope")),
    "PARABOLOID": (n.ParaboloidStorage, ("top_major_axis", "top_minor_axis", "full_height")),
    "PYRAMIDAL": (n.PyramidalStorage, ("base_length", "base_width", "side_slope")),
}


def parse_storage(values):
    if len(values) < 6:
        raise ValueError("Storage requires ID, elevation, full/initial depths and a shape")
    kind = values[4].upper()
    ignored = []
    if kind == "PARABOLIC":
        kind = "PARABOLOID"
    elif kind == "PARABOLOID":
        ignored.append(("storage.native_shape_spelling", "The manual's PARABOLOID spelling is not accepted by SWMM 5.2.4; "
                        "normalize to its native PARABOLIC keyword before running"))
    if kind == "TABULAR":
        shape = n.TabularStorage(curve=Ref(collection="swmm:curves", key=values[5]))
        index = 6
    else:
        if kind not in _STORAGE:
            raise UnsupportedGeometry(f"Unsupported storage shape: {kind}")
        if len(values) < 8:
            raise ValueError("Analytical storage requires three shape parameters")
        record_type, fields = _STORAGE[kind]
        numbers = tuple(finite_number(token) for token in values[5:8])
        if kind == "CYLINDRICAL":
            if numbers[2] < 0:
                raise ValueError("Native cylindrical placeholder must be nonnegative")
            if numbers[2] != 0:
                ignored.append(("storage.ignored_shape_parameter", "CYLINDRICAL's third shape parameter is ignored by native SWMM"))
        shape = record_type(**dict(zip(fields, numbers)))
        index = 8
    tail = tuple(finite_number(token) for token in values[index:])
    if len(tail) not in (0, 1, 2, 3, 5):
        raise ValueError("Storage accepts surcharge, evaporation, then either Ksat alone or Psi/Ksat/IMD")
    seepage = None
    if len(tail) == 3:
        seepage = n.ConstantSeepage(conductivity=tail[2])
    elif len(tail) == 5:
        seepage = n.Seepage(suction=tail[2], conductivity=tail[3], initial_deficit=tail[4])
    return n.Storage(id=values[0], elevation=finite_number(values[1]), max_depth=finite_number(values[2]),
                     initial_depth=finite_number(values[3]), shape=shape,
                     surcharge_depth=tail[0] if tail else None,
                     evaporation_fraction=tail[1] if len(tail) > 1 else None, seepage=seepage), ignored


def format_storage(record):
    shape = record.shape
    if type(shape) is n.TabularStorage:
        parameters = (shape.curve.key,)
    else:
        spec = _STORAGE.get(shape.kind)
        if spec is None or type(shape) is not spec[0]:
            raise ValueError(f"No INP writer for {type(shape).__name__}")
        parameters = tuple(number_text(getattr(shape, field)) for field in spec[1])
        if type(shape) is n.CylindricalStorage:
            parameters += ("0",)
    tail = [number_text(value) if value is not None else None
            for value in (record.surcharge_depth, record.evaporation_fraction)]
    if type(record.seepage) is n.ConstantSeepage:
        tail.append(number_text(record.seepage.conductivity))
    elif type(record.seepage) is n.Seepage:
        tail.extend(number_text(getattr(record.seepage, field)) for field in ("suction", "conductivity", "initial_deficit"))
    elif record.seepage is not None:
        raise ValueError(f"No INP writer for {type(record.seepage).__name__}")
    return (number_text(record.elevation), number_text(record.max_depth), number_text(record.initial_depth),
            "PARABOLIC" if type(shape) is n.ParaboloidStorage else shape.kind, *parameters, *optional_tail(tail))


def parse_divider(values):
    if len(values) < 4:
        raise ValueError("Divider requires ID, elevation, diverted link and law")
    kind = values[3].upper()
    if kind == "OVERFLOW":
        law, index = n.OverflowDivider(), 4
    elif kind in ("CUTOFF", "TABULAR"):
        if len(values) < 5:
            raise ValueError(f"Missing {kind} divider parameter")
        law = (n.CutoffDivider(cutoff_flow=finite_number(values[4])) if kind == "CUTOFF"
               else n.TabularDivider(curve=Ref(collection="swmm:curves", key=values[4])))
        index = 5
    elif kind == "WEIR":
        if len(values) < 7:
            raise ValueError("Weir divider requires minimum flow, height and coefficient")
        law = n.WeirDivider(minimum_flow=finite_number(values[4]), height=finite_number(values[5]), coefficient=finite_number(values[6]))
        index = 7
    else:
        raise UnsupportedGeometry(f"Unsupported divider law: {kind}")
    if len(values) > index + 4:
        raise ValueError("Extra divider fields")
    return n.Divider(id=values[0], elevation=finite_number(values[1]),
                     diverted_link=None if values[2] == "*" else Ref(collection="swmm:links", key=values[2]), law=law,
                     **dict(zip(("max_depth", "initial_depth", "surcharge_depth", "ponded_area"),
                                (finite_number(token) for token in values[index:]))))


def format_divider(record):
    law = record.law
    if type(law) is n.OverflowDivider:
        parameters = ()
    elif type(law) is n.CutoffDivider:
        parameters = (number_text(law.cutoff_flow),)
    elif type(law) is n.TabularDivider:
        parameters = (law.curve.key,)
    elif type(law) is n.WeirDivider:
        parameters = tuple(number_text(getattr(law, field)) for field in ("minimum_flow", "height", "coefficient"))
    else:
        raise ValueError(f"No INP writer for {type(law).__name__}")
    tail = tuple(number_text(value) if value is not None else None
                 for value in (record.max_depth, record.initial_depth, record.surcharge_depth, record.ponded_area))
    return (number_text(record.elevation), record.diverted_link.key if record.diverted_link else "*",
            law.kind, *parameters, *optional_tail(tail))


def node_field_layout(record, values):
    """Declared columns for variable-length node forms, including inactive slots."""
    coverage,assignments=[],[]

    def add(path,index,role=None,contributes=True):
        coverage.append(path)
        if index<len(values):
            assignments.append((path,(index,),role or ('marker' if values[index]=='*' else 'value'),contributes))

    def ref(path,index,value):
        add(path,index)
        if value is not None:
            add((*path,'key'),index)
            add((*path,'collection'),index,'derived')

    def aggregate(path,indexes):
        coverage.append(path)
        indexes=tuple(i for i in indexes if i<len(values))
        if indexes:assignments.append((path,indexes,'value',True))

    add(('id',),0);add(('elevation',),1)
    if type(record) is n.Storage:
        add(('max_depth',),2);add(('initial_depth',),3)
        if type(record.shape) is n.TabularStorage:
            ref(('shape','curve'),5,record.shape.curve)
            aggregate(('shape',),range(4,6));end=6
        else:
            names=_STORAGE[record.shape.kind][1]
            for index,name in enumerate(names,5):add(('shape',name),index)
            aggregate(('shape',),range(4,5+len(names)));end=8
            if type(record.shape) is n.CylindricalStorage:
                add(('shape',),7,'retained',False)
        add(('surcharge_depth',),end);add(('evaporation_fraction',),end+1)
        aggregate(('seepage',),range(end+2,len(values)))
        if type(record.seepage) is n.ConstantSeepage:
            add(('seepage','conductivity'),end+2)
        elif type(record.seepage) is n.Seepage:
            for index,name in enumerate(('suction','conductivity','initial_deficit'),end+2):
                add(('seepage',name),index)
    elif type(record) is n.Divider:
        ref(('diverted_link',),2,record.diverted_link)
        if type(record.law) is n.OverflowDivider:end=4
        elif type(record.law) is n.CutoffDivider:
            add(('law','cutoff_flow'),4);end=5
        elif type(record.law) is n.TabularDivider:
            ref(('law','curve'),4,record.law.curve);end=5
        else:
            for index,name in enumerate(('minimum_flow','height','coefficient'),4):add(('law',name),index)
            end=7
        aggregate(('law',),range(3,end))
        for index,name in enumerate(('max_depth','initial_depth','surcharge_depth','ponded_area'),end):
            add((name,),index)
    elif type(record) is n.Outfall:
        end=3
        if type(record.boundary) is n.FixedBoundary:
            add(('boundary','stage'),3);end=4
        elif type(record.boundary) is n.TidalBoundary:
            ref(('boundary','curve'),3,record.boundary.curve);end=4
        elif type(record.boundary) is n.SeriesBoundary:
            ref(('boundary','series'),3,record.boundary.series);end=4
        aggregate(('boundary',),range(2,end))
        add(('gated',),end);ref(('route_to',),end+1,record.route_to)
    else:
        raise TypeError('No field grammar declared for this node type')
    return tuple(coverage),tuple(assignments)
