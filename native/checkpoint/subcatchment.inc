/* Appended to subcatch.c. Snow, groundwater and LID have separate owners. */
#include "es_checkpoint.h"

#define ES_SF(p, f) es_ck_fixed_f64(c, (p)->f)
#define ES_SI(p, f) es_ck_fixed_i32(c, (p)->f)
#define ES_SD(p, f) es_ck_f64(c, &(p)->f)

static void es_ck_subcatch_array(EsCkCursor *c, double *p, int fixed)
{
    int i;
    if (Nobjects[POLLUT] && !p) { es_ck_fail(c, ES_CK_STATE); return; }
    for (i = 0; i < Nobjects[POLLUT] && !c->error; i++)
        if (fixed) es_ck_fixed_f64(c, p[i]); else es_ck_f64(c, &p[i]);
}

void es_ck_subcatchment(EsCkCursor *c)
{
    int j, i;
    TSubcatch *p;
    TSubarea *a;
    TLandFactor *land;
    es_ck_tag(c, "ESSUB001", 8);
    es_ck_fixed_i32(c, Nobjects[SUBCATCH]);
    es_ck_fixed_i32(c, Nobjects[POLLUT]);
    es_ck_fixed_i32(c, Nobjects[LANDUSE]);
    if (c->error) return;
    for (i = 0; i < Nobjects[POLLUT] && !c->error; i++)
    {
        es_ck_identity(c, Pollut[i].ID);
        ES_SI(&Pollut[i], units);
    }
    for (i = 0; i < Nobjects[LANDUSE] && !c->error; i++)
        es_ck_identity(c, Landuse[i].ID);
    for (j = 0; j < Nobjects[SUBCATCH] && !c->error; j++)
    {
        p = &Subcatch[j];
        es_ck_identity(c, p->ID);
        ES_SI(p, rptFlag); ES_SI(p, gage); ES_SI(p, outNode); ES_SI(p, outSubcatch);
        ES_SI(p, infilModel); ES_SI(p, infil);
        ES_SI(p, nPervPattern); ES_SI(p, dStorePattern); ES_SI(p, infilPattern);
        es_ck_fixed_i32(c, p->groundwater != NULL);
        es_ck_fixed_i32(c, p->gwLatFlowExpr != NULL);
        es_ck_fixed_i32(c, p->gwDeepFlowExpr != NULL);
        es_ck_fixed_i32(c, p->snowpack != NULL);
        ES_SF(p, width); ES_SF(p, area); ES_SF(p, fracImperv);
        ES_SF(p, slope); ES_SF(p, curbLength); ES_SF(p, lidArea);
        for (i = IMPERV0; i <= PERV; i++)
        {
            a = &p->subArea[i];
            ES_SI(a, routeTo); ES_SF(a, fOutlet); ES_SF(a, N);
            ES_SF(a, fArea); ES_SF(a, dStore); ES_SF(a, alpha);
            ES_SD(a, inflow); ES_SD(a, runoff); ES_SD(a, depth);
        }
        ES_SD(p, rainfall); ES_SD(p, evapLoss); ES_SD(p, infilLoss); ES_SD(p, runon);
        ES_SD(p, oldRunoff); ES_SD(p, newRunoff);
        ES_SD(p, oldSnowDepth); ES_SD(p, newSnowDepth);
        es_ck_subcatch_array(c, p->initBuildup, 1);
        es_ck_subcatch_array(c, p->oldQual, 0);
        es_ck_subcatch_array(c, p->newQual, 0);
        es_ck_subcatch_array(c, p->pondedQual, 0);
        es_ck_subcatch_array(c, p->totalLoad, 0);
        if (Nobjects[LANDUSE] && !p->landFactor)
        { es_ck_fail(c, ES_CK_STATE); return; }
        for (i = 0; i < Nobjects[LANDUSE] && !c->error; i++)
        {
            land = &p->landFactor[i];
            ES_SF(land, fraction);
            ES_SD(land, lastSwept);
            es_ck_subcatch_array(c, land->buildup, 0);
        }
    }
    /* theSubarea, Alpha/Dstore and the ten per-step water-volume globals are
       set before use. surfqual/landuse own no additional mutable globals. */
}

#undef ES_SF
#undef ES_SI
#undef ES_SD
