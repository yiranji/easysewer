/* Appended to flowrout.c. Numeric Node/Link/subtype state, not whole routing.
 * ES_CK_CUSTOM selects the matched prepared engine's ponding extensions.
 */
#include "es_checkpoint.h"
#include "infil.h"
#include "exfil.h"

static void es_ck_network_char(EsCkCursor *c, char *p, int maximum)
{
    int value = *p;
    es_ck_i32(c, &value, 0, maximum);
    if (!c->error && c->mode == ES_CK_APPLY) *p = (char)value;
}

static void es_ck_network_array(EsCkCursor *c, double *p, int n)
{
    int i;
    if (n < 0 || (n && !p)) { es_ck_fail(c, ES_CK_STATE); return; }
    for (i = 0; i < n && !c->error; i++) es_ck_f64(c, &p[i]);
}

#define ES_ND(p, field) es_ck_f64(c, &(p)->field)
#define ES_NF(p, field) es_ck_fixed_f64(c, (p)->field)
#define ES_NI(p, field) es_ck_fixed_i32(c, (p)->field)
#define ES_NB(p, field) es_ck_network_char(c, &(p)->field, 1)

static void es_ck_network_green_ampt(EsCkCursor *c, TGrnAmpt *p)
{
    if (!p) { es_ck_fail(c, ES_CK_STATE); return; }
    ES_NF(p, S); ES_NF(p, Ks); ES_NF(p, IMDmax); ES_NF(p, Lu);
    /* F and T can go negative during recovery; retain finite native values. */
    ES_ND(p, IMD); ES_ND(p, F); ES_ND(p, Fu); ES_ND(p, T); ES_NB(p, Sat);
}

static void es_ck_network_storage(EsCkCursor *c, TStorage *p)
{
    TExfil *x = p->exfil;
    ES_NI(p, shape); ES_NI(p, aCurve);
    ES_NF(p, a0); ES_NF(p, a1); ES_NF(p, a2); ES_NF(p, fEvap);
    ES_ND(p, hrt); ES_ND(p, evapLoss); ES_ND(p, exfilLoss);
    es_ck_fixed_i32(c, x != NULL);
    if (x && !c->error)
    {
        ES_NF(x, btmArea); ES_NF(x, bankMinDepth);
        ES_NF(x, bankMaxDepth); ES_NF(x, bankMaxArea);
        es_ck_network_green_ampt(c, x->btmExfil);
        es_ck_network_green_ampt(c, x->bankExfil);
    }
}

static void es_ck_network_outfall(EsCkCursor *c, TOutfall *p)
{
    int type;
    /* Public setOutfallStage changes both type and fixedStage at run time. */
    type = es_ck_i32(c, &p->type, FREE_OUTFALL, TIMESERIES_OUTFALL);
    ES_ND(p, fixedStage);
    ES_NI(p, hasFlapGate); ES_NI(p, tideCurve); ES_NI(p, stageSeries); ES_NI(p, routeTo);
    if ((type == TIDAL_OUTFALL && (p->tideCurve < 0 || p->tideCurve >= Nobjects[CURVE])) ||
        (type == TIMESERIES_OUTFALL && (p->stageSeries < 0 || p->stageSeries >= Nobjects[TSERIES])))
        es_ck_fail(c, ES_CK_STATE);
    if (p->routeTo >= 0)
    {
        ES_ND(p, vRouted);
        es_ck_network_array(c, p->wRouted, Nobjects[POLLUT]);
    }
}

