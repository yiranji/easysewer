/* Appended to stats.c. Groundwater stats have a separate owner. */
#include "es_checkpoint.h"
#include <limits.h>
static void es_ck_TSubcatchStats(EsCkCursor *c, TSubcatchStats *p)
{
    int i; (void)i;
    es_ck_f64(c, &p->precip);
    es_ck_f64(c, &p->runon);
    es_ck_f64(c, &p->evap);
    es_ck_f64(c, &p->infil);
    es_ck_f64(c, &p->runoff);
    es_ck_f64(c, &p->maxFlow);
    es_ck_f64(c, &p->impervRunoff);
    es_ck_f64(c, &p->pervRunoff);
}
static void es_ck_TNodeStats(EsCkCursor *c, TNodeStats *p)
{
    int i; (void)i;
    es_ck_f64(c, &p->avgDepth);
    es_ck_f64(c, &p->maxDepth);
    es_ck_f64(c, &p->maxDepthDate);
    es_ck_f64(c, &p->maxRptDepth);
    es_ck_f64(c, &p->volFlooded);
    es_ck_f64(c, &p->timeFlooded);
    es_ck_f64(c, &p->timeSurcharged);
    es_ck_f64(c, &p->timeCourantCritical);
    es_ck_f64(c, &p->totLatFlow);
    es_ck_f64(c, &p->maxLatFlow);
    es_ck_f64(c, &p->maxInflow);
    es_ck_f64(c, &p->maxOverflow);
    es_ck_f64(c, &p->maxPondedVol);
    es_ck_i32(c, &p->nonConvergedCount, 0, INT_MAX);
    es_ck_f64(c, &p->maxInflowDate);
    es_ck_f64(c, &p->maxOverflowDate);
}
static void es_ck_TLinkStats(EsCkCursor *c, TLinkStats *p)
{
    int i; (void)i;
    es_ck_f64(c, &p->maxFlow);
    es_ck_f64(c, &p->maxFlowDate);
    es_ck_f64(c, &p->maxVeloc);
    es_ck_f64(c, &p->maxDepth);
    es_ck_f64(c, &p->maxStreetFilled);
    es_ck_f64(c, &p->timeNormalFlow);
    es_ck_f64(c, &p->timeInletControl);
    es_ck_f64(c, &p->timeSurcharged);
    es_ck_f64(c, &p->timeFullUpstream);
    es_ck_f64(c, &p->timeFullDnstream);
    es_ck_f64(c, &p->timeFullFlow);
    es_ck_f64(c, &p->timeCapacityLimited);
    for (i=0; i<MAX_FLOW_CLASSES; i++) es_ck_f64(c, &p->timeInFlowClass[i]);
    es_ck_f64(c, &p->timeCourantCritical);
    es_ck_count(c, &p->flowTurns);
    es_ck_i32(c, &p->flowTurnSign, -1, 1);
}
static void es_ck_TStorageStats(EsCkCursor *c, TStorageStats *p)
{
    int i; (void)i;
    es_ck_f64(c, &p->initVol);
    es_ck_f64(c, &p->avgVol);
    es_ck_f64(c, &p->maxVol);
    es_ck_f64(c, &p->maxFlow);
    es_ck_f64(c, &p->evapLosses);
    es_ck_f64(c, &p->exfilLosses);
    es_ck_f64(c, &p->maxVolDate);
}
static void es_ck_TOutfallStats(EsCkCursor *c, TOutfallStats *p)
{
    int i; (void)i;
    es_ck_f64(c, &p->avgFlow);
    es_ck_f64(c, &p->maxFlow);
    if (Nobjects[POLLUT] && !p->totalLoad) { es_ck_fail(c, ES_CK_STATE); return; }
    for (i=0; i<Nobjects[POLLUT]; i++) es_ck_f64(c, &p->totalLoad[i]);
    es_ck_i32(c, &p->totalPeriods, 0, INT_MAX);
}
static void es_ck_TPumpStats(EsCkCursor *c, TPumpStats *p)
{
    int i; (void)i;
    es_ck_f64(c, &p->utilized);
    es_ck_f64(c, &p->minFlow);
    es_ck_f64(c, &p->avgFlow);
    es_ck_f64(c, &p->maxFlow);
    es_ck_f64(c, &p->volume);
    es_ck_f64(c, &p->energy);
    es_ck_f64(c, &p->offCurveLow);
    es_ck_f64(c, &p->offCurveHigh);
    es_ck_i32(c, &p->startUps, 0, INT_MAX);
    es_ck_i32(c, &p->totalPeriods, 0, INT_MAX);
}
void es_ck_stats(EsCkCursor *c)
{
    int j, k;
    if (c->error) return;
    es_ck_tag(c, "ESSTAT01", 8);
    es_ck_fixed_i32(c, Nobjects[SUBCATCH]);
    es_ck_fixed_i32(c, Nobjects[NODE]);
    es_ck_fixed_i32(c, Nobjects[LINK]);
    es_ck_fixed_i32(c, Nobjects[POLLUT]);
    es_ck_fixed_i32(c, Nnodes[STORAGE]);
    es_ck_fixed_i32(c, Nnodes[OUTFALL]);
    es_ck_fixed_i32(c, Nlinks[PUMP]);
    es_ck_fixed_i32(c, TIMELEVELS);
    es_ck_fixed_i32(c, MAX_FLOW_CLASSES);
    es_ck_fixed_i32(c, UnitSystem);
    es_ck_fixed_i32(c, FlowUnits);
    es_ck_fixed_i32(c, RouteModel);
    es_ck_fixed_i32(c, RptFlags.disabled);
    es_ck_fixed_i32(c, RptFlags.flowStats);
    es_ck_fixed_f64(c, ReportStart);
    for (j=0; j<Nobjects[POLLUT]; j++) es_ck_identity(c, Pollut[j].ID);
    es_ck_fixed_i32(c, SubcatchStats != NULL);
    es_ck_fixed_i32(c, NodeStats != NULL);
    es_ck_fixed_i32(c, LinkStats != NULL);
    es_ck_fixed_i32(c, StorageStats != NULL);
    es_ck_fixed_i32(c, OutfallStats != NULL);
    es_ck_fixed_i32(c, PumpStats != NULL);
    for (j=0; j<Nobjects[SUBCATCH] && !c->error; j++)
    {
        es_ck_identity(c, Subcatch[j].ID);
        if (SubcatchStats) es_ck_TSubcatchStats(c, &SubcatchStats[j]);
    }
    for (j=0; j<Nobjects[NODE] && !c->error; j++)
    {
        es_ck_identity(c, Node[j].ID);
        es_ck_fixed_i32(c, Node[j].type);
        if (NodeStats) es_ck_TNodeStats(c, &NodeStats[j]);
        if (Node[j].type == STORAGE)
        {
            k = Node[j].subIndex;
            if (k < 0 || k >= Nnodes[STORAGE] || !StorageStats)
            { es_ck_fail(c, ES_CK_STATE); return; }
            es_ck_fixed_i32(c, k);
            es_ck_TStorageStats(c, &StorageStats[k]);
        }
        if (Node[j].type == OUTFALL)
        {
            k = Node[j].subIndex;
            if (k < 0 || k >= Nnodes[OUTFALL] || !OutfallStats)
            { es_ck_fail(c, ES_CK_STATE); return; }
            es_ck_fixed_i32(c, k);
            es_ck_TOutfallStats(c, &OutfallStats[k]);
        }
    }
    for (j=0; j<Nobjects[LINK] && !c->error; j++)
    {
        es_ck_identity(c, Link[j].ID);
        es_ck_fixed_i32(c, Link[j].type);
        if (LinkStats) es_ck_TLinkStats(c, &LinkStats[j]);
        if (Link[j].type == PUMP)
        {
            k = Link[j].subIndex;
            if (k < 0 || k >= Nlinks[PUMP] || !PumpStats)
            { es_ck_fail(c, ES_CK_STATE); return; }
            es_ck_fixed_i32(c, k);
            es_ck_TPumpStats(c, &PumpStats[k]);
        }
    }
    es_ck_f64(c, &TimeStepStats.minTimeStep);
    es_ck_f64(c, &TimeStepStats.maxTimeStep);
    es_ck_f64(c, &TimeStepStats.routingTime);
    es_ck_i32(c, &TimeStepStats.timeStepCount, 0, INT_MAX);
    es_ck_f64(c, &TimeStepStats.trialsCount);
    es_ck_f64(c, &TimeStepStats.steadyStateTime);
    for (j=0; j<TIMELEVELS; j++) es_ck_fixed_f64(c, TimeStepStats.timeStepIntervals[j]);
    for (j=1; j<TIMELEVELS; j++) es_ck_i32(c, &TimeStepStats.timeStepCounts[j], 0, INT_MAX);
    es_ck_f64(c, &MaxOutfallFlow);
    es_ck_f64(c, &MaxRunoffFlow);
    es_ck_f64(c, &RoutingTimeSpan);
    /* Histogram slot 0 is unused. Ranking arrays are rebuilt before report;
       SysOutfallFlow is overwritten before use in each active stats update. */
}
