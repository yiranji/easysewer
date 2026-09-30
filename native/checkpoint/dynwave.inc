/* Appended to dynwave.c: state owned by this translation unit only.
 * Node/Link/Conduit values and statistics belong to other checkpoint owners.
 */
#include "es_checkpoint.h"

void es_ck_dynwave(EsCkCursor *c, int routing_active)
{
    int i, active = routing_active && RouteModel == DW;
    double step;
    if (c->error) return;
    if (es_ck_engine_boundary()) { es_ck_fail(c, ES_CK_STATE); return; }
    es_ck_tag(c, "ESDYNW01", 8);
    es_ck_fixed_i32(c, active);
    es_ck_fixed_i32(c, RouteModel);
    /* Disabled routing and SF/KW must never inspect stale Xnode memory. */
    if (!active || c->error) return;
    if (Nobjects[NODE] < 0 || (Nobjects[NODE] && (!Node || !Xnode)))
    {
        es_ck_fail(c, ES_CK_STATE);
        return;
    }
    es_ck_fixed_i32(c, Nobjects[NODE]);
    es_ck_fixed_i32(c, SurchargeMethod);
    es_ck_fixed_i32(c, MaxTrials);
    es_ck_fixed_f64(c, MinRouteStep);
    es_ck_fixed_f64(c, MinSurfArea);
    es_ck_fixed_f64(c, HeadTol);
    es_ck_fixed_f64(c, CrownCutoff);
    step = es_ck_f64_value(c, &VariableStep);
    /* Zero is the initial-step sentinel. Do not cap against RouteStep:
     * the public setter can change RouteStep after this value was computed. */
    if (step < 0.0) es_ck_fail(c, ES_CK_STATE);
    for (i = 0; i < Nobjects[NODE] && !c->error; i++)
    {
        double area, rate;
        es_ck_identity(c, Node[i].ID);
        es_ck_fixed_i32(c, Node[i].type);
        es_ck_fixed_f64(c, Node[i].crownElev);
        area = es_ck_f64_value(c, &Xnode[i].oldSurfArea);
        rate = es_ck_f64_value(c, &Xnode[i].dYdT);
        if (area < 0.0 || rate < 0.0) es_ck_fail(c, ES_CK_STATE);
    }
    /* Steps/Omega and Xnode.converged/newSurfArea/sumdqdh are overwritten
     * before use on the next solve. No raw structs, pointers or padding. */
}
