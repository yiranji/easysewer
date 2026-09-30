/* Test-only mass balance damage injection. */
#include "swmm5.h"
void DLLEXPORT es_test_massbal_change(int group, int poison)
{
    int j; double v = poison ? NAN : 0.0;
    if (!group || group == 1) {
        RunoffTotals.rainfall = v;
        RunoffTotals.evap = v;
        RunoffTotals.infil = v;
        RunoffTotals.runoff = v;
        RunoffTotals.drains = v;
        RunoffTotals.runon = v;
        RunoffTotals.initStorage = v;
        RunoffTotals.initSnowCover = v;
    }
    if (!group || group == 2) {
        GwaterTotals.infil = v;
        GwaterTotals.upperEvap = v;
        GwaterTotals.lowerEvap = v;
        GwaterTotals.lowerPerc = v;
        GwaterTotals.gwater = v;
        GwaterTotals.initStorage = v;
    }
    if (!group || group == 3) {
        FlowTotals.dwInflow = v;
        FlowTotals.wwInflow = v;
        FlowTotals.gwInflow = v;
        FlowTotals.iiInflow = v;
        FlowTotals.exInflow = v;
        FlowTotals.flooding = v;
        FlowTotals.outflow = v;
        FlowTotals.evapLoss = v;
        FlowTotals.seepLoss = v;
        FlowTotals.reacted = v;
        FlowTotals.initStorage = v;
    }
    if (!group || group == 4) {
        StepFlowTotals.dwInflow = v;
        StepFlowTotals.wwInflow = v;
        StepFlowTotals.gwInflow = v;
        StepFlowTotals.iiInflow = v;
        StepFlowTotals.exInflow = v;
        StepFlowTotals.flooding = v;
        StepFlowTotals.outflow = v;
        StepFlowTotals.evapLoss = v;
        StepFlowTotals.seepLoss = v;
        StepFlowTotals.reacted = v;
    }
    if (!group || group == 5) {
        OldStepFlowTotals.dwInflow = v;
        OldStepFlowTotals.wwInflow = v;
        OldStepFlowTotals.gwInflow = v;
        OldStepFlowTotals.iiInflow = v;
        OldStepFlowTotals.exInflow = v;
        OldStepFlowTotals.flooding = v;
        OldStepFlowTotals.outflow = v;
        OldStepFlowTotals.evapLoss = v;
        OldStepFlowTotals.seepLoss = v;
        OldStepFlowTotals.reacted = v;
    }
    if (!group || group == 6) for (j=0; j<Nobjects[POLLUT]; j++) {
        LoadingTotals[j].initLoad = v;
        LoadingTotals[j].buildup = v;
        LoadingTotals[j].deposition = v;
        LoadingTotals[j].sweeping = v;
        LoadingTotals[j].bmpRemoval = v;
        LoadingTotals[j].infil = v;
        LoadingTotals[j].runoff = v;
        LoadingTotals[j].finalLoad = v;
    }
    if (!group || group == 7) for (j=0; j<Nobjects[POLLUT]; j++) {
        QualTotals[j].dwInflow = v;
        QualTotals[j].wwInflow = v;
        QualTotals[j].gwInflow = v;
        QualTotals[j].iiInflow = v;
        QualTotals[j].exInflow = v;
        QualTotals[j].flooding = v;
        QualTotals[j].outflow = v;
        QualTotals[j].seepLoss = v;
        QualTotals[j].reacted = v;
        QualTotals[j].initStorage = v;
        QualTotals[j].finalStorage = v;
    }
    if (!group || group == 8) for (j=0; j<Nobjects[POLLUT]; j++) {
        StepQualTotals[j].dwInflow = v;
        StepQualTotals[j].wwInflow = v;
        StepQualTotals[j].gwInflow = v;
        StepQualTotals[j].iiInflow = v;
        StepQualTotals[j].exInflow = v;
        StepQualTotals[j].flooding = v;
        StepQualTotals[j].outflow = v;
        StepQualTotals[j].seepLoss = v;
        StepQualTotals[j].reacted = v;
        StepQualTotals[j].finalStorage = v;
    }
    if (!group || group == 9) for (j=0; j<Nobjects[NODE]; j++) NodeInflow[j] = NodeOutflow[j] = v;
    if (!group || group == 10) RunoffError = GwaterError = FlowError = QualError = v;
    if (group == 11) {
        RunoffTotals.finalStorage = RunoffTotals.finalSnowCover = RunoffTotals.snowRemoved = RunoffTotals.pctError = NAN;
        GwaterTotals.finalStorage = GwaterTotals.pctError = NAN;
        FlowTotals.finalStorage = FlowTotals.pctError = NAN;
        StepFlowTotals.initStorage = StepFlowTotals.finalStorage = StepFlowTotals.pctError = NAN;
        OldStepFlowTotals.initStorage = OldStepFlowTotals.finalStorage = OldStepFlowTotals.pctError = NAN;
        for (j=0; j<Nobjects[POLLUT]; j++) {
            LoadingTotals[j].pctError = QualTotals[j].pctError = QualTotals[j].evapLoss = NAN;
            StepQualTotals[j].initStorage = StepQualTotals[j].pctError = StepQualTotals[j].evapLoss = NAN;
        }
    }
}
