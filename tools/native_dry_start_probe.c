/* Read-only diagnostic ABI; never shipped as a solver capability. */
#include "headers.h"
#include "include/swmm5.h"
#include <string.h>
extern TRoutingTotals FlowTotals;
extern double massbal_getStorage(char isFinalStorage);

/* Native units: ft, ft2, ft3, cfs; clock is seconds. */
double DLLEXPORT easysewer_probe(int group, int index, int field)
{
    if (group == 0) {
        switch (field) {
        case 0: return NewRoutingTime / 1000.0;
        case 1: return FlowTotals.initStorage;
        case 2: return FlowTotals.dwInflow + FlowTotals.wwInflow +
            FlowTotals.gwInflow + FlowTotals.iiInflow + FlowTotals.exInflow;
        case 3: return FlowTotals.outflow + FlowTotals.flooding +
            FlowTotals.evapLoss + FlowTotals.seepLoss + FlowTotals.reacted;
        case 4: return massbal_getStorage(0);
        }
    }
    if (group == 1 && index >= 0 && index < Nobjects[NODE]) {
        switch (field) {
        case 0: return Node[index].newVolume;
        case 1: return Node[index].newDepth;
        case 2: return Node[index].inflow;
        case 3: return Node[index].outflow;
        }
    }
    if (group == 2 && index >= 0 && index < Nobjects[LINK]) {
        int k = Link[index].subIndex;
        switch (field) {
        case 0: return Link[index].newVolume;
        case 1: return Link[index].newDepth;
        case 2: return Link[index].newFlow;
        case 3: return Conduit[k].a1;
        case 4: return Conduit[k].a2;
        case 5: return Conduit[k].q1;
        case 6: return Conduit[k].q2;
        case 7: return Link[index].surfArea1;
        case 8: return Link[index].surfArea2;
        case 9: return Link[index].flowClass;
        }
    }
    return -1.0;
}

/* Isolated circular geometry: local structure only, no solver state writes.
   Fixed 3 ft diameter. field 0/2/3/4 report constants; field 1 evaluates S(A).
   These exports are diagnostic helpers, not production ABI capabilities. */
double DLLEXPORT easysewer_probe_circle(int field, double area_fraction)
{
    TXsect section;
    double params[4] = {3.0, 0.0, 0.0, 0.0};
    if (!(area_fraction >= 0.0 && area_fraction <= 1.0)) return -1.0;
    memset(&section, 0, sizeof(section));
    if (!xsect_setParams(&section, CIRCULAR, params, 1.0)) return -1.0;
    switch (field) {
    case 0: return section.aFull;
    case 1: return xsect_getSofA(&section, area_fraction*section.aFull);
    case 2: return section.sFull;
    case 3: return section.sMax;
    case 4: return xsect_getAmax(&section);
    }
    return -1.0;
}
