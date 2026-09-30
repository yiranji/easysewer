static void es_test_streams_walk(EsCkCursor *c, EsCkControlsStage *controls,
    EsCkTablesStage *tables, EsCkStreamsStage *streams, int only)
{
    if (!only) es_test_tables_walk(c, controls, tables, 0);
    es_ck_streams(c, streams);
}
int DLLEXPORT es_test_streams_save(void *data, size_t size, size_t *used, int only)
{
    EsCkCursor c;
    es_test_table_arm(-1);
    es_ck_init(&c, data ? ES_CK_WRITE : ES_CK_MEASURE, NULL, data, size);
    es_test_streams_walk(&c, NULL, NULL, NULL, only);
    *used = c.position;
    return es_ck_finish(&c);
}
int DLLEXPORT es_test_streams_restore(const void *data, size_t size, int fail_at,
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
    es_test_streams_walk(&c, &controls, &tables, &streams, only);
    error = es_ck_finish(&c);
    if (!error && !only) error = es_ck_controls_prepare(&controls);
    es_test_table_arm(fail_at);
    if (!error && !only) error = es_ck_tables_prepare(&tables, owned, size, &resources);
    if (!error) error = es_ck_streams_prepare(&streams, &resources);
    if (!error)
    {
        es_ck_init(&c, ES_CK_APPLY, owned, NULL, size);
        es_test_streams_walk(&c, &controls, &tables, &streams, only);
        error = es_ck_finish(&c);
        *committed = streams.committed;
    }
    *cleanup_error = es_ck_streams_discard(&streams);
    next = es_ck_tables_discard(&tables);
    if (!*cleanup_error) *cleanup_error = next;
    es_ck_controls_discard(&controls);
    free(owned);
    return error;
}
const char *DLLEXPORT es_test_streams_resource(int role)
{
    if (role < 0 || role >= ES_CK_READ_STREAMS || !es_ck_stream_active(role)) return NULL;
    return es_ck_stream_identity(role);
}
const char *DLLEXPORT es_test_streams_path(int role)
{
    if (!es_test_streams_resource(role)) return NULL;
    return es_ck_stream_file(role)->name;
}
void DLLEXPORT es_test_streams_drop(void)
{
    int role;
    es_test_table_arm(-1);
    for (role = 0; role < ES_CK_READ_STREAMS; role++) if (es_ck_stream_active(role))
        es_ck_read_close(&es_ck_stream_file(role)->file);
}
int DLLEXPORT es_test_streams_peek(int role, void *bytes, size_t capacity, size_t *used)
{
    FILE *file;
    fpos_t position;
    if (!es_test_streams_resource(role)) return ES_CK_ARGUMENT;
    file = es_ck_stream_file(role)->file;
    if (!file || fgetpos(file, &position)) return ES_CK_IO;
    *used = fread(bytes, 1, capacity, file);
    if (ferror(file) || fsetpos(file, &position)) return ES_CK_IO;
    return 0;
}
