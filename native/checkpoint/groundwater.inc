/* Appended to gwater.c. Object state and nested statistics persist; per-call
 * ODE/flux variables are scratch after the HydCon update fix. */
#include "es_checkpoint.h"

#define ES_GF(p, f) es_ck_fixed_f64(c, (p)->f)
#define ES_GI(p, f) es_ck_fixed_i32(c, (p)->f)
#define ES_GD(p, f) es_ck_f64(c, &(p)->f)

static void es_ck_gw_expression(EsCkCursor *c, MathExpr *expression)
{
    MathExpr *p, *previous = NULL;
    uint32_t count = 0;
    for (p = expression; p; p = p->next)
    {
        if (count == UINT32_MAX || p->prev != previous)
        { es_ck_fail(c, ES_CK_STATE); return; }
        count++; previous = p;
    }
    es_ck_fixed_u32(c, count);
    for (p = expression; p && !c->error; p = p->next)
    { ES_GI(p, opcode); ES_GI(p, ivar); ES_GF(p, fvalue); }
}

void es_ck_groundwater(EsCkCursor *c)
{
    int i, j, index, has_capacity;
    double pervious;
    TAquifer *a;
    TGroundwater *g;
    TGWaterStats *s;
    if (c->error) return;
    es_ck_tag(c, "ESGWT001", 8);
    es_ck_fixed_i32(c, Nobjects[SUBCATCH]);
    es_ck_fixed_i32(c, Nobjects[AQUIFER]);
    es_ck_fixed_i32(c, IgnoreGwater);
    es_ck_fixed_i32(c, UnitSystem); es_ck_fixed_i32(c, FlowUnits);
    for (i = 0; i < Nobjects[AQUIFER] && !c->error; i++)
    {
        a = &Aquifer[i]; es_ck_identity(c, a->ID);
        ES_GF(a, porosity); ES_GF(a, wiltingPoint); ES_GF(a, fieldCapacity);
        ES_GF(a, conductivity); ES_GF(a, conductSlope); ES_GF(a, tensionSlope);
        ES_GF(a, upperEvapFrac); ES_GF(a, lowerEvapDepth); ES_GF(a, lowerLossCoeff);
        ES_GF(a, bottomElev); ES_GF(a, waterTableElev); ES_GF(a, upperMoisture);
        ES_GI(a, upperEvapPat);
        index = a->upperEvapPat;
        if (index >= 0)
        {
            if (index >= Nobjects[TIMEPATTERN]) { es_ck_fail(c, ES_CK_STATE); return; }
            es_ck_identity(c, Pattern[index].ID);
            es_ck_fixed_i32(c, Pattern[index].type);
            es_ck_fixed_i32(c, Pattern[index].count);
            for (j = 0; j < 12; j++) es_ck_fixed_f64(c, Pattern[index].factor[j]);
        }
    }
    for (i = 0; i < Nobjects[SUBCATCH] && !c->error; i++)
    {
        es_ck_identity(c, Subcatch[i].ID);
        g = Subcatch[i].groundwater;
        es_ck_fixed_i32(c, g != NULL);
        if (!g) continue;
        if (g->aquifer < 0 || g->aquifer >= Nobjects[AQUIFER] ||
            g->node < 0 || g->node >= Nobjects[NODE])
        { es_ck_fail(c, ES_CK_STATE); return; }
        ES_GI(g, aquifer); ES_GI(g, node); es_ck_identity(c, Node[g->node].ID);
        ES_GF(g, surfElev); ES_GF(g, a1); ES_GF(g, b1); ES_GF(g, a2); ES_GF(g, b2);
        ES_GF(g, a3); ES_GF(g, fixedDepth); ES_GF(g, nodeElev); ES_GF(g, bottomElev);
        ES_GF(g, waterTableElev); ES_GF(g, upperMoisture);
        pervious = subcatch_getFracPerv(i);
        es_ck_fixed_f64(c, pervious);
        es_ck_fixed_f64(c, Subcatch[i].area);
        es_ck_gw_expression(c, Subcatch[i].gwLatFlowExpr);
        es_ck_gw_expression(c, Subcatch[i].gwDeepFlowExpr);
        ES_GD(g, theta); ES_GD(g, lowerDepth);
        ES_GD(g, oldFlow); ES_GD(g, newFlow); ES_GD(g, evapLoss);
        /* Native init divides by pervious fraction. With no pervious surface,
         * capacity can be infinite/NaN and is never consumed; neither surface
         * infiltration nor groundwater integration can use it. */
        has_capacity = pervious > 0.0;
        es_ck_fixed_i32(c, has_capacity);
        if (has_capacity) ES_GD(g, maxInfilVol);
        s = &g->stats;
        ES_GD(s, infil); ES_GD(s, evap); ES_GD(s, latFlow); ES_GD(s, deepFlow);
        ES_GD(s, avgUpperMoist); ES_GD(s, finalUpperMoist);
        ES_GD(s, avgWaterTable); ES_GD(s, finalWaterTable); ES_GD(s, maxFlow);
    }
}

#undef ES_GF
#undef ES_GI
#undef ES_GD
