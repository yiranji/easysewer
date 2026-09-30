/* Appended to swmm5.c after engine/stream owners. No test API dependency. */
#include "es_checkpoint_api.h"

typedef struct {
    EsCkControlsStage controls;
    EsCkTablesStage tables;
    EsCkStreamsStage streams;
    EsCkWritesStage writes;
} EsCkNativeStage;
typedef struct { EsCkInputPath input; void *context; } EsCkPathContext;
static int EsCheckpointBusy = 0;

static int es_ck_api_enter(void)
{
    if (EsCheckpointBusy || es_ck_engine_boundary()) return ES_CK_STATE;
    EsCheckpointBusy = 1;
    return 0;
}

static void es_ck_native_walk(EsCkCursor *c, const void *binding, EsCkNativeStage *s)
{
    c->logical_resources = 1;
    es_ck_tag(c,"ESCKPT02",8);
    es_ck_fixed_u32(c,2); es_ck_fixed_i32(c,ES_CK_CUSTOM);
    es_ck_fixed_i32(c,swmm_getVersion());
    es_ck_tag(c,(const char *)binding,32);
    es_ck_engine(c); es_ck_controls(c,s ? &s->controls : NULL);
    es_ck_dynwave(c,DoRouting); es_ck_network(c); es_ck_inlet(c);
    es_ck_infiltration(c); es_ck_subcatchment(c); es_ck_gage(c);
    es_ck_runoff(c,DoRunoff); es_ck_climate(c);
    es_ck_tables(c,s ? &s->tables : NULL); es_ck_streams(c,s ? &s->streams : NULL);
    es_ck_input_frames(c); es_ck_groundwater(c); es_ck_snow(c); es_ck_lid(c);
    es_ck_massbal(c); es_ck_stats(c); es_ck_output(c);
#if ES_CK_CUSTOM
    es_ck_ponding(c);
#endif
    es_ck_writes(c,s ? &s->writes : NULL);
}

static int es_ck_native_discard(EsCkNativeStage *s)
{
    int error=es_ck_writes_discard(&s->writes), next;
    next=es_ck_streams_discard(&s->streams); if (!error) error=next;
    next=es_ck_tables_discard(&s->tables); if (!error) error=next;
    es_ck_controls_discard(&s->controls);
    return error;
}

static FILE *es_ck_path_open(void *context, const char *identity, int *error)
{
    EsCkPathContext *provider=context;
    char path[MAXFNAME+1]={0};
    FILE *file;
    *error=provider->input(provider->context,identity,path,sizeof(path));
    if (*error) return NULL;
    if (!path[0] || !memchr(path,0,sizeof(path)))
    { *error=ES_CK_ARGUMENT; return NULL; }
    file=fopen(path,"rb");
    if (!file) *error=ES_CK_IO;
    return file;
}

int ES_CK_EXPORT swmm_checkpointVersion(void) { return 2; }

int ES_CK_EXPORT swmm_checkpointCapture(const void *binding, void *data,
                                      size_t capacity, size_t *used)
{
    EsCkCursor c;
    unsigned char key[32], *owned=NULL;
    size_t length;
    int error;
    if (!used) return ES_CK_ARGUMENT;
    *used=0;
    if (!binding || (!data && capacity)) return ES_CK_ARGUMENT;
    error=es_ck_api_enter(); if (error) return error;
    memcpy(key,binding,32);
    es_ck_init(&c,ES_CK_MEASURE,NULL,NULL,0); es_ck_native_walk(&c,key,NULL);
    error=es_ck_finish(&c); length=c.position;
    if (!error) *used=length;
    if (!error && data)
    {
        if (capacity<length) error=ES_CK_BOUNDS;
        else if (!(owned=malloc(length ? length : 1))) error=ES_CK_MEMORY;
        else
        {
            es_ck_init(&c,ES_CK_WRITE,NULL,owned,length); es_ck_native_walk(&c,key,NULL);
            error=es_ck_finish(&c);
            if (!error) memcpy(data,owned,length);
        }
    }
    free(owned); EsCheckpointBusy=0;
    return error;
}

int ES_CK_EXPORT swmm_checkpointValidate(const void *binding, const void *data, size_t size)
{
    EsCkNativeStage s={0};
    EsCkCursor c;
    int error;
    if (!binding || !data || !size) return ES_CK_ARGUMENT;
    error=es_ck_api_enter(); if (error) return error;
    es_ck_init(&c,ES_CK_VALIDATE,data,NULL,size); es_ck_native_walk(&c,binding,&s);
    error=es_ck_finish(&c);
    es_ck_native_discard(&s); EsCheckpointBusy=0;
    return error;
}

