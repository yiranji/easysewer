/* Appended to table.c. Entries remain project-owned; cursor nodes and binary
 * read streams are staged independently, then transferred without I/O. */
#include "es_checkpoint.h"
#include <sys/types.h>

struct EsCkTableOwner {
    TTable *target;
    TTable replacement, retired;
};

static int es_ck_table_seek(FILE *file, uint64_t position)
{
    if (position > INT64_MAX) return ES_CK_NUMBER;
#ifdef _WIN32
    return _fseeki64(file, (__int64)position, SEEK_SET) ? ES_CK_IO : 0;
#else
    if (sizeof(off_t) < 8) return ES_CK_STATE;
    return fseeko(file, (off_t)position, SEEK_SET) ? ES_CK_IO : 0;
#endif
}

int es_ck_read_position(FILE *file, uint64_t *position)
{
#ifdef _WIN32
    __int64 offset = _ftelli64(file);
#else
    off_t offset = ftello(file);
#endif
    if (offset < 0 || ferror(file)) return ES_CK_IO;
    *position = (uint64_t)offset;
    return 0;
}

int es_ck_read_close(FILE **file)
{
    FILE *retired = *file;
    *file = NULL;
    return retired && fclose(retired) ? ES_CK_IO : 0;
}

int es_ck_read_prepare(FILE **file, const EsCkReadResources *resources,
                       const char *identity, uint64_t position)
{
    uint64_t extent = 0;
    int error = 0;
    if (!file || *file || !resources || !resources->open_read || !identity)
        return ES_CK_STATE;
    if (position > INT64_MAX) return ES_CK_NUMBER;
    *file = resources->open_read(resources->context, identity, &error);
    if (!*file) return error ? error : ES_CK_IO;
    /* Ownership is visible immediately, including provider errors after open. */
    if (error) return error;
#ifdef _WIN32
    if (_fseeki64(*file, 0, SEEK_END)) error = ES_CK_IO;
#else
    if (fseeko(*file, 0, SEEK_END)) error = ES_CK_IO;
#endif
    if (!error) error = es_ck_read_position(*file, &extent);
    if (!error && position > extent) error = ES_CK_NUMBER;
    if (!error) error = es_ck_table_seek(*file, position);
    if (!error) clearerr(*file);
    return error;
}

static int es_ck_table_release(TTable *table)
{
    int error = 0, next;
    if (table->lookup)
    {
        next = es_ck_table_release(table->lookup);
        if (!error) error = next;
        free(table->lookup);
        table->lookup = NULL;
    }
    next = es_ck_read_close(&table->file.file);
    if (!error) error = next;
    return error;
}

int es_ck_tables_discard(EsCkTablesStage *stage)
{
    uint32_t i;
    int error = 0, next;
    if (!stage) return 0;
    if (stage->owners) for (i = 0; i < stage->count; i++)
    {
        next = es_ck_table_release(stage->committed ?
            &stage->owners[i].retired : &stage->owners[i].replacement);
        if (!error) error = next;
    }
    free(stage->owners);
    memset(stage, 0, sizeof(*stage));
    return error;
}

/* VALIDATE parses into local/staged values, never into the live owner. The
 * reconstructed entry list and file identity are held fixed by the caller. */
