/* Appended to inlet.c: private inlet objects and cumulative statistics. */
#include "es_checkpoint.h"
#include <limits.h>

#define ES_IF(p, field) es_ck_fixed_f64(c, (p)->field)
#define ES_II(p, field) es_ck_fixed_i32(c, (p)->field)
#define ES_ID(p, field) es_ck_f64(c, &(p)->field)

void es_ck_inlet(EsCkCursor *c)
{
    TInlet *p;
    TInletDesign *d;
    int count = 0, i, flow, capture, backflow;
    double peak, efficiency, average, bypass;
    es_ck_tag(c, "ESINL001", 8);
    es_ck_fixed_i32(c, UsesInlets);
    es_ck_fixed_i32(c, InletDesignCount);
    es_ck_fixed_i32(c, Nobjects[NODE]);
    es_ck_fixed_i32(c, Nobjects[LINK]);
    if (c->error) return;
    if (InletDesignCount < 0 || (InletDesignCount && !InletDesigns))
    { es_ck_fail(c, ES_CK_STATE); return; }
    for (p = FirstInlet; p; p = p->nextInlet)
    {
        if (++count > Nobjects[LINK])
        { es_ck_fail(c, ES_CK_STATE); return; }
    }
    es_ck_fixed_i32(c, count);
    /* Designs are calloc-backed; unused variant fields are initialized zeros. */
    for (i = 0; i < InletDesignCount && !c->error; i++)
    {
        d = &InletDesigns[i];
        es_ck_identity(c, d->ID); ES_II(d, type); ES_II(d, customCurve);
        ES_II(&d->grateInlet, type);
        ES_IF(&d->grateInlet, length); ES_IF(&d->grateInlet, width);
        ES_IF(&d->grateInlet, fracOpenArea); ES_IF(&d->grateInlet, splashVeloc);
        ES_IF(&d->slottedInlet, length); ES_IF(&d->slottedInlet, width);
        ES_IF(&d->curbInlet, length); ES_IF(&d->curbInlet, height);
        ES_II(&d->curbInlet, throatAngle);
    }
    /* The linked-list order is significant for accumulation and is bound,
       never serialized as pointers or reconstructed from link index order. */
    for (p = FirstInlet; p && !c->error; p = p->nextInlet)
    {
        if (p->linkIndex < 0 || p->linkIndex >= Nobjects[LINK] ||
            p->nodeIndex < 0 || p->nodeIndex >= Nobjects[NODE] ||
            p->designIndex < 0 || p->designIndex >= InletDesignCount ||
            Link[p->linkIndex].inlet != p)
        { es_ck_fail(c, ES_CK_STATE); return; }
        ES_II(p, linkIndex); es_ck_identity(c, Link[p->linkIndex].ID);
        ES_II(p, nodeIndex); es_ck_identity(c, Node[p->nodeIndex].ID);
        ES_II(p, designIndex); ES_II(p, numInlets); ES_II(p, placement);
        ES_IF(p, clogFactor); ES_IF(p, flowLimit);
        ES_IF(p, localDepress); ES_IF(p, localWidth);
        ES_IF(p, flowFactor); ES_IF(p, backflowRatio);
        /* initInletStats initializes both values even before the first step. */
        ES_ID(p, flowCapture); ES_ID(p, backflow);
        flow = es_ck_i32(c, &p->stats.flowPeriods, 0, INT_MAX);
        capture = es_ck_i32(c, &p->stats.capturePeriods, 0, INT_MAX);
        backflow = es_ck_i32(c, &p->stats.backflowPeriods, 0, INT_MAX);
        peak = es_ck_f64_value(c, &p->stats.peakFlow);
        efficiency = es_ck_f64_value(c, &p->stats.peakFlowCapture);
        average = es_ck_f64_value(c, &p->stats.avgFlowCapture);
        bypass = es_ck_f64_value(c, &p->stats.bypassFreq);
        if ((int64_t)capture + backflow != flow || peak < 0 ||
            efficiency < 0 || efficiency > 100 || average < 0 ||
            bypass < 0 || bypass > capture)
            es_ck_fail(c, ES_CK_STATE);
    }
    /* InletFlow and the geometry globals are scratch, assigned before use.
       No allocation is required: project reconstruction owns this list. */
}

#undef ES_IF
#undef ES_II
#undef ES_ID