static void es_ck_network_node(EsCkCursor *c, TNode *p)
{
    es_ck_identity(c, p->ID);
    ES_NI(p, type); ES_NI(p, subIndex); ES_NI(p, rptFlag);
    ES_NI(p, degree); ES_NI(p, inlet);
    ES_NF(p, invertElev); ES_NF(p, fullDepth); ES_NF(p, surDepth);
    ES_NF(p, pondedArea); ES_NF(p, crownElev); ES_NF(p, fullVolume);
    ES_NB(p, updated);
    ES_ND(p, inflow); ES_ND(p, outflow); ES_ND(p, losses);
    ES_ND(p, oldVolume); ES_ND(p, newVolume); ES_ND(p, overflow);
    ES_ND(p, oldDepth); ES_ND(p, newDepth); ES_ND(p, oldLatFlow); ES_ND(p, newLatFlow);
    ES_ND(p, oldFlowInflow); ES_ND(p, oldNetInflow); ES_ND(p, qualInflow); ES_ND(p, apiExtInflow);
#if ES_CK_CUSTOM
    ES_ND(p, exflooding); ES_ND(p, pondingBaseVolume); ES_ND(p, pondingBaseDepth);
    ES_ND(p, pondingBaseOverflow); ES_ND(p, pondingRemovedVolume);
#endif
    es_ck_network_array(c, p->oldQual, Nobjects[POLLUT]);
    es_ck_network_array(c, p->newQual, Nobjects[POLLUT]);
    if (c->error) return;
    if (p->type < JUNCTION || p->type > DIVIDER || p->subIndex < 0 ||
        p->subIndex >= Nnodes[p->type])
    { es_ck_fail(c, ES_CK_STATE); return; }
    if (p->type == STORAGE)
    {
        if (!Storage) { es_ck_fail(c, ES_CK_STATE); return; }
        es_ck_network_storage(c, &Storage[p->subIndex]);
    }
    else if (p->type == OUTFALL)
    {
        if (!Outfall) { es_ck_fail(c, ES_CK_STATE); return; }
        es_ck_network_outfall(c, &Outfall[p->subIndex]);
    }
    /* Divider's fields are all reconstructed input/derived configuration. */
}

static void es_ck_network_conduit(EsCkCursor *c, TConduit *p)
{
    int full = p->fullState;
    ES_NF(p, length); ES_NF(p, roughness); ES_NI(p, barrels);
    ES_NF(p, modLength); ES_NF(p, roughFactor); ES_NF(p, slope);
    ES_NF(p, beta); ES_NF(p, qMax); ES_NI(p, superCritical); ES_NI(p, hasLosses);
    ES_ND(p, a1); ES_ND(p, a2); ES_ND(p, q1); ES_ND(p, q2);
    ES_ND(p, q1Old); ES_ND(p, q2Old); ES_ND(p, evapLossRate); ES_ND(p, seepLossRate);
    ES_NB(p, capacityLimited);
    full = es_ck_i32(c, &full, 0, ALL_FULL);
    if (full != 0 && full != UP_FULL && full != DN_FULL && full != ALL_FULL)
        es_ck_fail(c, ES_CK_STATE);
    if (!c->error && c->mode == ES_CK_APPLY) p->fullState = (char)full;
}

