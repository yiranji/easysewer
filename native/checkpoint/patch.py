"""Shared exact-source checkpoint ABI 2 and six numerical corrections.

Call only after pinned base preparation. Both production families and the
historical development recipe use this one transformation.
"""
from pathlib import Path

ABI = 2
FIXES = dict(event_rule_clock_fix=1, inactive_routing_clock_fix=1,
             paraboloid_exfil_fix=1, inlet_sides_fix=1,
             groundwater_conductivity_fix=1, snow_initialization_fix=1)
RECIPE_FILES = ("patch.py", "codec.h", "codec.c", "controls.inc", "engine.inc", "routing.inc", "dynwave.inc", "network.inc", "inlet.inc", "infiltration.inc", "subcatchment.inc", "gage.inc", "runoff.inc", "climate.inc", "climate_tables.inc", "tables.inc", "streams.inc", "rdii.inc", "iface.inc", "groundwater.inc", "snow.inc", "lid.inc", "massbal.inc", "stats.inc", "output.inc", "ponding.inc", "writes.c", "write_owners.inc", "lid_writes.inc", "report_boundary.inc", "coordinator.inc", "api.h")


def patch(contents, *, custom):
    if type(custom) is not bool:raise TypeError('custom must be a boolean')
    if 'src/solver/es_checkpoint.c' in contents or b'#define ES_CK_CUSTOM' in contents['src/solver/swmm5.c']:
        raise ValueError('Checkpoint source has already been applied')
    root = Path(__file__).resolve().parent
    controls = "src/solver/controls.c"
    contents[controls] += b"\n" + (root / "controls.inc").read_bytes()
    routing = "src/solver/routing.c"
    anchor = b"if ( date1 < Event[NextEvent].start ) return fixedStep;"
    if contents[routing].count(anchor) != 1:
        raise ValueError("Unexpected event routing-step source")
    # An early return here skips the common RULE_STEP cap,
    # misses a control boundary and eventually produces a negative step.
    # Keep the chosen fixed step, then pass through the existing cap below.
    contents[routing] = contents[routing].replace(
        anchor, b"if ( date1 < Event[NextEvent].start ) routingStep = fixedStep;")
    contents[routing] += b"\n" + (root / "routing.inc").read_bytes()
    contents["src/solver/dynwave.c"] += b"\n" + (root / "dynwave.inc").read_bytes()
    inlet = "src/solver/inlet.c"
    before = b"    totalInlets = Nsides * inlet->numInlets;\n"
    after = (b"    getConduitGeometry(inlet);\n\n"
             b"    // --- set flow limit per inlet")
    if contents[inlet].count(before) != 1 or contents[inlet].count(after) != 1:
        raise ValueError("Unexpected on-sag inlet geometry source")
    # Select this inlet's street sides before multiplying its inlet count.
    # Otherwise the preceding inlet (or validation order) controls capture.
    contents[inlet] = contents[inlet].replace(before, b"").replace(
        after, b"    getConduitGeometry(inlet);\n" + before +
        b"\n    // --- set flow limit per inlet")
    contents[inlet] += b"\n" + (root / "inlet.inc").read_bytes()
    contents["src/solver/infil.c"] += b"\n" + (root / "infiltration.inc").read_bytes()
    contents["src/solver/subcatch.c"] += b"\n" + (root / "subcatchment.inc").read_bytes()
    contents["src/solver/gage.c"] += b"\n" + (root / "gage.inc").read_bytes()
    contents["src/solver/runoff.c"] += b"\n" + (root / "runoff.inc").read_bytes()
    contents["src/solver/massbal.c"] += b"\n" + (root / "massbal.inc").read_bytes()
    contents["src/solver/stats.c"] += b"\n" + (root / "stats.inc").read_bytes()
    contents["src/solver/output.c"] += b"\n" + (root / "output.inc").read_bytes()
    contents["src/solver/report.c"] += b"\n" + (root / "report_boundary.inc").read_bytes()
    contents["src/solver/climate.c"] += b"\n" + (root / "climate.inc").read_bytes()
    contents["src/solver/climate.c"] += b"\n" + (root / "climate_tables.inc").read_bytes()
    contents["src/solver/table.c"] += b"\n" + (root / "tables.inc").read_bytes()
    contents["src/solver/rdii.c"] += b"\n" + (root / "rdii.inc").read_bytes()
    contents["src/solver/iface.c"] += b"\n" + (root / "iface.inc").read_bytes()
    groundwater = "src/solver/gwater.c"
    before = (b"    // --- no perc. from upper zone if no depth or moisture content too low    \n"
              b"    if ( upperDepth <= 0.0 || theta <= A.fieldCapacity ) return 0.0;\n\n"
              b"    // --- compute hyd. conductivity as function of moisture content\n"
              b"    delta = theta - A.porosity;\n"
              b"    hydcon = A.conductivity * exp(delta * A.conductSlope);")
    after = (b"    // --- K is current unsaturated conductivity even when Fu is zero.\n"
             b"    //     Update before the percolation gate; never reuse another\n"
             b"    //     subcatchment, ODE evaluation or project's value.\n"
             b"    delta = theta - A.porosity;\n"
             b"    hydcon = A.conductivity * exp(delta * A.conductSlope);\n"
             b"    HydCon = hydcon;\n\n"
             b"    if ( upperDepth <= 0.0 || theta <= A.fieldCapacity ) return 0.0;")
    if contents[groundwater].count(before) != 1:
        raise ValueError("Unexpected groundwater conductivity source")
    contents[groundwater] = contents[groundwater].replace(before, after)
    contents[groundwater] += b"\n" + (root / "groundwater.inc").read_bytes()
    snow = "src/solver/snow.c"
    before = b"        snowpack->awe[i]   = 1.0;"
    after = (before + b"\n        snowpack->sba[i]   = 0.0;\n"
             b"        snowpack->sbws[i]  = 0.0;\n"
             b"        snowpack->imelt[i] = 0.0;")
    if contents[snow].count(before) != 1:
        raise ValueError("Unexpected snowpack initialization source")
    contents[snow] = contents[snow].replace(before, after)
    before = (b"            snowpack->wsnow[i] += snowfall * tStep;\n"
              b"            snowpack->imelt[i] = 0.0;\n        }")
    after = (b"            snowpack->wsnow[i] += snowfall * tStep;\n        }\n"
             b"        // Zero-area surfaces also enter net-precipitation arithmetic.\n"
             b"        // Reset every immediate-melt slot before it is consumed.\n"
             b"        snowpack->imelt[i] = 0.0;")
    if contents[snow].count(before) != 1:
        raise ValueError("Unexpected immediate snowmelt reset source")
    contents[snow] = contents[snow].replace(before, after)
    contents[snow] += b"\n" + (root / "snow.inc").read_bytes()
    contents["src/solver/lid.c"] += b"\n" + (root / "lid.inc").read_bytes()
    contents["src/solver/lid.c"] += b"\n" + (root / "lid_writes.inc").read_bytes()
    engine = "src/solver/swmm5.c"
    # A prior project's OldRoutingTime must not enter disabled-routing output
    # interpolation. Advancing both ends also gives it a valid current bracket.
    for before, after in (
        (b"NewRoutingTime = 0.0;", b"OldRoutingTime = NewRoutingTime = 0.0;"),
        (b"            NewRoutingTime = nextRoutingTime;",
         b"        {\n            OldRoutingTime = NewRoutingTime;\n"
         b"            NewRoutingTime = nextRoutingTime;\n        }"),
    ):
        if contents[engine].count(before) != 1:
            raise ValueError("Unexpected engine routing clock source")
        contents[engine] = contents[engine].replace(before, after)
    if custom:
        contents["src/solver/easysewer_ponding.c"] += b"\n" + (root / "ponding.inc").read_bytes()
    contents["src/solver/flowrout.c"] += (f"\n#define ES_CK_CUSTOM {int(custom)}\n".encode()
                                         + (root / "network.inc").read_bytes())
    exfil = "src/solver/exfil.c"
    before = b"            case CYLINDRICAL:\n            case CONICAL:"
    if contents[exfil].count(before) != 1:
        raise ValueError("Unexpected analytical storage exfiltration source")
    contents[exfil] = contents[exfil].replace(before, before + b"\n            case PARABOLOID:")
    contents["src/solver/swmm5.c"] += (f"\n#define ES_CK_CUSTOM {int(custom)}\n".encode()
                                      + (root / "engine.inc").read_bytes())
    contents["src/solver/swmm5.c"] += b"\n" + (root / "streams.inc").read_bytes()
    contents["src/solver/swmm5.c"] += b"\n" + (root / "write_owners.inc").read_bytes()
    contents["src/solver/swmm5.c"] += b"\n" + (root / "coordinator.inc").read_bytes()
    contents["src/solver/es_checkpoint_api.h"] = (root / "api.h").read_bytes()
    contents["src/solver/es_checkpoint.h"] = (root / "codec.h").read_bytes()
    contents["src/solver/es_checkpoint.c"] = (root / "codec.c").read_bytes()
    contents["src/solver/es_checkpoint_writes.c"] = (root / "writes.c").read_bytes()
