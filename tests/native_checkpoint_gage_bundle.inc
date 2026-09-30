/* Appended after the catchment bundle, preserving all smaller test ABIs. */
void es_test_gage_poison(int group);
static void es_test_gage_walk(EsCkCursor *c, EsCkControlsStage *stage)
{
    es_test_catchment_walk(c, stage);
    es_ck_gage(c);
}

int DLLEXPORT es_test_gage_save(void *data, size_t size, size_t *used)
{
    EsCkCursor c;
    es_ck_init(&c, data ? ES_CK_WRITE : ES_CK_MEASURE, NULL, data, size);
    es_test_gage_walk(&c, NULL);
    *used = c.position;
    return es_ck_finish(&c);
}

int DLLEXPORT es_test_gage_restore(const void *data, size_t size, int fail_at)
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
    es_test_gage_walk(&c, &stage);
    error = es_ck_finish(&c);
    if (!error) error = es_test_prepare_controls(&stage, fail_at);
    if (!error)
    {
        es_ck_init(&c, ES_CK_APPLY, owned, NULL, size);
        es_test_gage_walk(&c, &stage);
        error = es_ck_finish(&c);
    }
    es_ck_controls_discard(&stage);
    free(owned);
    return error;
}

void DLLEXPORT es_test_gage_bundle_poison(void)
{
    es_test_catchment_bundle_poison();
    es_test_gage_poison(0);
}
