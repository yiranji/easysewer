/* Test-only instrumentation appended to inlet.c. */
#include "swmm5.h"
void DLLEXPORT es_test_inlet_poison(int group)
{
    TInlet *p;
    int i;
    if (group == 0 || group == 1)
        for (p = FirstInlet; p; p = p->nextInlet)
        {
            p->flowCapture = -123.0; p->backflow = 456.0;
            memset(&p->stats, 0, sizeof(p->stats));
        }
    if (group == 0 || group == 2)
    {
        Sx = SL = Sw = a = W = T = n = Tcrown = Beta = Qfactor = -9876.0;
        Nsides = -88; xsect = NULL;
        if (InletFlow)
            for (i = 0; i < Nobjects[NODE]; i++) InletFlow[i] = -6543.0;
    }
}

double DLLEXPORT es_test_inlet_observe(int index, int field)
{
    TInlet *p = FirstInlet;
    while (p && index-- > 0) p = p->nextInlet;
    if (!p) return -1.0;
    switch (field)
    {
        case 0: return p->flowCapture;
        case 1: return p->backflow;
        case 2: return p->stats.flowPeriods;
        case 3: return p->stats.capturePeriods;
        case 4: return p->stats.backflowPeriods;
        case 5: return p->stats.peakFlow;
        case 6: return p->stats.peakFlowCapture;
        case 7: return p->stats.avgFlowCapture;
        case 8: return p->stats.bypassFreq;
        case 9: return p->backflowRatio;
    }
    return -2.0;
}
