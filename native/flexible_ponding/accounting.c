/* easysewer custom ABI 201: post-routing ponding adjustments and accounting.
 * External removal is a discrete end-of-step volume, never a trapezoidal
 * StepFlowTotals rate. The ordinary SWMM integration remains separate.
 */
#include <math.h>
#include "headers.h"

extern TRoutingTotals FlowTotals;
extern TRoutingTotals* QualTotals;
extern double* NodeOutflow;
extern TNodeStats* NodeStats;

int PondingMode = FALSE;
int PondingPending = FALSE;
double PondingFlow = 0.0;
double OldPondingFlow = 0.0;
static double PondingStep = 0.0;
static int CollectStats = FALSE;
static int StepTrials = 0;
static int StepSteady = FALSE;

static int failPonding(const char* reason)
{
    report_writeErrorMsg(ERR_API_PROPERTY_VALUE, "");
    snprintf(ErrorMsg, MAXMSG, "\n  ERROR 508: %s", reason);
    report_writeLine(ErrorMsg);
    return ErrorCode;
}

void ponding_reset(void)
{
    int j;
    PondingMode = FALSE;
    PondingPending = FALSE;
    PondingFlow = OldPondingFlow = PondingStep = 0.0;
    CollectStats = FALSE;
    for (j = 0; j < Nobjects[NODE]; j++)
    {
        Node[j].exflooding = 0.0;
        Node[j].pondingRemovedVolume = 0.0;
    }
    for (j = 0; j < Nobjects[POLLUT]; j++)
        Pollut[j].pondingRemovedMass = 0.0;
}

int ponding_begin(void)
{
    int j;
    if (PondingPending)
        return failPonding("previous ponding step must be finalized before routing again");
    if (RouteModel != DW || !AllowPonding || IgnoreRouting || Nobjects[NODE] == 0)
        return failPonding("ponding requires active dynamic-wave nodes and ALLOW_PONDING");
    PondingMode = TRUE;
    OldPondingFlow = PondingFlow;
    PondingFlow = 0.0;
    CollectStats = FALSE;
    for (j = 0; j < Nobjects[NODE]; j++) Node[j].exflooding = 0.0;
    return 0;
}

void ponding_stepStats(int trials, int steady)
{
    CollectStats = TRUE;
    StepTrials = trials;
    StepSteady = steady;
}

int ponding_capture(void)
{
    int j;
    PondingStep = (NewRoutingTime - OldRoutingTime) / 1000.0;
    if (!isfinite(PondingStep) || PondingStep <= 0.0)
        return failPonding("routing clock did not advance");
    for (j = 0; j < Nobjects[NODE]; j++)
    {
        Node[j].pondingBaseVolume = Node[j].newVolume;
        Node[j].pondingBaseDepth = Node[j].newDepth;
        Node[j].pondingBaseOverflow = Node[j].overflow;
    }
    PondingPending = TRUE;
    return 0;
}

int ponding_finalize(void)
{
    int j, p;
    double volume, depth, tolerance, mass;
    TNode* node;
    if (!PondingPending)
        return failPonding("no routed ponding step is awaiting finalization");

    /* Validate all hydraulic changes before changing any cumulative totals. */
    for (j = 0; j < Nobjects[NODE]; j++)
    {
        node = &Node[j];
        volume = node->exflooding * PondingStep;
        tolerance = 1.e-9 * MAX(1.0, node->pondingBaseVolume);
        if (!isfinite(volume) || volume < 0.0 ||
            !isfinite(node->newVolume) || !isfinite(node->newDepth) ||
            !isfinite(node->overflow) || node->newVolume < 0.0 ||
            fabs(node->pondingBaseVolume - node->newVolume - volume) > tolerance)
            return failPonding("external flow and actual removed node volume disagree");
        depth = node->pondingBaseDepth;
        if (volume > 0.0)
        {
            if ((node->type != JUNCTION && node->type != DIVIDER) ||
                node->pondedArea <= 0.0 || node->pondingBaseDepth < node->fullDepth ||
                volume > node->pondedArea * (depth - node->fullDepth) + tolerance ||
                volume > node->pondingBaseOverflow * PondingStep + tolerance)
                return failPonding("removal exceeds available ponded water or overflow");
            depth -= volume / node->pondedArea;
        }
        if (fabs(node->newDepth - depth) > 1.e-9 * MAX(1.0, fabs(depth)) ||
            fabs(node->overflow - (node->pondingBaseOverflow - node->exflooding)) >
                1.e-9 * MAX(1.0, fabs(node->pondingBaseOverflow)))
            return failPonding("ponding depth, volume and overflow changes disagree");
        if (!IgnoreQuality) for (p = 0; p < Nobjects[POLLUT]; p++)
        {
            mass = volume * node->newQual[p];
            if (!isfinite(mass) || mass < 0.0)
                return failPonding("removed pollutant quantity is invalid");
        }
    }

    for (j = 0; j < Nobjects[NODE]; j++)
    {
        node = &Node[j];
        volume = node->exflooding * PondingStep;
        FlowTotals.flooding += volume;
        NodeOutflow[j] += volume;
        node->pondingRemovedVolume += volume;
        PondingFlow += node->exflooding;
        if (!IgnoreQuality) for (p = 0; p < Nobjects[POLLUT]; p++)
        {
            /* Complete mixing: volume removal leaves concentration unchanged. */
            mass = volume * node->newQual[p];
            QualTotals[p].flooding += mass;
            Pollut[p].pondingRemovedMass += mass;
        }
    }
    if (CollectStats)
    {
        stats_updateFlowStats(PondingStep, getDateTime(NewRoutingTime));
        stats_updateTimeStepStats(PondingStep, StepTrials, StepSteady);
    }
    PondingPending = FALSE;
    return 0;
}

int ponding_nodeStat(int j, int field, double* value)
{
    TNodeStats* stats;
    if (!value) return ERR_API_PROPERTY_VALUE;
    if (j < 0 || j >= Nobjects[NODE]) return ERR_API_OBJECT_INDEX;
    if (!NodeStats) return ERR_API_NOT_STARTED;
    stats = &NodeStats[j];
    switch (field)
    {
    case 0: *value = Node[j].pondingRemovedVolume * UCF(VOLUME); break;
    case 1: *value = stats->maxDepth * UCF(LENGTH); break;
    case 2: *value = stats->maxOverflow * UCF(FLOW); break;
    case 3: *value = stats->volFlooded * UCF(VOLUME); break;
    case 4: *value = stats->maxPondedVol * UCF(VOLUME); break;
    case 5: *value = stats->timeFlooded; break;
    case 6: *value = stats->maxDepthDate; break;
    case 7: *value = stats->maxOverflowDate; break;
    case 8: *value = ReportStepCount ? stats->avgDepth / ReportStepCount * UCF(LENGTH) : 0.0; break;
    default: return ERR_API_PROPERTY_TYPE;
    }
    return 0;
}
