/* Appended to lid.c after checked detailed-report helpers. Output streams and
 * prefixes have separate transaction ownership; this block owns numeric state
 * and the initialized previous-report-row cache only. */
#include "es_checkpoint.h"

#define ES_LF(p, f) es_ck_fixed_f64(c, (p)->f)
#define ES_LI(p, f) es_ck_fixed_i32(c, (p)->f)
#define ES_LD(p, f) es_ck_f64(c, &(p)->f)

static void es_ck_lid_row(EsCkCursor *c, TLidRptFile *r)
{
    uint32_t length = 0;
    const char *end;
    if (c->error) return;
    es_ck_i32(c, &r->wasDry, 0, 2);
    if (c->mode == ES_CK_MEASURE || c->mode == ES_CK_WRITE)
    {
        end = memchr(r->results, '\0', sizeof(r->results));
        if (!end) { es_ck_fail(c, ES_CK_STATE); return; }
        length = (uint32_t)(end - r->results);
    }
    length = es_ck_u32(c, &length);
    if (c->error) return;
    if (length >= sizeof(r->results) || length > SIZE_MAX - c->position ||
        (c->mode != ES_CK_MEASURE &&
         (c->position > c->size || length > c->size - c->position)))
    { es_ck_fail(c, ES_CK_BOUNDS); return; }
    if (c->mode == ES_CK_WRITE)
        memcpy(c->output + c->position, r->results, length);
    else if (c->mode == ES_CK_VALIDATE || c->mode == ES_CK_APPLY)
    {
        if (memchr(c->input + c->position, '\0', length))
        { es_ck_fail(c, ES_CK_STATE); return; }
        if (c->mode == ES_CK_APPLY)
        {
            memcpy(r->results, c->input + c->position, length);
            r->results[length] = '\0';
        }
    }
    c->position += length;
}

