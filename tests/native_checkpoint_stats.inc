/* Test-only statistics damage injection. */
#include "swmm5.h"
void DLLEXPORT es_test_stats_change(int group, int poison)
{
    int j, i; double v = poison ? NAN : 0.0; int n = poison ? -7 : 0;
    if ((!group || group == 1) && SubcatchStats) for (j=0; j<Nobjects[SUBCATCH]; j++)
    {
        SubcatchStats[j].precip = v;
        SubcatchStats[j].runon = v;
        SubcatchStats[j].evap = v;
        SubcatchStats[j].infil = v;
        SubcatchStats[j].runoff = v;
        SubcatchStats[j].maxFlow = v;
        SubcatchStats[j].impervRunoff = v;
        SubcatchStats[j].pervRunoff = v;
    }
    if ((!group || group == 2) && NodeStats) for (j=0; j<Nobjects[NODE]; j++)
    {
        NodeStats[j].avgDepth = v;
        NodeStats[j].maxDepth = v;
        NodeStats[j].maxDepthDate = v;
        NodeStats[j].maxRptDepth = v;
        NodeStats[j].volFlooded = v;
        NodeStats[j].timeFlooded = v;
        NodeStats[j].timeSurcharged = v;
        NodeStats[j].timeCourantCritical = v;
        NodeStats[j].totLatFlow = v;
        NodeStats[j].maxLatFlow = v;
        NodeStats[j].maxInflow = v;
        NodeStats[j].maxOverflow = v;
        NodeStats[j].maxPondedVol = v;
        NodeStats[j].nonConvergedCount = n;
        NodeStats[j].maxInflowDate = v;
        NodeStats[j].maxOverflowDate = v;
    }
    if ((!group || group == 3) && LinkStats) for (j=0; j<Nobjects[LINK]; j++)
    {
        LinkStats[j].maxFlow = v;
        LinkStats[j].maxFlowDate = v;
        LinkStats[j].maxVeloc = v;
        LinkStats[j].maxDepth = v;
        LinkStats[j].maxStreetFilled = v;
        LinkStats[j].timeNormalFlow = v;
        LinkStats[j].timeInletControl = v;
        LinkStats[j].timeSurcharged = v;
        LinkStats[j].timeFullUpstream = v;
        LinkStats[j].timeFullDnstream = v;
        LinkStats[j].timeFullFlow = v;
        LinkStats[j].timeCapacityLimited = v;
        for (i=0; i<MAX_FLOW_CLASSES; i++) LinkStats[j].timeInFlowClass[i] = v;
        LinkStats[j].timeCourantCritical = v;
        LinkStats[j].flowTurns = n;
        LinkStats[j].flowTurnSign = n;
    }
    if ((!group || group == 4) && StorageStats) for (j=0; j<Nnodes[STORAGE]; j++)
    {
        StorageStats[j].initVol = v;
        StorageStats[j].avgVol = v;
        StorageStats[j].maxVol = v;
        StorageStats[j].maxFlow = v;
        StorageStats[j].evapLosses = v;
        StorageStats[j].exfilLosses = v;
        StorageStats[j].maxVolDate = v;
    }
    if ((!group || group == 5) && OutfallStats) for (j=0; j<Nnodes[OUTFALL]; j++)
    {
        OutfallStats[j].avgFlow = v;
        OutfallStats[j].maxFlow = v;
        for (i=0; i<Nobjects[POLLUT]; i++) OutfallStats[j].totalLoad[i] = v;
        OutfallStats[j].totalPeriods = n;
    }
    if ((!group || group == 6) && PumpStats) for (j=0; j<Nlinks[PUMP]; j++)
    {
        PumpStats[j].utilized = v;
        PumpStats[j].minFlow = v;
        PumpStats[j].avgFlow = v;
        PumpStats[j].maxFlow = v;
        PumpStats[j].volume = v;
        PumpStats[j].energy = v;
        PumpStats[j].offCurveLow = v;
        PumpStats[j].offCurveHigh = v;
        PumpStats[j].startUps = n;
        PumpStats[j].totalPeriods = n;
    }
    if (!group || group == 7) {
        TimeStepStats.minTimeStep = v;
        TimeStepStats.maxTimeStep = v;
        TimeStepStats.routingTime = v;
        TimeStepStats.timeStepCount = n;
        TimeStepStats.trialsCount = v;
        TimeStepStats.steadyStateTime = v;
        for (j=1; j<TIMELEVELS; j++) TimeStepStats.timeStepCounts[j] = n;
        MaxOutfallFlow = v;
        MaxRunoffFlow = v;
        RoutingTimeSpan = v;
    }
    if (group == 8) {
        SysOutfallFlow = NAN; TimeStepStats.timeStepCounts[0] = -7;
        for (j=0; j<MAX_STATS; j++) {
            MaxMassBalErrs[j].value = NAN; MaxMassBalErrs[j].index = -7; MaxMassBalErrs[j].objType = -7;
            MaxCourantCrit[j].value = NAN; MaxCourantCrit[j].index = -7; MaxCourantCrit[j].objType = -7;
            MaxFlowTurns[j].value = NAN; MaxFlowTurns[j].index = -7; MaxFlowTurns[j].objType = -7;
            MaxNonConverged[j].value = NAN; MaxNonConverged[j].index = -7; MaxNonConverged[j].objType = -7;
        }
    }
}
