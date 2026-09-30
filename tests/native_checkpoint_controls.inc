/* Test-only exports appended after controls.inc. Never shipped in a solver. */
#include "swmm5.h"
static int es_test_fail_at = -1, es_test_alloc_index = 0, es_test_staging = 0;
void *es_test_stage_calloc(size_t n, size_t size)
{
    if (es_test_staging && es_test_alloc_index++ == es_test_fail_at) return NULL;
    return calloc(n, size);
}

int es_test_prepare_controls(EsCkControlsStage *stage, int fail_at)
{
    int error;
    es_test_fail_at = fail_at;
    es_test_alloc_index = 0;
    es_test_staging = 1;
    error = es_ck_controls_prepare(stage);
    es_test_staging = 0;
    return error;
}

int DLLEXPORT es_test_controls_save(void *data, size_t size, size_t *used)
{
    EsCkCursor c;
    es_ck_init(&c, data ? ES_CK_WRITE : ES_CK_MEASURE, NULL, data, size);
    es_ck_controls(&c, NULL);
    *used = c.position;
    return es_ck_finish(&c);
}

int DLLEXPORT es_test_controls_restore(const void *data, size_t size, int fail_at)
{
    EsCkCursor c;
    EsCkControlsStage stage = {0};
    unsigned char *owned;
    int error;
    /* A future complete checkpoint reader enforces its own total budgets. */
    if (!data || size > 16*1024*1024) return ES_CK_ARGUMENT;
    owned = malloc(size ? size : 1);
    if (!owned) return ES_CK_MEMORY;
    memcpy(owned, data, size);
    es_ck_init(&c, ES_CK_VALIDATE, owned, NULL, size);
    es_ck_controls(&c, &stage);
    error = es_ck_finish(&c);
    if (!error)
    {
        es_test_fail_at = fail_at;
        es_test_alloc_index = 0;
        es_test_staging = 1;
        error = es_ck_controls_prepare(&stage);
        es_test_staging = 0;
    }
    if (!error)
    {
        es_ck_init(&c, ES_CK_APPLY, owned, NULL, size);
        es_ck_controls(&c, &stage);
        error = es_ck_finish(&c);
    }
    es_ck_controls_discard(&stage);
    free(owned);
    return error;
}

void DLLEXPORT es_test_controls_poison(void)
{
    int r, branch;
    struct TAction *a;
    ControlValue = -321.5;
    SetPoint = 753.25;
    CurrentDate = 1234;
    CurrentTime = .987;
    for (r = 0; r < RuleCount; r++) for (branch = 0; branch < 2; branch++)
    {
        a = branch ? Rules[r].elseActions : Rules[r].thenActions;
        for (; a; a = a->next)
        {
            if (a->attribute == r_PID)
            {
                a->value = .456; a->e1 = -123; a->e2 = 987;
            }
            else if (a->curve >= 0 || a->tseries >= 0) a->value = .456;
        }
    }
    deleteActionList();
}

int DLLEXPORT es_test_codec_overflow(void)
{
    EsCkCursor c;
    es_ck_init(&c, ES_CK_MEASURE, NULL, NULL, 0);
    c.position = SIZE_MAX - 2;
    es_ck_fixed_u32(&c, 1);
    return c.error;
}
