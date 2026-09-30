/* Test-only instrumentation appended to subcatch.c. */
#include "swmm5.h"
void DLLEXPORT es_test_subcatchment_poison(int group)
{
    int j, i, k;
    TSubcatch *p;
    if (group == 0 || group == 4)
    {
        theSubarea = NULL; Dstore = Alpha = -777;
        Vevap = Vpevap = Vinfil = Vinflow = Voutflow = VlidIn = -888;
        VlidInfil = VlidOut = VlidDrain = VlidReturn = -999;
    }
    for (j = 0; j < Nobjects[SUBCATCH]; j++)
    {
        p = &Subcatch[j];
        if (group == 0 || group == 1)
        {
            for (i = IMPERV0; i <= PERV; i++)
                p->subArea[i].inflow = p->subArea[i].runoff = p->subArea[i].depth = 0;
            p->rainfall = p->evapLoss = p->infilLoss = p->runon = 0;
            p->oldRunoff = p->newRunoff = p->oldSnowDepth = p->newSnowDepth = 0;
        }
        if (group == 0 || group == 2)
            for (k = 0; k < Nobjects[POLLUT]; k++)
                p->oldQual[k] = p->newQual[k] = p->pondedQual[k] = p->totalLoad[k] = 0;
        for (i = 0; i < Nobjects[LANDUSE]; i++)
        {
            if (group == 0 || group == 3)
                for (k = 0; k < Nobjects[POLLUT]; k++) p->landFactor[i].buildup[k] = 0;
            if (group == 0 || group == 5) p->landFactor[i].lastSwept = StartDateTime;
        }
    }
}

double DLLEXPORT es_test_subcatchment_observe(int j, int group, int k)
{
    TSubcatch *p;
    if (j < 0 || j >= Nobjects[SUBCATCH]) return -1;
    p = &Subcatch[j];
    if (group == 0 && k >= 0 && k < 3) return p->subArea[k].depth;
    if (group == 1) return p->newRunoff;
    if (group == 2 && k >= 0 && k < Nobjects[POLLUT]) return p->totalLoad[k];
    if (group == 3 && k >= 0 && k < Nobjects[LANDUSE]) return p->landFactor[k].lastSwept;
    if (group == 4 && k >= 0 && k < Nobjects[LANDUSE] && Nobjects[POLLUT])
        return p->landFactor[k].buildup[0];
    return -2;
}
