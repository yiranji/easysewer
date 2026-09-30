/* Appended to rdii.c. Headers/arrays are reconstructed from verified resources
 * before restoring. UH convolution has finished and freed its workspace at
 * the allowed post-start capture boundary. Physical stream is a separate block. */
#include "es_checkpoint.h"

void es_ck_rdii(EsCkCursor *c, int active)
{
    int i, eof;
    double start, end, previous, value;
    float converted;
    if (c->error) return;
    es_ck_tag(c, "ESRDI001", 8);
    es_ck_fixed_i32(c, active);
    es_ck_fixed_i32(c, IgnoreRainfall);
    es_ck_fixed_i32(c, IgnoreRDII);
    if (c->error || !active) return;
    if (UHGroup || RdiiWriting || NumRdiiNodes <= 0 ||
        NumRdiiNodes > Nobjects[NODE] || !RdiiNodeIndex || !RdiiNodeFlow ||
        !RdiiPendingFlow || RdiiStep <= 0 ||
        (RdiiFileType != BINARY && RdiiFileType != TEXT))
    { es_ck_fail(c, ES_CK_STATE); return; }
    es_ck_fixed_i32(c, RdiiFileType);
    es_ck_fixed_i32(c, RdiiStep);
    es_ck_fixed_i32(c, NumRdiiNodes);
    /* Binary frames are already cfs; their flow-unit global is unused/stale. */
    if (RdiiFileType == TEXT) es_ck_fixed_i32(c, RdiiFlowUnits);
    for (i = 0; i < NumRdiiNodes && !c->error; i++)
    {
        if (RdiiNodeIndex[i] < 0 || RdiiNodeIndex[i] >= Nobjects[NODE])
        { es_ck_fail(c, ES_CK_STATE); return; }
        es_ck_fixed_i32(c, RdiiNodeIndex[i]);
        es_ck_identity(c, Node[RdiiNodeIndex[i]].ID);
    }
    eof = RdiiEof; start = RdiiStartDate; end = RdiiEndDate; previous = RdiiPreviousDate;
    eof = es_ck_i32(c, &eof, 0, 1);
    start = es_ck_f64_value(c, &start); end = es_ck_f64_value(c, &end);
    previous = es_ck_f64_value(c, &previous);
    if ((previous != NO_DATE && !rdiiDateValid(previous)) ||
        (eof && (start != NO_DATE || end != NO_DATE)) ||
        (!eof && (!rdiiDateValid(start) || !rdiiDateValid(end) ||
         previous != start || end != datetime_addSeconds(start, RdiiStep) || end <= start)))
        es_ck_fail(c, ES_CK_NUMBER);
    for (i = 0; i < NumRdiiNodes && !c->error; i++)
    {
        value = RdiiNodeFlow[i]; value = es_ck_f64_value(c, &value);
        /* Binary32 values widen exactly. Reject payloads that would round or
         * overflow on restore, rather than silently accepting different data. */
        if (fabs(value) > FLT_MAX) { es_ck_fail(c, ES_CK_NUMBER); break; }
        converted = (float)value;
        if ((double)converted != value) { es_ck_fail(c, ES_CK_NUMBER); break; }
        if (!c->error && c->mode == ES_CK_APPLY) RdiiNodeFlow[i] = converted;
    }
    if (!c->error && c->mode == ES_CK_APPLY)
    {
        RdiiEof = eof; RdiiStartDate = start; RdiiEndDate = end; RdiiPreviousDate = previous;
    }
    /* PendingFlow is fully overwritten before the next successful publish.
     * TotalRainVol/TotalRdiiVol are generation/report-only at this boundary. */
}
