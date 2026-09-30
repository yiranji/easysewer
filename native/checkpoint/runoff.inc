/* Appended to runoff.c. Sequential file positions belong to the resource owner. */
#include "es_checkpoint.h"

void es_ck_runoff(EsCkCursor *c, int active)
{
    int j, runoff, snow, wet, steps;
    double replay_time;
    uint64_t items;
    es_ck_tag(c, "ESRUN001", 8);
    es_ck_fixed_i32(c, active);
    if (c->error || !active) return;
    es_ck_fixed_i32(c, Frunoff.mode);
    es_ck_fixed_i32(c, FlowUnits);
    es_ck_fixed_i32(c, Nobjects[SUBCATCH]);
    es_ck_fixed_i32(c, Nobjects[POLLUT]);
    es_ck_fixed_i32(c, Nobjects[GAGE]);
    es_ck_fixed_i32(c, ReplayRainActive);
    if (Frunoff.mode == USE_FILE || Frunoff.mode == SAVE_FILE)
    {
        /* Reconstructed layout, not mutable per-frame state. */
        if (Nobjects[POLLUT] > INT_MAX - (MAX_SUBCATCH_RESULTS - 1) ||
            RunoffResults != MAX_SUBCATCH_RESULTS - 1 + Nobjects[POLLUT] ||
            MaxStepsPos != RUNOFF_STAMP_SIZE + 3 * sizeof(int) ||
            ((c->mode == ES_CK_MEASURE || c->mode == ES_CK_WRITE) && !Frunoff.file) || !RunoffFrame)
        { es_ck_fail(c, ES_CK_STATE); return; }
        items = 1 + (uint64_t)Nobjects[SUBCATCH] * (uint64_t)RunoffResults;
        if (items != RunoffItems)
        { es_ck_fail(c, ES_CK_STATE); return; }
        es_ck_fixed_i32(c, RunoffResults);
        es_ck_fixed_i32(c, (int)MaxStepsPos);
        if (Frunoff.mode == USE_FILE) es_ck_fixed_i32(c, MaxSteps);
    }
    runoff = HasRunoff; snow = HasSnow; wet = HasWetLids; steps = Nsteps;
    runoff = es_ck_i32(c, &runoff, 0, 1);
    snow = es_ck_i32(c, &snow, 0, 1);
    wet = es_ck_i32(c, &wet, 0, 1);
    steps = es_ck_i32(c, &steps, 0, Frunoff.mode == USE_FILE ? MaxSteps : INT_MAX);
    if (!c->error && c->mode == ES_CK_APPLY)
    {
        HasRunoff = (char)runoff; HasSnow = (char)snow;
        HasWetLids = (char)wet; Nsteps = steps;
    }
    if (ReplayRainActive)
    {
        if (Frunoff.mode != USE_FILE || (Nobjects[GAGE] && !ReplayRawRain))
        { es_ck_fail(c, ES_CK_STATE); return; }
        replay_time = ReplayRainTime;
        replay_time = es_ck_f64_value(c, &replay_time);
        if (replay_time < 0.0 || replay_time > TotalDuration)
            es_ck_fail(c, ES_CK_NUMBER);
        for (j = 0; j < Nobjects[GAGE] && !c->error; j++)
        {
            es_ck_identity(c, Gage[j].ID);
            es_ck_f64(c, &ReplayRawRain[j]);
        }
        if (!c->error && c->mode == ES_CK_APPLY) ReplayRainTime = replay_time;
    }
    /* IsRaining is selected again before step selection. OutflowLoad is zeroed
       for each catchment washoff. RunoffFrame is fully filled by each read or
       save. The resource owner must stage the matching file cursor/output prefix
       before applying Nsteps; applying this block alone is not resume. */
}
