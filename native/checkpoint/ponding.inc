/* Appended only to easysewer_ponding.c, at a finalized public boundary. */
#include "es_checkpoint.h"
void es_ck_ponding(EsCkCursor *c)
{
    int j, mode;
    double flow, old_flow;
    if (c->error) return;
    if (PondingPending) { es_ck_fail(c, ES_CK_STATE); return; }
    es_ck_tag(c, "ESPOND01", 8);
    es_ck_fixed_i32(c, RouteModel);
    es_ck_fixed_i32(c, AllowPonding);
    es_ck_fixed_i32(c, IgnoreRouting);
    es_ck_fixed_i32(c, Nobjects[NODE]);
    for (j=0; j<Nobjects[NODE]; j++) es_ck_identity(c, Node[j].ID);
    mode = es_ck_i32(c, &PondingMode, FALSE, TRUE);
    flow = es_ck_f64_value(c, &PondingFlow);
    old_flow = es_ck_f64_value(c, &OldPondingFlow);
    if (flow < 0.0 || old_flow < 0.0 ||
        (!mode && (flow != 0.0 || old_flow != 0.0)) ||
        (mode && (RouteModel != DW || !AllowPonding || IgnoreRouting || !Nobjects[NODE])))
        es_ck_fail(c, ES_CK_NUMBER);
    /* Step/trials/steady/CollectStats belong to the completed split step.
     * begin clears CollectStats; capture and stepStats replace values before
     * use. Node/Pollut counters and baselines belong to network.inc. */
}
