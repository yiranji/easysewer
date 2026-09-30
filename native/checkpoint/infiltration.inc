/* Appended to infil.c. Only the active member of each union is inspected. */
#include "es_checkpoint.h"

#define ES_AF(p, f) es_ck_fixed_f64(c, (p)->f)
#define ES_AD(p, f) es_ck_f64(c, &(p)->f)

void es_ck_infiltration(EsCkCursor *c)
{
    int j, sat;
    TInfil *p;
    es_ck_tag(c, "ESINF001", 8);
    es_ck_fixed_i32(c, Nobjects[SUBCATCH]);
    if (c->error) return;
    if (Nobjects[SUBCATCH] && !Infil)
    { es_ck_fail(c, ES_CK_STATE); return; }
    for (j = 0; j < Nobjects[SUBCATCH] && !c->error; j++)
    {
        es_ck_identity(c, Subcatch[j].ID);
        es_ck_fixed_i32(c, Subcatch[j].infil);
        es_ck_fixed_i32(c, Subcatch[j].infilModel);
        if (Subcatch[j].infil < 0) continue;
        if (Subcatch[j].infil != j)
        { es_ck_fail(c, ES_CK_STATE); return; }
        p = &Infil[j];
        switch (Subcatch[j].infilModel)
        {
        case HORTON:
        case MOD_HORTON:
            ES_AF(&p->horton, f0); ES_AF(&p->horton, fmin);
            ES_AF(&p->horton, Fmax); ES_AF(&p->horton, decay); ES_AF(&p->horton, regen);
            ES_AD(&p->horton, tp); ES_AD(&p->horton, Fe);
            break;
        case GREEN_AMPT:
        case MOD_GREEN_AMPT:
            ES_AF(&p->grnAmpt, S); ES_AF(&p->grnAmpt, Ks);
            ES_AF(&p->grnAmpt, IMDmax); ES_AF(&p->grnAmpt, Lu);
            /* Recovery may produce signed F and T; don't clamp them. */
            ES_AD(&p->grnAmpt, IMD); ES_AD(&p->grnAmpt, F);
            ES_AD(&p->grnAmpt, Fu); ES_AD(&p->grnAmpt, T);
            sat = p->grnAmpt.Sat;
            es_ck_i32(c, &sat, 0, 1);
            if (!c->error && c->mode == ES_CK_APPLY) p->grnAmpt.Sat = (char)sat;
            break;
        case CURVE_NUMBER:
            ES_AF(&p->curveNum, Smax); ES_AF(&p->curveNum, regen); ES_AF(&p->curveNum, Tmax);
            ES_AD(&p->curveNum, S); ES_AD(&p->curveNum, F); ES_AD(&p->curveNum, P);
            ES_AD(&p->curveNum, T); ES_AD(&p->curveNum, Se); ES_AD(&p->curveNum, f);
            break;
        default: es_ck_fail(c, ES_CK_STATE);
        }
    }
    /* Fumax is recomputed for every Green-Ampt call. InfilFactor is selected
       before each subcatchment and before routing/storage infiltration. */
}

#undef ES_AF
#undef ES_AD
