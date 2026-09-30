/* Appended to the verified controls.c: owns its private action structures. */
#include "es_checkpoint.h"

static void es_ck_control_actions(EsCkCursor *c, struct TAction *a)
{
    struct TAction *item;
    uint32_t count = 0;
    for (item = a; item; item = item->next)
    {
        if (count == UINT32_MAX) { es_ck_fail(c, ES_CK_BOUNDS); return; }
        count++;
    }
    es_ck_fixed_u32(c, count);
    for (; a && !c->error; a = a->next)
    {
        es_ck_fixed_i32(c, a->rule);
        es_ck_fixed_i32(c, a->link);
        es_ck_fixed_i32(c, a->attribute);
        es_ck_fixed_i32(c, a->curve);
        es_ck_fixed_i32(c, a->tseries);
        if (a->attribute == r_PID)
        {
            es_ck_fixed_f64(c, a->kp);
            es_ck_fixed_f64(c, a->ki);
            es_ck_fixed_f64(c, a->kd);
            es_ck_f64(c, &a->value);
            es_ck_f64(c, &a->e1);
            es_ck_f64(c, &a->e2);
        }
        else if (a->curve >= 0 || a->tseries >= 0) es_ck_f64(c, &a->value);
        else es_ck_fixed_f64(c, a->value);
        /* Non-PID coefficients/error fields are uninitialized in SWMM.
         * Never read them or emit allocator contents in a checkpoint. */
    }
}

void es_ck_controls(EsCkCursor *c, EsCkControlsStage *stage)
{
    int r;
    uint32_t capacity = 0;
    struct TActionList *item;
    if (RuleCount < 0 || VariableCount < 0 || ExpressionCount < 0 ||
        (RuleCount && !Rules) || (VariableCount && !NamedVariable) ||
        (ExpressionCount && !Expression))
        es_ck_fail(c, ES_CK_STATE);
    if ((c->mode == ES_CK_VALIDATE && (!stage || stage->prepared || stage->actions)) ||
        (c->mode == ES_CK_APPLY && (!stage || !stage->prepared || !stage->validated)))
        es_ck_fail(c, ES_CK_STATE);
    if (c->error) return;
    if (c->mode == ES_CK_VALIDATE) stage->validated = 0;

    es_ck_tag(c, "ESCTRL01", 8);
    es_ck_fixed_i32(c, RuleCount);
    es_ck_fixed_i32(c, VariableCount);
    es_ck_fixed_i32(c, ExpressionCount);
    for (item = ActionList; item; item = item->next)
    {
        if (capacity == UINT32_MAX) { es_ck_fail(c, ES_CK_BOUNDS); return; }
        capacity++;
    }
    capacity = es_ck_u32(c, &capacity);
    if (Nobjects[LINK] < 0 || capacity > (uint32_t)Nobjects[LINK])
        es_ck_fail(c, ES_CK_STATE);
    if (c->mode == ES_CK_APPLY && capacity != stage->capacity)
        es_ck_fail(c, ES_CK_STATE);

    /* Time EQ/NE and MISSING premises can leave these across evaluations. */
    es_ck_f64(c, &ControlValue);
    es_ck_f64(c, &SetPoint);
    es_ck_f64(c, &CurrentDate);
    es_ck_f64(c, &CurrentTime);
    for (r = 0; r < RuleCount && !c->error; r++)
    {
        es_ck_identity(c, Rules[r].ID);
        es_ck_fixed_f64(c, Rules[r].priority);
        es_ck_control_actions(c, Rules[r].thenActions);
        es_ck_control_actions(c, Rules[r].elseActions);
    }
    if (!c->error && c->mode == ES_CK_VALIDATE)
    {
        stage->capacity = capacity;
        stage->validated = 1;
    }
    if (!c->error && c->mode == ES_CK_APPLY)
    {
        /* Next evaluation clears all entries, but retains the allocation
         * count. That count influences distinct-link execution/report order.
         * Preserve it; no action is still pending after a completed step. */
        deleteActionList();
        ActionList = stage->actions;
        stage->actions = NULL;
        stage->prepared = stage->validated = 0;
    }
}

void es_ck_controls_discard(EsCkControlsStage *stage)
{
    struct TActionList *item, *next;
    if (!stage) return;
    for (item = stage->actions; item; item = next)
    {
        next = item->next;
        free(item);
    }
    memset(stage, 0, sizeof(*stage));
}

int es_ck_controls_prepare(EsCkControlsStage *stage)
{
    uint32_t i;
    struct TActionList *item;
    if (!stage || !stage->validated || stage->prepared || stage->actions)
        return ES_CK_STATE;
    for (i = 0; i < stage->capacity; i++)
    {
        item = calloc(1, sizeof(*item));
        if (!item)
        {
            es_ck_controls_discard(stage);
            return ES_CK_MEMORY;
        }
        item->next = stage->actions;
        stage->actions = item;
    }
    stage->prepared = 1;
    return ES_CK_OK;
}
