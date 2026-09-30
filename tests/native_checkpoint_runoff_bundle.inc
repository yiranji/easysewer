/* The engine owns DoRunoff; inactive projects never inspect stale allocations. */
void es_test_runoff_poison(int group);
void es_test_ode_poison(void);
static void es_test_runoff_walk(EsCkCursor *c, EsCkControlsStage *stage)
{
    es_test_gage_walk(c, stage);
    es_ck_runoff(c, DoRunoff);
}

int DLLEXPORT es_test_runoff_save(void *data, size_t size, size_t *used)
{
    EsCkCursor c;
    es_ck_init(&c, data ? ES_CK_WRITE : ES_CK_MEASURE, NULL, data, size);
    es_test_runoff_walk(&c, NULL);
    *used = c.position;
    return es_ck_finish(&c);
}

int DLLEXPORT es_test_runoff_restore(const void *data, size_t size, int fail_at)
{
    EsCkCursor c;
    EsCkControlsStage stage = {0};
    unsigned char *owned;
    int error;
    if (!data || size > 16*1024*1024) return ES_CK_ARGUMENT;
    owned = malloc(size ? size : 1);
    if (!owned) return ES_CK_MEMORY;
    memcpy(owned, data, size);
    es_ck_init(&c, ES_CK_VALIDATE, owned, NULL, size);
    es_test_runoff_walk(&c, &stage);
    error = es_ck_finish(&c);
    if (!error) error = es_test_prepare_controls(&stage, fail_at);
    if (!error)
    {
        es_ck_init(&c, ES_CK_APPLY, owned, NULL, size);
        es_test_runoff_walk(&c, &stage);
        error = es_ck_finish(&c);
    }
    es_ck_controls_discard(&stage);
    free(owned);
    return error;
}

void DLLEXPORT es_test_runoff_bundle_poison(void)
{
    es_test_gage_bundle_poison();
    if (DoRunoff)
    {
        es_test_runoff_poison(0);
        es_test_ode_poison();
    }
}
