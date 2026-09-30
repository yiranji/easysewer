/* Appended to swmm5.c. ES_CK_CUSTOM is supplied by the development recipe. */
#include "es_checkpoint.h"
#include <limits.h>

int es_ck_engine_boundary(void)
{
    if (!IsOpenFlag || !IsStartedFlag || ErrorCode || ExceptionCount)
        return ES_CK_STATE;
#if ES_CK_CUSTOM
    if (PondingPending) return ES_CK_STATE;
#endif
    /* A stride restores its original duration before returning to its caller.
     * Caller serialization also forbids capture inside any native API call. */
    if (RoutingDuration != TotalDuration) return ES_CK_STATE;
    return ES_CK_OK;
}

void es_ck_engine(EsCkCursor *c)
{
    double report = ReportTime, old_runoff = OldRunoffTime, runoff = NewRunoffTime;
    double old_routing = OldRoutingTime, routing = NewRoutingTime, elapsed = ElapsedTime;
    double route_step = RouteStep, courant = CourantFactor;
    long total = TotalStepCount, reported = ReportStepCount, nonconverge = NonConvergeCount;
    int warnings = Warnings;
    uint64_t total_wire, reported_wire, nonconverge_wire;
    if (es_ck_engine_boundary()) { es_ck_fail(c, ES_CK_STATE); return; }
    es_ck_tag(c, "ESCLK001", 8);
    es_ck_fixed_i32(c, ES_CK_CUSTOM);
    es_ck_fixed_i32(c, SaveResultsFlag);
    es_ck_fixed_i32(c, DoRunoff);
    es_ck_fixed_i32(c, DoRouting);
    es_ck_fixed_f64(c, StartDateTime);
    es_ck_fixed_f64(c, EndDateTime);
    es_ck_fixed_f64(c, ReportStart);
    es_ck_fixed_f64(c, TotalDuration);
    es_ck_fixed_i32(c, ReportStep);
    es_ck_fixed_i32(c, WetStep);
    es_ck_fixed_i32(c, DryStep);
    es_ck_fixed_i32(c, RuleStep);
    es_ck_fixed_i32(c, RptFlags.averages);
    report = es_ck_f64_value(c, &report);
    old_runoff = es_ck_f64_value(c, &old_runoff);
    runoff = es_ck_f64_value(c, &runoff);
    old_routing = es_ck_f64_value(c, &old_routing);
    routing = es_ck_f64_value(c, &routing);
    elapsed = es_ck_f64_value(c, &elapsed);
    route_step = es_ck_f64_value(c, &route_step);
    courant = es_ck_f64_value(c, &courant);
    total_wire = es_ck_count(c, &total);
    reported_wire = es_ck_count(c, &reported);
    nonconverge_wire = es_ck_count(c, &nonconverge);
    warnings = es_ck_i32(c, &warnings, 0, INT_MAX);
    if (report <= 0.0 || runoff < 0.0 || runoff > TotalDuration + 1.0 ||
        routing < 0.0 || routing > TotalDuration + 1.0 ||
        elapsed < 0.0 || elapsed > TotalDuration / MSECperDAY + 1.0 / SECperDAY ||
        route_step <= 0.0 || courant < 0.0 ||
        reported_wire > total_wire || nonconverge_wire > total_wire)
        es_ck_fail(c, ES_CK_STATE);
    /* OldRunoffTime can contain an unused previous-owner value at start.
     * Candidate clocks now reset/advance OldRoutingTime even without routing;
     * constrain its ordering whenever the shared routing clock has advanced. */
    if ((DoRunoff && runoff > 0.0 && (old_runoff < 0.0 || old_runoff > runoff)) ||
        (total_wire > 0 && (old_routing < 0.0 || old_routing > routing)))
        es_ck_fail(c, ES_CK_STATE);
    /* Do not collapse hydrology and routing clocks, or equate ElapsedTime
     * with routing: the custom split-step API updates it during controls. */
    es_ck_routing(c, DoRouting, routing);
    if (!c->error && c->mode == ES_CK_APPLY)
    {
        ReportTime = report;
        OldRunoffTime = old_runoff; NewRunoffTime = runoff;
        OldRoutingTime = old_routing; NewRoutingTime = routing;
        ElapsedTime = elapsed; RouteStep = route_step; CourantFactor = courant;
        TotalStepCount = total; ReportStepCount = reported;
        NonConvergeCount = nonconverge; Warnings = warnings;
    }
}