static void es_ck_table_cursor(EsCkCursor *c, TTable *live, TTable *base,
                               uint32_t entries, TTable *out,
                               EsCkTablesStage *stage, int depth, const char *resource_key)
{
    TTable local;
    TTableEntry *entry, *selected = NULL;
    uint32_t index = UINT32_MAX, i = 0;
    uint64_t position = 0;
    int opened = 0, lookup = 0, error = 0, saving;
    saving = c->mode == ES_CK_MEASURE || c->mode == ES_CK_WRITE;
    local = live ? *live : *base;
    if (saving)
    {
        if (!live || live->fileValidating || (depth && live->lookup))
        { es_ck_fail(c, ES_CK_STATE); return; }
        opened = live->file.file != NULL;
        lookup = live->lookup != NULL;
        if (live->thisEntry)
        {
            for (entry = base->firstEntry; entry; entry = entry->next, i++)
                if (entry == live->thisEntry) { index = i; break; }
            if (index == UINT32_MAX) { es_ck_fail(c, ES_CK_STATE); return; }
        }
        if (opened) error = es_ck_read_position(live->file.file, &position);
        if (error) { es_ck_fail(c, error); return; }
    }
    index = es_ck_u32(c, &index);
    if (index != UINT32_MAX && index >= entries) es_ck_fail(c, ES_CK_NUMBER);
    local.lastDate = es_ck_f64_value(c, &local.lastDate);
    local.x1 = es_ck_f64_value(c, &local.x1); local.x2 = es_ck_f64_value(c, &local.x2);
    local.y1 = es_ck_f64_value(c, &local.y1); local.y2 = es_ck_f64_value(c, &local.y2);
    local.lookupReady = es_ck_i32(c, &local.lookupReady, 0, 1);
    local.lookupEnded = es_ck_i32(c, &local.lookupEnded, 0, 1);
    local.fileHasPrevious = es_ck_i32(c, &local.fileHasPrevious, 0, 1);
    local.filePrevious = es_ck_f64_value(c, &local.filePrevious);
    if (local.lookupReady && local.x2 < local.x1) es_ck_fail(c, ES_CK_NUMBER);
    opened = es_ck_i32(c, &opened, 0, 1);
    if (opened != (base->file.mode == USE_FILE)) es_ck_fail(c, ES_CK_STATE);
    if (opened)
    {
        position = es_ck_u64(c, &position);
        if (position > INT64_MAX) es_ck_fail(c, ES_CK_NUMBER);
    }
    lookup = es_ck_i32(c, &lookup, 0, depth ? 0 : 1);
    if (c->error) return;
    if (out)
    {
        /* Publish ownership immediately so every partial prepare is discardable. */
        *out = local;
        out->lookup = NULL; out->file.file = NULL; out->fileValidating = FALSE;
        for (entry = base->firstEntry, i = 0; entry; entry = entry->next, i++)
            if (i == index) { selected = entry; break; }
        out->thisEntry = selected;
        if (opened)
        {
            error = es_ck_read_prepare(&out->file.file, stage->resources,
                                       stage->logical_resources ? resource_key : base->file.name, position);
            if (error) { es_ck_fail(c, error); return; }
        }
        if (lookup)
        {
            out->lookup = calloc(1, sizeof(TTable));
            if (!out->lookup) { es_ck_fail(c, ES_CK_MEMORY); return; }
        }
    }
    if (lookup) es_ck_table_cursor(c, live ? live->lookup : NULL, base, entries,
                                   out ? out->lookup : NULL, stage, depth+1, resource_key);
}

static void es_ck_table_owner(EsCkCursor *c, TTable *table,
                              EsCkTablesStage *stage, uint32_t index, const char *resource_key)
{
    TTableEntry *entry, *last = NULL;
    uint32_t count = 0;
    TTable *out = NULL;
    if (c->error) return;
    es_ck_identity(c, table->ID);
    es_ck_fixed_i32(c, table->curveType); es_ck_fixed_i32(c, table->refersTo);
    es_ck_fixed_f64(c, table->dxMin);
    es_ck_fixed_i32(c, table->file.mode);
    es_ck_identity(c, c->logical_resources && table->file.name[0] ? resource_key : table->file.name);
    for (entry = table->firstEntry; entry; entry = entry->next)
    {
        if (count == UINT32_MAX) { es_ck_fail(c, ES_CK_BOUNDS); return; }
        count++; last = entry;
    }
    if (last != table->lastEntry) { es_ck_fail(c, ES_CK_STATE); return; }
    es_ck_fixed_u32(c, count);
    for (entry = table->firstEntry; entry && !c->error; entry = entry->next)
    { es_ck_fixed_f64(c, entry->x); es_ck_fixed_f64(c, entry->y); }
    if (stage && stage->preparing)
    {
        stage->owners[index].target = table;
        out = &stage->owners[index].replacement;
    }
    es_ck_table_cursor(c, table, table, count, out, stage, 0, resource_key);
}