static void es_ck_network_link(EsCkCursor *c, TLink *p)
{
    int k = p->subIndex;
    es_ck_identity(c, p->ID);
    ES_NI(p, type); ES_NI(p, subIndex); ES_NI(p, rptFlag);
    ES_NI(p, node1); ES_NI(p, node2); ES_NI(p, direction);
    ES_NF(p, qFull); ES_NF(p, offset1); ES_NF(p, offset2);
    es_ck_fixed_i32(c, p->xsect.type);
    if (p->xsect.type == FILLED_CIRCULAR)
    {
        /* Geometry helpers temporarily add/subtract yBot/aBot in-place.
         * Floating add/subtract need not restore the original bit pattern. */
        double depth = es_ck_f64_value(c, &p->xsect.yFull);
        double area = es_ck_f64_value(c, &p->xsect.aFull);
        if (depth <= 0.0 || area <= 0.0) es_ck_fail(c, ES_CK_STATE);
    }
    else
    {
        es_ck_fixed_f64(c, p->xsect.yFull);
        es_ck_fixed_f64(c, p->xsect.aFull);
    }
    ES_ND(p, oldFlow); ES_ND(p, newFlow); ES_ND(p, oldDepth); ES_ND(p, newDepth);
    ES_ND(p, oldVolume); ES_ND(p, newVolume); ES_ND(p, surfArea1); ES_ND(p, surfArea2);
    ES_ND(p, setting); ES_ND(p, targetSetting); ES_ND(p, timeLastSet);
    ES_ND(p, froude); ES_ND(p, dqdh);
    es_ck_i32(c, &p->flowClass, DRY, MAX_FLOW_CLASSES-1);
    ES_NB(p, bypassed); ES_NB(p, normalFlow); ES_NB(p, inletControl);
    es_ck_network_array(c, p->oldQual, Nobjects[POLLUT]);
    es_ck_network_array(c, p->newQual, Nobjects[POLLUT]);
    es_ck_network_array(c, p->totalLoad, Nobjects[POLLUT]);
    if (c->error) return;
    if (p->type < CONDUIT || p->type > OUTLET || k < 0 || k >= Nlinks[p->type])
    { es_ck_fail(c, ES_CK_STATE); return; }
    if (p->type == CONDUIT)
    {
        if (!Conduit) { es_ck_fail(c, ES_CK_STATE); return; }
        es_ck_network_conduit(c, &Conduit[k]);
    }
    else if (p->type == ORIFICE)
    {
        TOrifice *o;
        if (!Orifice) { es_ck_fail(c, ES_CK_STATE); return; }
        o = &Orifice[k];
        ES_NI(o, type); ES_NI(o, shape); ES_NF(o, cDisch); ES_NF(o, orate); ES_NF(o, length);
        ES_ND(o, cOrif); ES_ND(o, hCrit); ES_ND(o, cWeir); ES_ND(o, surfArea);
    }
    else if (p->type == WEIR)
    {
        TWeir *w;
        if (!Weir) { es_ck_fail(c, ES_CK_STATE); return; }
        w = &Weir[k];
        ES_NI(w, type); ES_NF(w, length); ES_NF(w, slope);
        ES_ND(w, cSurcharge); ES_ND(w, surfArea);
    }
    /* Pump/Outlet subtype structs contain only reconstructed configuration. */
}

void es_ck_network(EsCkCursor *c)
{
    int i;
    if (c->error) return;
    if (es_ck_engine_boundary()) { es_ck_fail(c, ES_CK_STATE); return; }
    es_ck_tag(c, "ESNET001", 8);
    es_ck_fixed_i32(c, ES_CK_CUSTOM);
    es_ck_fixed_i32(c, RouteModel);
    es_ck_fixed_i32(c, Nobjects[NODE]); es_ck_fixed_i32(c, Nobjects[LINK]);
    es_ck_fixed_i32(c, Nobjects[POLLUT]);
    if (Nobjects[NODE] < 0 || Nobjects[LINK] < 0 || Nobjects[POLLUT] < 0 ||
        (Nobjects[NODE] && !Node) || (Nobjects[LINK] && !Link) ||
        (Nobjects[POLLUT] && !Pollut))
    { es_ck_fail(c, ES_CK_STATE); return; }
    for (i = 0; i < Nobjects[POLLUT] && !c->error; i++)
    {
        es_ck_identity(c, Pollut[i].ID);
        es_ck_fixed_i32(c, Pollut[i].units);
#if ES_CK_CUSTOM
        es_ck_f64(c, &Pollut[i].pondingRemovedMass);
#endif
    }
    for (i = 0; i < Nobjects[NODE] && !c->error; i++) es_ck_network_node(c, &Node[i]);
    for (i = 0; i < Nobjects[LINK] && !c->error; i++) es_ck_network_link(c, &Link[i]);
}

#undef ES_ND
#undef ES_NF
#undef ES_NI
#undef ES_NB
