#include "swmm5.h"

void DLLEXPORT es_test_groundwater_poison(int group)
{
    int i;
    TGroundwater *g;
    if (!group)
    {
        Area = Infil = MaxEvap = AvailEvap = UpperEvap = LowerEvap = NAN;
        UpperPerc = LowerLoss = GWFlow = MaxUpperPerc = MaxGWFlowPos = MaxGWFlowNeg = NAN;
        FracPerv = TotalDepth = Theta = HydCon = Hgw = Hstar = Hsw = Tstep = NAN;
        memset(&A, 0, sizeof(A)); GW = NULL; LatFlowExpr = DeepFlowExpr = NULL;
        return;
    }
    for (i = 0; i < Nobjects[SUBCATCH]; i++)
    {
        g = Subcatch[i].groundwater; if (!g) continue;
        if (group == 1)
        {
            g->theta = g->lowerDepth = g->oldFlow = g->newFlow = g->evapLoss = NAN;
            if (subcatch_getFracPerv(i) > 0.0) g->maxInfilVol = NAN;
        }
        else if (group == 2)
        {
            g->stats.infil = g->stats.evap = g->stats.latFlow = g->stats.deepFlow = NAN;
            g->stats.avgUpperMoist = g->stats.finalUpperMoist = NAN;
            g->stats.avgWaterTable = g->stats.finalWaterTable = g->stats.maxFlow = NAN;
        }
    }
}

int DLLEXPORT es_test_groundwater_observe(int index, double *values)
{
    TGroundwater *g;
    if (index < 0 || index >= Nobjects[SUBCATCH]) return 0;
    g = Subcatch[index].groundwater; if (!g) return 0;
    values[0] = g->theta; values[1] = g->lowerDepth;
    values[2] = g->oldFlow; values[3] = g->newFlow; values[4] = g->evapLoss;
    values[5] = subcatch_getFracPerv(index) > 0.0 ? g->maxInfilVol : 0.0;
    values[6] = g->stats.infil; values[7] = g->stats.evap;
    values[8] = g->stats.latFlow; values[9] = g->stats.deepFlow;
    values[10] = g->stats.avgUpperMoist; values[11] = g->stats.finalUpperMoist;
    values[12] = g->stats.avgWaterTable; values[13] = g->stats.finalWaterTable;
    values[14] = g->stats.maxFlow;
    return 15;
}

double DLLEXPORT es_test_groundwater_percolation(double theta, double depth,
    double porosity, double conductivity, double slope, double capacity,
    double tension, double *current_conductivity)
{
    TAquifer saved = A;
    double previous = HydCon, flow;
    A.porosity = porosity; A.conductivity = conductivity;
    A.conductSlope = slope; A.fieldCapacity = capacity; A.tensionSlope = tension;
    flow = getUpperPerc(theta, depth);
    *current_conductivity = HydCon;
    A = saved; HydCon = previous;
    return flow;
}

void es_test_groundwater_walk(EsCkCursor *c) { es_ck_groundwater(c); }

void DLLEXPORT es_test_groundwater_reset(int group)
{
    int i;
    for (i = 0; i < Nobjects[SUBCATCH]; i++) if (Subcatch[i].groundwater)
    {
        if (group == 1) gwater_initState(i);
        else if (group == 2) memset(&Subcatch[i].groundwater->stats, 0, sizeof(TGWaterStats));
    }
}