int ES_CK_EXPORT swmm_checkpointRestore(const void *binding, const void *data, size_t size,
    EsCkInputPath input, EsCkOutputPath output, void *context, int *committed, int *cleanup_error)
{
    EsCkNativeStage s={0};
    EsCkCursor c;
    EsCkPathContext provider={input,context};
    EsCkReadResources reads={&provider,es_ck_path_open};
    EsCkWriteResources writes={context,output};
    unsigned char key[32], *owned;
    int error;
    if (!committed || !cleanup_error) return ES_CK_ARGUMENT;
    *committed=*cleanup_error=0;
    if (!binding || !data || !size || !input || !output) return ES_CK_ARGUMENT;
    error=es_ck_api_enter(); if (error) return error;
    memcpy(key,binding,32);
    owned=malloc(size);
    if (!owned) { EsCheckpointBusy=0; return ES_CK_MEMORY; }
    memcpy(owned,data,size);
    es_ck_init(&c,ES_CK_VALIDATE,owned,NULL,size); es_ck_native_walk(&c,key,&s);
    error=es_ck_finish(&c);
    if (!error) error=es_ck_controls_prepare(&s.controls);
    if (!error) error=es_ck_tables_prepare(&s.tables,owned,size,&reads);
    if (!error) error=es_ck_streams_prepare(&s.streams,&reads);
    if (!error) error=es_ck_writes_prepare(&s.writes,owned,size,&writes);
    if (!error)
    {
        *committed=1;
        es_ck_init(&c,ES_CK_APPLY,owned,NULL,size); es_ck_native_walk(&c,key,&s);
        error=es_ck_finish(&c);
    }
    *cleanup_error=es_ck_native_discard(&s);
    free(owned); EsCheckpointBusy=0;
    return error;
}

int ES_CK_EXPORT swmm_checkpointOutputCount(uint32_t *count)
{
    int error;
    if (!count) return ES_CK_ARGUMENT;
    *count=0; error=es_ck_api_enter(); if (error) return error;
    error=es_ck_write_inventory(NULL,0,count); EsCheckpointBusy=0;
    return error;
}

int ES_CK_EXPORT swmm_checkpointOutputInfo(uint32_t index, int *role, int *text,
    uint64_t *bytes, char *path, size_t capacity)
{
    EsCkWriteOwner *owners=NULL;
    const char *name;
    uint32_t count, actual;
    int error;
    if (!role || !text || !bytes || !path || !capacity) return ES_CK_ARGUMENT;
    error=es_ck_api_enter(); if (error) return error;
    error=es_ck_write_inventory(NULL,0,&count);
    if (!error && index>=count) error=ES_CK_BOUNDS;
    if (!error && count && sizeof(*owners)>SIZE_MAX/count) error=ES_CK_BOUNDS;
    if (!error && !(owners=calloc(count,sizeof(*owners)))) error=ES_CK_MEMORY;
    if (!error) error=es_ck_write_inventory(owners,count,&actual);
    if (!error && actual!=count) error=ES_CK_STATE;
    if (!error)
    {
        name=owners[index].dynamic_name ? *owners[index].dynamic_name : owners[index].fixed_name;
        if (!name || strlen(name)>=capacity) error=ES_CK_BOUNDS;
        if (!error) error=es_ck_write_extent(*owners[index].file,bytes);
        if (!error) { strcpy(path,name); *role=owners[index].role; *text=owners[index].text; }
    }
    free(owners); EsCheckpointBusy=0;
    return error;
}

static int es_ck_input_item(uint32_t index, uint32_t *count,
    char *identity, size_t identity_capacity, const char **path)
{
    int group, i, n;
    TTable *table;
    *count=0; identity[0]=0; *path=NULL;
    for (group=0; group<3; group++)
    {
        n=group==0 ? Nobjects[CURVE] : group==1 ? Nobjects[TSERIES] : 2;
        for (i=0; i<n; i++)
        {
            table=group==0 ? &Curve[i] : group==1 ? &Tseries[i] : es_ck_climate_table(i);
            if (!table || !table->file.name[0]) continue;
            if (*count==UINT32_MAX) return ES_CK_BOUNDS;
            if ((*count)++==index)
            { es_ck_table_key(identity,identity_capacity,group,i); *path=table->file.name; }
        }
    }
    for (i=0; i<ES_CK_READ_STREAMS; i++) if (es_ck_stream_active(i))
    {
        if (*count==UINT32_MAX) return ES_CK_BOUNDS;
        if ((*count)++==index)
        { snprintf(identity,identity_capacity,"%s",es_ck_stream_key(i)); *path=es_ck_stream_file(i)->name; }
    }
    return 0;
}

int ES_CK_EXPORT swmm_checkpointInputCount(uint32_t *count)
{
    char identity[64];
    const char *path;
    int error;
    if (!count) return ES_CK_ARGUMENT;
    *count=0; error=es_ck_api_enter(); if (error) return error;
    error=es_ck_input_item(UINT32_MAX,count,identity,sizeof(identity),&path); EsCheckpointBusy=0;
    return error;
}

int ES_CK_EXPORT swmm_checkpointInputInfo(uint32_t index, char *identity,
    size_t identity_capacity, char *path, size_t path_capacity)
{
    char key[64];
    const char *name;
    uint32_t count;
    int error;
    if (!identity || !identity_capacity || !path || !path_capacity) return ES_CK_ARGUMENT;
    error=es_ck_api_enter(); if (error) return error;
    error=es_ck_input_item(index,&count,key,sizeof(key),&name);
    if (!error && (index>=count || !key[0] || !name)) error=ES_CK_BOUNDS;
    if (!error && (strlen(key)>=identity_capacity || strlen(name)>=path_capacity)) error=ES_CK_BOUNDS;
    if (!error) { strcpy(identity,key); strcpy(path,name); }
    EsCheckpointBusy=0;
    return error;
}
