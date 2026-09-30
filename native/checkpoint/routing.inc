/* Appended to routing.c; sorting/event layout is rebuilt from identical INP. */
#include "es_checkpoint.h"

void es_ck_routing(EsCkCursor *c, int active, double routing_time)
{
    int j, next = NextEvent, between = BetweenEvents;
    double rule = NewRuleTime;
    es_ck_tag(c, "ESROUT01", 8);
    es_ck_fixed_i32(c, active);
    if (!active || c->error) return;
    if (Nobjects[LINK] < 0 || NumEvents < 0 ||
        (Nobjects[LINK] && !SortedLinks) || (NumEvents && !Event))
    {
        es_ck_fail(c, ES_CK_STATE);
        return;
    }
    es_ck_fixed_i32(c, RouteModel);
    es_ck_fixed_i32(c, RuleStep);
    es_ck_fixed_i32(c, Nobjects[LINK]);
    for (j = 0; j < Nobjects[LINK] && !c->error; j++)
    {
        es_ck_fixed_i32(c, SortedLinks[j]);
        es_ck_identity(c, Link[j].ID);
    }
    es_ck_fixed_i32(c, NumEvents);
    /* The terminal sentinel at NumEvents is a real, reachable event slot. */
    if (NumEvents) for (j = 0; j <= NumEvents && !c->error; j++)
    {
        es_ck_fixed_f64(c, Event[j].start);
        es_ck_fixed_f64(c, Event[j].end);
    }
    next = es_ck_i32(c, &next, 0, NumEvents);
    between = es_ck_i32(c, &between, FALSE, TRUE);
    rule = es_ck_f64_value(c, &rule);
    if ((!NumEvents && (next || between)) || rule < 0.0 ||
        rule > routing_time + 1.0 || (RuleStep == 0 && rule != 0.0))
        es_ck_fail(c, ES_CK_STATE);
    if (!c->error && c->mode == ES_CK_APPLY)
    {
        NextEvent = next;
        BetweenEvents = between;
        NewRuleTime = rule;
    }
}
