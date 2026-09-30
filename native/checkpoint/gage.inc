/* Appended to gage.c. Table owners and physical file handles are separate. */
#include "es_checkpoint.h"

void es_ck_gage(EsCkCursor *c)
{
    int j, k;
    uint64_t position;
    TGage *p;
    char resource_key[64];
    es_ck_tag(c, "ESGAG001", 8);
    es_ck_fixed_i32(c, Nobjects[GAGE]);
    es_ck_fixed_i32(c, MAXPASTRAIN);
    es_ck_fixed_i32(c, IgnoreRainfall);
    es_ck_fixed_i32(c, UnitSystem);
    if (c->error) return;
    if (Nobjects[GAGE] && !Gage)
    { es_ck_fail(c, ES_CK_STATE); return; }
    for (j = 0; j < Nobjects[GAGE] && !c->error; j++)
    {
        p = &Gage[j];
        es_ck_identity(c, p->ID);
        es_ck_fixed_i32(c, p->dataSource);
        es_ck_fixed_i32(c, p->tSeries);
        es_ck_fixed_i32(c, p->rainType);
        es_ck_fixed_i32(c, p->rainInterval);
        es_ck_fixed_i32(c, p->rainUnits);
        es_ck_fixed_i32(c, p->coGage);
        es_ck_fixed_i32(c, p->isUsed);
        es_ck_fixed_f64(c, p->snowFactor);
        es_ck_fixed_f64(c, p->unitsFactor);
        if (p->dataSource == RAIN_FILE)
        {
            snprintf(resource_key, sizeof(resource_key), "swmm:rain-source:%d", j);
            es_ck_identity(c, c->logical_resources ? resource_key : p->fname);
            es_ck_identity(c, p->staID);
            es_ck_fixed_f64(c, p->startFileDate);
            es_ck_fixed_f64(c, p->endFileDate);
            /* The qualified rain reader caps all interface offsets at INT_MAX
               and uses exactly 12 bytes per record (f64 date, f32 depth). */
            if (p->startFilePos < 0 || p->endFilePos < p->startFilePos ||
                p->endFilePos > INT_MAX ||
                (p->endFilePos - p->startFilePos) % 12 != 0)
            { es_ck_fail(c, ES_CK_STATE); return; }
            es_ck_fixed_u32(c, (uint32_t)p->startFilePos);
            es_ck_fixed_u32(c, (uint32_t)p->endFilePos);
            position = es_ck_count(c, &p->currentFilePos);
            if (position < (uint64_t)p->startFilePos ||
                position > (uint64_t)p->endFilePos ||
                (position - (uint64_t)p->startFilePos) % 12 != 0)
                es_ck_fail(c, ES_CK_NUMBER);
        }
        else if (p->dataSource != RAIN_TSERIES)
        { es_ck_fail(c, ES_CK_STATE); return; }
        es_ck_f64(c, &p->rainAccum);
        es_ck_f64(c, &p->startDate);
        es_ck_f64(c, &p->endDate);
        es_ck_f64(c, &p->nextDate);
        es_ck_f64(c, &p->rainfall);
        es_ck_f64(c, &p->nextRainfall);
        es_ck_f64(c, &p->apiRainfall);
        es_ck_f64(c, &p->reportRainfall);
        for (k = 0; k <= MAXPASTRAIN; k++) es_ck_f64(c, &p->pastRain[k]);
        /* Normal accumulation can leave precisely 3600 here until next time. */
        es_ck_i32(c, &p->pastInterval, 0, 3600);
        /* isCurrent is RDII scratch, reset for every rainfall time step. */
    }
}
