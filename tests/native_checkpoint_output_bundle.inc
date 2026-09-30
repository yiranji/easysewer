static void es_test_output_bundle_walk(EsCkCursor *c, EsCkControlsStage *controls,
    EsCkTablesStage *tables, EsCkStreamsStage *streams, int only)
{
    if (only)
    {
        if (es_ck_engine_boundary()) { es_ck_fail(c, ES_CK_STATE); return; }
    }
    else es_test_balance_bundle_walk(c, controls, tables, streams, 0);
    es_ck_output(c);
#if ES_CK_CUSTOM
    es_ck_ponding(c);
#endif
}
int DLLEXPORT es_test_output_save(void *data, size_t size, size_t *used, int only)
{
    EsCkCursor c;
    es_test_table_arm(-1);
    es_ck_init(&c, data ? ES_CK_WRITE : ES_CK_MEASURE, NULL, data, size);
    es_test_output_bundle_walk(&c, NULL, NULL, NULL, only);
    *used = c.position;
    return es_ck_finish(&c);
}
int DLLEXPORT es_test_output_restore(const void *data, size_t size, int fail_at,
    FILE *(*open_read)(void *, const char *, int *), int only,
    int *cleanup_error, int *committed)
{
    EsCkCursor c;
    EsCkControlsStage controls = {0};
    EsCkTablesStage tables = {0};
    EsCkStreamsStage streams = {0};
    EsCkReadResources resources = {NULL, open_read};
    unsigned char *owned;
    int error, next;
    *cleanup_error = *committed = 0;
    if (!data || size > 16*1024*1024) return ES_CK_ARGUMENT;
    owned = malloc(size ? size : 1);
    if (!owned) return ES_CK_MEMORY;
    memcpy(owned, data, size);
    es_ck_init(&c, ES_CK_VALIDATE, owned, NULL, size);
    es_test_output_bundle_walk(&c, &controls, &tables, &streams, only);
    error = es_ck_finish(&c);
    if (!error && !only) error = es_ck_controls_prepare(&controls);
    es_test_table_arm(fail_at);
    if (!error && !only) error = es_ck_tables_prepare(&tables, owned, size, &resources);
    if (!error && !only) error = es_ck_streams_prepare(&streams, &resources);
    if (!error)
    {
        es_ck_init(&c, ES_CK_APPLY, owned, NULL, size);
        es_test_output_bundle_walk(&c, &controls, &tables, &streams, only);
        error = es_ck_finish(&c);
        *committed = !error;
    }
    *cleanup_error = es_ck_streams_discard(&streams);
    next = es_ck_tables_discard(&tables);
    if (!*cleanup_error) *cleanup_error = next;
    es_ck_controls_discard(&controls);
    free(owned);
    return error;
}
