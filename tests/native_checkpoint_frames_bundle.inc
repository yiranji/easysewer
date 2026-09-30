void es_test_rdii_poison(int scratch);
void es_test_iface_poison(int scratch);
void DLLEXPORT es_test_frames_poison(int scratch)
{
    if (es_ck_stream_active(3)) es_test_rdii_poison(scratch);
    if (es_ck_stream_active(4)) es_test_iface_poison(scratch);
}
static void es_test_frames_walk(EsCkCursor *c, EsCkControlsStage *controls,
    EsCkTablesStage *tables, EsCkStreamsStage *streams, int only)
{
    es_test_streams_walk(c, controls, tables, streams, only);
    es_ck_input_frames(c);
}
int DLLEXPORT es_test_frames_save(void *data, size_t size, size_t *used, int only)
{
    EsCkCursor c;
    es_test_table_arm(-1);
    es_ck_init(&c, data ? ES_CK_WRITE : ES_CK_MEASURE, NULL, data, size);
    es_test_frames_walk(&c, NULL, NULL, NULL, only);
    *used = c.position;
    return es_ck_finish(&c);
}
int DLLEXPORT es_test_frames_restore(const void *data, size_t size, int fail_at,
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
    es_test_frames_walk(&c, &controls, &tables, &streams, only);
    error = es_ck_finish(&c);
    if (!error && !only) error = es_ck_controls_prepare(&controls);
    es_test_table_arm(fail_at);
    if (!error && !only) error = es_ck_tables_prepare(&tables, owned, size, &resources);
    if (!error) error = es_ck_streams_prepare(&streams, &resources);
    if (!error)
    {
        es_ck_init(&c, ES_CK_APPLY, owned, NULL, size);
        es_test_frames_walk(&c, &controls, &tables, &streams, only);
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
/* Read/query only the two input owners, independent of all other native state.
 * This allows a fresh process to prove future frame consumption, not merely
 * that the serialized bytes happen to round-trip. */
int DLLEXPORT es_test_frames_query(double date, double *values, size_t capacity)
{
    int i, j, p, n, used = 0;
    double flow;
    if (capacity < (size_t)Nobjects[NODE]*(2+(size_t)Nobjects[POLLUT])) return -1;
    for (i = 0; i < Nobjects[NODE]*(2+Nobjects[POLLUT]); i++) values[i] = 0.0;
    if (es_ck_stream_active(3))
    {
        n = rdii_getNumRdiiFlows(date);
        for (i = 0; i < n; i++)
        { rdii_getRdiiFlow(i, &j, &flow); values[j] += flow; }
    }
    used = Nobjects[NODE];
    if (es_ck_stream_active(4))
    {
        n = iface_getNumIfaceNodes(date);
        for (i = 0; i < n; i++)
        {
            j = iface_getIfaceNode(i); if (j < 0) continue;
            values[used+j] += iface_getIfaceFlow(i);
            for (p = 0; p < Nobjects[POLLUT]; p++)
                values[used+(p+1)*Nobjects[NODE]+j] += iface_getIfaceQual(i, p);
        }
    }
    return ErrorCode ? -ErrorCode : Nobjects[NODE]*(2+Nobjects[POLLUT]);
}