void es_ck_lid(EsCkCursor *c)
{
    int i, j, k, sat;
    uint32_t count;
    TLidProc *p;
    TLidGroup g;
    TLidList *item;
    TLidUnit *u;
    TGrnAmpt *a;
    TWaterBalance *w;
    TLidRptFile *r;
    if (c->error) return;
    /* Failed or not-yet-flushed reports are not a completed success boundary.
       The coordinator also checks the engine's lifecycle/step boundary. */
    if (LidReportFailed || LidReportOwnError || LidDirtyReports ||
        LidCount < 0 || GroupCount != Nobjects[SUBCATCH] ||
        (LidCount && !LidProcs) || (GroupCount && !LidGroups))
    { es_ck_fail(c, ES_CK_STATE); return; }
    es_ck_tag(c, "ESLID001", 8);
    es_ck_fixed_i32(c, LidCount); es_ck_fixed_i32(c, GroupCount);
    es_ck_fixed_i32(c, Nobjects[POLLUT]);
    es_ck_fixed_i32(c, UnitSystem); es_ck_fixed_i32(c, FlowUnits);
    es_ck_fixed_i32(c, IgnoreRainfall);
    for (i = 0; i < Nobjects[POLLUT]; i++) es_ck_identity(c, Pollut[i].ID);
    for (i = 0; i < LidCount && !c->error; i++)
    {
        p = &LidProcs[i]; es_ck_identity(c, p->ID); ES_LI(p, lidType);
        ES_LF(p, surface.thickness); ES_LF(p, surface.voidFrac);
        ES_LF(p, surface.roughness); ES_LF(p, surface.surfSlope);
        ES_LF(p, surface.sideSlope); ES_LF(p, surface.alpha);
        ES_LI(p, surface.canOverflow);
        ES_LF(p, pavement.thickness); ES_LF(p, pavement.voidFrac);
        ES_LF(p, pavement.impervFrac); ES_LF(p, pavement.kSat);
        ES_LF(p, pavement.clogFactor); ES_LF(p, pavement.regenDays);
        ES_LF(p, pavement.regenDegree);
        ES_LF(p, soil.thickness); ES_LF(p, soil.porosity);
        ES_LF(p, soil.fieldCap); ES_LF(p, soil.wiltPoint);
        ES_LF(p, soil.suction); ES_LF(p, soil.kSat); ES_LF(p, soil.kSlope);
        ES_LF(p, storage.thickness); ES_LF(p, storage.voidFrac);
        ES_LF(p, storage.kSat); ES_LF(p, storage.clogFactor); ES_LI(p, storage.covered);
        ES_LF(p, drain.coeff); ES_LF(p, drain.expon); ES_LF(p, drain.offset);
        ES_LF(p, drain.delay); ES_LF(p, drain.hOpen); ES_LF(p, drain.hClose);
        ES_LI(p, drain.qCurve);
        /* Absent DRAIN rows leave an inert calloc-zero qCurve, not a reference.
           Bind the value, and resolve an identity only for a consumed curve. */
        if (p->drain.coeff > 0.0 && p->lidType != ROOF_DISCON && p->drain.qCurve >= 0)
        {
            if (p->drain.qCurve >= Nobjects[CURVE])
            { es_ck_fail(c, ES_CK_STATE); return; }
            es_ck_identity(c, Curve[p->drain.qCurve].ID);
        }
        ES_LF(p, drainMat.thickness); ES_LF(p, drainMat.voidFrac);
        ES_LF(p, drainMat.roughness); ES_LF(p, drainMat.alpha);
        if (Nobjects[POLLUT] && !p->drainRmvl)
        { es_ck_fail(c, ES_CK_STATE); return; }
        for (j = 0; j < Nobjects[POLLUT]; j++) es_ck_fixed_f64(c, p->drainRmvl[j]);
    }
    for (j = 0; j < GroupCount && !c->error; j++)
    {
        es_ck_identity(c, Subcatch[j].ID);
        es_ck_fixed_f64(c, Subcatch[j].area);
        es_ck_fixed_f64(c, Subcatch[j].lidArea);
        g = LidGroups[j]; es_ck_fixed_i32(c, g != NULL);
        if (!g) continue;
        ES_LF(g, pervArea);
        ES_LD(g, flowToPerv); ES_LD(g, oldDrainFlow); ES_LD(g, newDrainFlow);
        count = 0;
        for (item = g->lidList; item; item = item->nextLidUnit)
        {
            if (!item->lidUnit || count == UINT32_MAX)
            { es_ck_fail(c, ES_CK_STATE); return; }
            count++;
        }
        es_ck_fixed_u32(c, count);
        for (item = g->lidList; item && !c->error; item = item->nextLidUnit)
        {
            u = item->lidUnit; k = u->lidIndex;
            if (k < 0 || k >= LidCount) { es_ck_fail(c, ES_CK_STATE); return; }
            ES_LI(u, lidIndex); ES_LI(u, number);
            ES_LF(u, area); ES_LF(u, fullWidth);
            /* botWidth is an unused, uninitialized legacy member. Swale
               geometry uses a separate local variable in lidproc.c. */
            ES_LF(u, initSat); ES_LF(u, fromImperv); ES_LF(u, fromPerv);
            ES_LI(u, toPerv); ES_LI(u, drainSubcatch); ES_LI(u, drainNode);
            if (u->drainSubcatch >= 0)
            {
                if (u->drainSubcatch >= GroupCount) { es_ck_fail(c, ES_CK_STATE); return; }
                es_ck_identity(c, Subcatch[u->drainSubcatch].ID);
            }
            if (u->drainNode >= 0)
            {
                if (u->drainNode >= Nobjects[NODE]) { es_ck_fail(c, ES_CK_STATE); return; }
                es_ck_identity(c, Node[u->drainNode].ID);
            }
            a = &u->soilInfil; ES_LF(a, Ks);
            if (a->Ks > 0.0)
            {
                ES_LF(a, S); ES_LF(a, IMDmax); ES_LF(a, Lu);
                ES_LD(a, IMD); ES_LD(a, F); ES_LD(a, Fu); ES_LD(a, T);
                sat = a->Sat; es_ck_i32(c, &sat, 0, 1);
                if (!c->error && c->mode == ES_CK_APPLY) a->Sat = (char)sat;
            }
            ES_LD(u, surfaceDepth); ES_LD(u, paveDepth);
            ES_LD(u, soilMoisture); ES_LD(u, storageDepth);
            for (i = 0; i < MAX_LAYERS; i++) es_ck_f64(c, &u->oldFluxRates[i]);
            ES_LD(u, dryTime); ES_LD(u, oldDrainFlow); ES_LD(u, newDrainFlow);
            ES_LD(u, volTreated); ES_LD(u, nextRegenDay);
            w = &u->waterBalance;
            ES_LD(w, inflow); ES_LD(w, evap); ES_LD(w, infil);
            ES_LD(w, surfFlow); ES_LD(w, drainFlow); ES_LD(w, initVol); ES_LD(w, finalVol);
            r = u->rptFile; es_ck_fixed_i32(c, r != NULL);
            if (r)
            {
                if (r->failed || r->dirty || r->dirtyNext ||
                    ((c->mode == ES_CK_MEASURE || c->mode == ES_CK_WRITE) && !r->file))
                { es_ck_fail(c, ES_CK_STATE); return; }
                es_ck_lid_row(c, r);
            }
        }
    }
}

#undef ES_LF
#undef ES_LI
#undef ES_LD
