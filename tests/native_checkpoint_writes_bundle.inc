void es_test_writes_arm(int);
static void es_test_writes_walk(EsCkCursor *c, EsCkControlsStage *controls,
    EsCkTablesStage *tables, EsCkStreamsStage *streams, EsCkWritesStage *writes, int only)
{
    if (only)
    {
        if (es_ck_engine_boundary()) { es_ck_fail(c,ES_CK_STATE); return; }
    }
    else es_test_output_bundle_walk(c,controls,tables,streams,0);
    es_ck_writes(c,writes);
}
int DLLEXPORT es_test_writes_save(void *data, size_t size, size_t *used, int only)
{
    EsCkCursor c;
    es_test_table_arm(-1);
    es_test_writes_arm(-1);
    es_ck_init(&c,data ? ES_CK_WRITE : ES_CK_MEASURE,NULL,data,size);
    es_test_writes_walk(&c,NULL,NULL,NULL,NULL,only);
    *used=c.position;
    return es_ck_finish(&c);
}
int DLLEXPORT es_test_writes_restore(const void *data, size_t size, int fail_at, int write_fail_at,
    FILE *(*open_read)(void *, const char *, int *),
    int (*prepare_prefix)(void *, int, uint32_t, int, uint64_t, char *, size_t),
    int only, int *cleanup_error, int *committed)
{
    EsCkCursor c;
    EsCkControlsStage controls={0}; EsCkTablesStage tables={0};
    EsCkStreamsStage streams={0}; EsCkWritesStage writes={0};
    EsCkReadResources inputs={NULL,open_read};
    EsCkWriteResources outputs={NULL,prepare_prefix};
    unsigned char *owned;
    int error,next;
    *cleanup_error=*committed=0;
    es_test_writes_arm(write_fail_at);
    if (!data || size>16*1024*1024) return ES_CK_ARGUMENT;
    owned=malloc(size ? size : 1); if (!owned) return ES_CK_MEMORY;
    memcpy(owned,data,size);
    es_ck_init(&c,ES_CK_VALIDATE,owned,NULL,size);
    es_test_writes_walk(&c,&controls,&tables,&streams,&writes,only);
    error=es_ck_finish(&c);
    if (!error && !only) error=es_ck_controls_prepare(&controls);
    es_test_table_arm(fail_at);
    if (!error && !only) error=es_ck_tables_prepare(&tables,owned,size,&inputs);
    if (!error && !only) error=es_ck_streams_prepare(&streams,&inputs);
    if (!error) error=es_ck_writes_prepare(&writes,owned,size,&outputs);
    if (!error)
    {
        es_ck_init(&c,ES_CK_APPLY,owned,NULL,size);
        es_test_writes_walk(&c,&controls,&tables,&streams,&writes,only);
        error=es_ck_finish(&c); *committed=!error;
    }
    *cleanup_error=es_ck_writes_discard(&writes);
    next=es_ck_streams_discard(&streams); if (!*cleanup_error) *cleanup_error=next;
    next=es_ck_tables_discard(&tables); if (!*cleanup_error) *cleanup_error=next;
    es_ck_controls_discard(&controls); free(owned);
    return error;
}

int DLLEXPORT es_test_writes_info(uint32_t index, int *role, int *text,
    uint64_t *bytes, char *path, size_t capacity)
{
    EsCkWriteOwner *owners;
    uint32_t count,actual;
    const char *name;
    int error=es_ck_write_inventory(NULL,0,&count);
    if (error) return -error;
    if (index>=count) return 0;
    owners=calloc(count,sizeof(*owners)); if (!owners) return -ES_CK_MEMORY;
    error=es_ck_write_inventory(owners,count,&actual);
    if (!error) error=es_ck_write_extent(*owners[index].file,bytes);
    if (!error)
    {
        name=owners[index].dynamic_name ? *owners[index].dynamic_name : owners[index].fixed_name;
        if (!name || strlen(name)>=capacity) error=ES_CK_BOUNDS;
        else { strcpy(path,name); *role=owners[index].role; *text=owners[index].text; }
    }
    free(owners); return error ? -error : 1;
}
int DLLEXPORT es_test_writes_drop(void)
{
    EsCkWriteOwner *owners;
    uint32_t count,actual,i;
    int error=es_ck_write_inventory(NULL,0,&count),next;
    if (error) return error;
    owners=calloc(count,sizeof(*owners)); if (!owners) return ES_CK_MEMORY;
    error=es_ck_write_inventory(owners,count,&actual);
    if (!error) for (i=0;i<count;i++)
    {
        next=es_ck_read_close(owners[i].file); if (!error) error=next;
    }
    free(owners); return error;
}