void es_ck_tables(EsCkCursor *c, EsCkTablesStage *stage)
{
    TTable *private_tables[2];
    uint32_t count, index = 0;
    uint64_t total;
    size_t begin = c->position;
    int i, kind;
    char resource_key[64];
    if (c->error) return;
    if (c->mode == ES_CK_APPLY)
    {
        if (!stage || !stage->validated || !stage->prepared || stage->committed ||
            stage->begin != c->position || stage->end > c->size)
        { es_ck_fail(c, ES_CK_STATE); return; }
        for (index = 0; index < stage->count; index++)
        {
            struct EsCkTableOwner *owner = &stage->owners[index];
            owner->retired = *owner->target;
            *owner->target = owner->replacement;
            memset(&owner->replacement, 0, sizeof(owner->replacement));
        }
        stage->committed = 1;
        c->position = stage->end;
        return;
    }
    if (c->mode == ES_CK_VALIDATE && (!stage || stage->prepared || stage->committed))
    { es_ck_fail(c, ES_CK_STATE); return; }
    if (Nobjects[CURVE] < 0 || Nobjects[TSERIES] < 0 ||
        (Nobjects[CURVE] && !Curve) || (Nobjects[TSERIES] && !Tseries))
    { es_ck_fail(c, ES_CK_STATE); return; }
    total = (uint64_t)Nobjects[CURVE] + (uint64_t)Nobjects[TSERIES];
    for (i = 0; i < 2; i++)
    { private_tables[i] = es_ck_climate_table(i); if (private_tables[i]) total++; }
    if (total > UINT32_MAX) { es_ck_fail(c, ES_CK_BOUNDS); return; }
    count = (uint32_t)total;
    es_ck_tag(c, "ESTBL001", 8);
    es_ck_fixed_i32(c, Nobjects[CURVE]); es_ck_fixed_i32(c, Nobjects[TSERIES]);
    if (stage && c->mode == ES_CK_VALIDATE && !stage->preparing)
        stage->logical_resources = c->logical_resources;
    if (stage && stage->preparing && stage->count != count)
    { es_ck_fail(c, ES_CK_STATE); return; }
    for (kind = 0; kind < 2; kind++)
        for (i = 0; i < Nobjects[kind ? TSERIES : CURVE] && !c->error; i++)
        {
            es_ck_table_key(resource_key, sizeof(resource_key), kind, i);
            es_ck_table_owner(c, &(kind ? Tseries : Curve)[i], stage, index++, resource_key);
        }
    for (i = 0; i < 2 && !c->error; i++)
    {
        es_ck_fixed_i32(c, private_tables[i] != NULL);
        es_ck_table_key(resource_key, sizeof(resource_key), 2, i);
        if (private_tables[i]) es_ck_table_owner(c, private_tables[i], stage, index++, resource_key);
    }
    if (!c->error && c->mode == ES_CK_VALIDATE && !stage->preparing)
    {
        stage->begin = begin; stage->end = c->position;
        stage->count = count; stage->validated = 1;
    }
}

int es_ck_tables_prepare(EsCkTablesStage *stage, const void *data, size_t size,
                         const EsCkTableResources *resources)
{
    EsCkCursor c;
    int error;
    if (!stage || !stage->validated || stage->prepared || stage->preparing ||
        stage->owners || stage->committed || !data || stage->end > size ||
        !resources || !resources->open_read) return ES_CK_STATE;
    if (stage->count && sizeof(*stage->owners) > SIZE_MAX / (size_t)stage->count)
        return ES_CK_BOUNDS;
    stage->owners = calloc(stage->count ? stage->count : 1, sizeof(*stage->owners));
    if (!stage->owners) return ES_CK_MEMORY;
    stage->resources = resources; stage->preparing = 1;
    es_ck_init(&c, ES_CK_VALIDATE, data, NULL, stage->end);
    c.logical_resources = stage->logical_resources;
    c.position = stage->begin;
    es_ck_tables(&c, stage);
    error = es_ck_finish(&c);
    stage->preparing = 0; stage->resources = NULL;
    if (!error) stage->prepared = 1;
    /* Caller must discard on failure, retaining primary and cleanup errors. */
    return error;
}
