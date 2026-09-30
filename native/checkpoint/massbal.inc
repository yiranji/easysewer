/* Appended to massbal.c. Capture is before end/finalization. */
#include "es_checkpoint.h"
void es_ck_massbal(EsCkCursor *c)
{
    int j;
    if (c->error) return;
    es_ck_tag(c, "ESMASS01", 8);
    es_ck_fixed_i32(c, Nobjects[SUBCATCH]);
    es_ck_fixed_i32(c, Nobjects[NODE]);
    es_ck_fixed_i32(c, Nobjects[LINK]);
    es_ck_fixed_i32(c, Nobjects[POLLUT]);
    es_ck_fixed_i32(c, UnitSystem);
    es_ck_fixed_i32(c, FlowUnits);
    es_ck_fixed_i32(c, IgnoreRainfall);
    es_ck_fixed_i32(c, IgnoreGwater);
    es_ck_fixed_i32(c, IgnoreRouting);
    es_ck_fixed_i32(c, IgnoreQuality);
    es_ck_fixed_i32(c, RouteModel);
    es_ck_fixed_f64(c, TotalArea);
    if ((Nobjects[NODE] && (!NodeInflow || !NodeOutflow)) ||
        (Nobjects[POLLUT] && (!LoadingTotals || !QualTotals || !StepQualTotals)))
    { es_ck_fail(c, ES_CK_STATE); return; }
    for (j=0; j<Nobjects[SUBCATCH]; j++) es_ck_identity(c, Subcatch[j].ID);
    for (j=0; j<Nobjects[NODE]; j++) es_ck_identity(c, Node[j].ID);
    for (j=0; j<Nobjects[LINK]; j++) es_ck_identity(c, Link[j].ID);
    es_ck_f64(c, &RunoffTotals.rainfall);
    es_ck_f64(c, &RunoffTotals.evap);
    es_ck_f64(c, &RunoffTotals.infil);
    es_ck_f64(c, &RunoffTotals.runoff);
    es_ck_f64(c, &RunoffTotals.drains);
    es_ck_f64(c, &RunoffTotals.runon);
    es_ck_f64(c, &RunoffTotals.initStorage);
    es_ck_f64(c, &RunoffTotals.initSnowCover);
    es_ck_f64(c, &GwaterTotals.infil);
    es_ck_f64(c, &GwaterTotals.upperEvap);
    es_ck_f64(c, &GwaterTotals.lowerEvap);
    es_ck_f64(c, &GwaterTotals.lowerPerc);
    es_ck_f64(c, &GwaterTotals.gwater);
    es_ck_f64(c, &GwaterTotals.initStorage);
    es_ck_f64(c, &FlowTotals.dwInflow);
    es_ck_f64(c, &FlowTotals.wwInflow);
    es_ck_f64(c, &FlowTotals.gwInflow);
    es_ck_f64(c, &FlowTotals.iiInflow);
    es_ck_f64(c, &FlowTotals.exInflow);
    es_ck_f64(c, &FlowTotals.flooding);
    es_ck_f64(c, &FlowTotals.outflow);
    es_ck_f64(c, &FlowTotals.evapLoss);
    es_ck_f64(c, &FlowTotals.seepLoss);
    es_ck_f64(c, &FlowTotals.reacted);
    es_ck_f64(c, &FlowTotals.initStorage);
    es_ck_f64(c, &StepFlowTotals.dwInflow);
    es_ck_f64(c, &StepFlowTotals.wwInflow);
    es_ck_f64(c, &StepFlowTotals.gwInflow);
    es_ck_f64(c, &StepFlowTotals.iiInflow);
    es_ck_f64(c, &StepFlowTotals.exInflow);
    es_ck_f64(c, &StepFlowTotals.flooding);
    es_ck_f64(c, &StepFlowTotals.outflow);
    es_ck_f64(c, &StepFlowTotals.evapLoss);
    es_ck_f64(c, &StepFlowTotals.seepLoss);
    es_ck_f64(c, &StepFlowTotals.reacted);
    es_ck_f64(c, &OldStepFlowTotals.dwInflow);
    es_ck_f64(c, &OldStepFlowTotals.wwInflow);
    es_ck_f64(c, &OldStepFlowTotals.gwInflow);
    es_ck_f64(c, &OldStepFlowTotals.iiInflow);
    es_ck_f64(c, &OldStepFlowTotals.exInflow);
    es_ck_f64(c, &OldStepFlowTotals.flooding);
    es_ck_f64(c, &OldStepFlowTotals.outflow);
    es_ck_f64(c, &OldStepFlowTotals.evapLoss);
    es_ck_f64(c, &OldStepFlowTotals.seepLoss);
    es_ck_f64(c, &OldStepFlowTotals.reacted);
    for (j=0; j<Nobjects[POLLUT] && !c->error; j++)
    {
        es_ck_identity(c, Pollut[j].ID);
        es_ck_fixed_i32(c, Pollut[j].units);
        es_ck_fixed_f64(c, Pollut[j].mcf);
        es_ck_f64(c, &LoadingTotals[j].initLoad);
        es_ck_f64(c, &LoadingTotals[j].buildup);
        es_ck_f64(c, &LoadingTotals[j].deposition);
        es_ck_f64(c, &LoadingTotals[j].sweeping);
        es_ck_f64(c, &LoadingTotals[j].bmpRemoval);
        es_ck_f64(c, &LoadingTotals[j].infil);
        es_ck_f64(c, &LoadingTotals[j].runoff);
        es_ck_f64(c, &LoadingTotals[j].finalLoad);
        es_ck_f64(c, &QualTotals[j].dwInflow);
        es_ck_f64(c, &QualTotals[j].wwInflow);
        es_ck_f64(c, &QualTotals[j].gwInflow);
        es_ck_f64(c, &QualTotals[j].iiInflow);
        es_ck_f64(c, &QualTotals[j].exInflow);
        es_ck_f64(c, &QualTotals[j].flooding);
        es_ck_f64(c, &QualTotals[j].outflow);
        es_ck_f64(c, &QualTotals[j].seepLoss);
        es_ck_f64(c, &QualTotals[j].reacted);
        es_ck_f64(c, &QualTotals[j].initStorage);
        es_ck_f64(c, &QualTotals[j].finalStorage);
        es_ck_f64(c, &StepQualTotals[j].dwInflow);
        es_ck_f64(c, &StepQualTotals[j].wwInflow);
        es_ck_f64(c, &StepQualTotals[j].gwInflow);
        es_ck_f64(c, &StepQualTotals[j].iiInflow);
        es_ck_f64(c, &StepQualTotals[j].exInflow);
        es_ck_f64(c, &StepQualTotals[j].flooding);
        es_ck_f64(c, &StepQualTotals[j].outflow);
        es_ck_f64(c, &StepQualTotals[j].seepLoss);
        es_ck_f64(c, &StepQualTotals[j].reacted);
        es_ck_f64(c, &StepQualTotals[j].finalStorage);
    }
    for (j=0; j<Nobjects[NODE]; j++)
    { es_ck_f64(c, &NodeInflow[j]); es_ck_f64(c, &NodeOutflow[j]); }
    es_ck_f64(c, &RunoffError);
    es_ck_f64(c, &GwaterError);
    es_ck_f64(c, &FlowError);
    es_ck_f64(c, &QualError);
    /* Final runoff/GW storage and percentages are recomputed at end.
       Loading.finalLoad and Qual.finalStorage accumulate before end and MUST
       survive. Unused step storage/percent fields and quality evaporation do
       not enter supported runtime/output paths; do not read stale copies. */
}
