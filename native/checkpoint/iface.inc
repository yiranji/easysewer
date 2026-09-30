/* Appended to iface.c. Header names, mappings and allocation shapes are rebuilt
 * from content-verified input. Output line accounting belongs to output state. */
#include "es_checkpoint.h"

void es_ck_iface(EsCkCursor *c, int active)
{
    int i, j, eof;
    double old, next, previous, fraction;
    uint64_t bytes;
    if (c->error) return;
    es_ck_tag(c, "ESIFC001", 8);
    es_ck_fixed_i32(c, active);
    es_ck_fixed_i32(c, Nobjects[NODE]);
    es_ck_fixed_i32(c, Nobjects[POLLUT]);
    if (c->error || !active) return;
    if (NumIfaceNodes <= 0 || NumIfacePolluts < 0 || NumIfacePolluts >= MAXLINE-8 ||
        !IfaceNodes || !IfaceNodeNames || !IfacePollutNames ||
        !OldIfaceValues || !NewIfaceValues || !IfacePendingValues ||
        (Nobjects[POLLUT] && !IfacePolluts) || IfaceStep <= 0)
    { es_ck_fail(c, ES_CK_STATE); return; }
    es_ck_fixed_i32(c, IfaceStep); es_ck_fixed_i32(c, IfaceFlowUnits);
    es_ck_fixed_i32(c, NumIfaceNodes); es_ck_fixed_i32(c, NumIfacePolluts);
    bytes = IfaceFileBytes;
    if (es_ck_u64(c, &bytes) != IfaceFileBytes) es_ck_fail(c, ES_CK_MISMATCH);
    for (i = 0; i <= NumIfacePolluts && !c->error; i++) es_ck_identity(c, IfacePollutNames[i]);
    for (i = 0; i < Nobjects[POLLUT] && !c->error; i++)
    {
        es_ck_identity(c, Pollut[i].ID);
        es_ck_fixed_i32(c, Pollut[i].units);
        es_ck_fixed_i32(c, IfacePolluts[i]);
    }
    for (i = 0; i < NumIfaceNodes && !c->error; i++)
    {
        es_ck_identity(c, IfaceNodeNames[i]); es_ck_fixed_i32(c, IfaceNodes[i]);
        if (IfaceNodes[i] < -1 || IfaceNodes[i] >= Nobjects[NODE] ||
            !OldIfaceValues[i] || !NewIfaceValues[i] || !IfacePendingValues[i])
        { es_ck_fail(c, ES_CK_STATE); return; }
        if (IfaceNodes[i] >= 0) es_ck_identity(c, Node[IfaceNodes[i]].ID);
    }
    eof = IfaceEof; old = OldIfaceDate; next = NewIfaceDate;
    previous = IfacePreviousDate; fraction = IfaceFrac;
    eof = es_ck_i32(c, &eof, 0, 1);
    old = es_ck_f64_value(c, &old); next = es_ck_f64_value(c, &next);
    previous = es_ck_f64_value(c, &previous); fraction = es_ck_f64_value(c, &fraction);
    if (old < NO_DATE+1.0 || old >= 2958466.0 || fraction < 0.0 || fraction > 1.0 ||
        (eof && (next != NO_DATE || previous != old)) ||
        (!eof && (next < old || next >= 2958466.0 || previous != next)))
        es_ck_fail(c, ES_CK_NUMBER);
    for (i = 0; i < NumIfaceNodes && !c->error; i++)
        for (j = 0; j <= NumIfacePolluts && !c->error; j++)
        {
            es_ck_f64(c, &OldIfaceValues[i][j]);
            es_ck_f64(c, &NewIfaceValues[i][j]);
        }
    if (!c->error && c->mode == ES_CK_APPLY)
    {
        IfaceEof = eof; OldIfaceDate = old; NewIfaceDate = next;
        IfacePreviousDate = previous; IfaceFrac = fraction;
    }
    /* PendingValues are all written before publishing a complete new frame.
     * Preserve both old/new numeric frames even after logical EOF. */
}
