/* Appended to output.c. Numeric owner only: the coordinator must separately
 * stage the matching OUT prefix before committing Nperiods and these buffers. */
#include "es_checkpoint.h"
#include <float.h>

typedef char es_ck_output_float_width[(sizeof(REAL4) == 4 && FLT_MANT_DIG == 24 &&
                                      FLT_MAX_EXP == 128) ? 1 : -1];

static void es_ck_output_offset(EsCkCursor *c, F_OFF offset)
{
    uint64_t value = (uint64_t)offset;
    if (offset < 0) { es_ck_fail(c, ES_CK_STATE); return; }
    if (es_ck_u64(c, &value) != (uint64_t)offset)
        es_ck_fail(c, ES_CK_MISMATCH);
}

static void es_ck_output_float(EsCkCursor *c, REAL4 *slot)
{
    double value = *slot;
    REAL4 converted;
    value = es_ck_f64_value(c, &value);
    if (c->error) return;
    if (fabs(value) > FLT_MAX) { es_ck_fail(c, ES_CK_NUMBER); return; }
    converted = (REAL4)value;
    if ((double)converted != value) { es_ck_fail(c, ES_CK_NUMBER); return; }
    if (c->mode == ES_CK_APPLY) *slot = converted;
}

void es_ck_output(EsCkCursor *c)
{
    int i, j;
    uint64_t periods;
    if (c->error) return;
    if (!OutReady || OutFailed || OutEnded ||
        !SubcatchResults || !NodeResults || !LinkResults)
    { es_ck_fail(c, ES_CK_STATE); return; }
    es_ck_tag(c, "ESOUT001", 8);
    es_ck_fixed_i32(c, UnitSystem);
    es_ck_fixed_i32(c, FlowUnits);
    es_ck_fixed_i32(c, IgnoreQuality);
    es_ck_fixed_i32(c, RptFlags.averages);
    es_ck_fixed_i32(c, ReportStep);
    es_ck_fixed_f64(c, ReportStart);
    es_ck_fixed_i32(c, NumSubcatchVars);
    es_ck_fixed_i32(c, NumNodeVars);
    es_ck_fixed_i32(c, NumLinkVars);
    es_ck_fixed_i32(c, NumPolluts);
    es_ck_fixed_i32(c, NumSubcatch);
    es_ck_fixed_i32(c, NumNodes);
    es_ck_fixed_i32(c, NumLinks);
    es_ck_output_offset(c, IDStartPos);
    es_ck_output_offset(c, InputStartPos);
    es_ck_output_offset(c, OutputStartPos);
    es_ck_output_offset(c, BytesPerPeriod);
    /* Bind the full ordered mapping, including unreported objects. */
    es_ck_fixed_i32(c, Nobjects[SUBCATCH]);
    es_ck_fixed_i32(c, Nobjects[NODE]);
    es_ck_fixed_i32(c, Nobjects[LINK]);
    for (i=0; i<Nobjects[SUBCATCH]; i++)
    {
        es_ck_identity(c, Subcatch[i].ID);
        es_ck_fixed_i32(c, Subcatch[i].rptFlag);
    }
    for (i=0; i<Nobjects[NODE]; i++)
    {
        es_ck_identity(c, Node[i].ID);
        es_ck_fixed_i32(c, Node[i].rptFlag);
    }
    for (i=0; i<Nobjects[LINK]; i++)
    {
        es_ck_identity(c, Link[i].ID);
        es_ck_fixed_i32(c, Link[i].rptFlag);
        es_ck_fixed_i32(c, Link[i].type);
    }
    for (i=0; i<NumPolluts; i++)
    {
        es_ck_identity(c, Pollut[i].ID);
        es_ck_fixed_i32(c, Pollut[i].units);
    }
    periods = es_ck_count(c, &Nperiods);
    if (periods > INT32_MAX || BytesPerPeriod < 8 || OutputStartPos < 0 ||
        periods > (uint64_t)((INT64_MAX-OutputStartPos-24)/BytesPerPeriod))
        es_ck_fail(c, ES_CK_NUMBER);
    es_ck_i32(c, &Nsteps, 0, RptFlags.averages ? INT_MAX : 0);
    if (c->error || !RptFlags.averages) return;
    if ((NumNodes && !AvgNodeResults) || (NumLinks && !AvgLinkResults))
    { es_ck_fail(c, ES_CK_STATE); return; }
    for (i=0; i<NumNodes && !c->error; i++)
    {
        if (!AvgNodeResults[i].xAvg) { es_ck_fail(c, ES_CK_STATE); return; }
        for (j=0; j<NumNodeVars; j++) es_ck_output_float(c, &AvgNodeResults[i].xAvg[j]);
    }
    for (i=0; i<NumLinks && !c->error; i++)
    {
        if (!AvgLinkResults[i].xAvg) { es_ck_fail(c, ES_CK_STATE); return; }
        for (j=0; j<NumLinkVars; j++) es_ck_output_float(c, &AvgLinkResults[i].xAvg[j]);
    }
    /* Subcatch/Node/LinkResults and SysResults are scratch: queries reread
     * their record, and writers compute them anew. FILE position is separate. */
}
