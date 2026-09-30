/* Private output-prefix transaction. Byte digests and immutable snapshots are
 * the resource provider's responsibility. Native validation checks ownership,
 * physical extents and text mode; commit exchanges handles AND filenames. */
#include "es_checkpoint.h"
#include <stdlib.h>
#include <string.h>
#include <limits.h>
#include <sys/types.h>
#ifdef _WIN32
#include <io.h>
#include <windows.h>
#else
#include <sys/stat.h>
#include <unistd.h>
#endif

typedef struct { uint64_t volume, index, bytes; } EsCkFileIdentity;
struct EsCkWriteEntry {
    EsCkWriteOwner owner;
    FILE *replacement, *retired;
    char *path, *retired_name;
};

static int file_identity(FILE *file, EsCkFileIdentity *id)
{
    if (!file) return ES_CK_STATE;
#ifdef _WIN32
    BY_HANDLE_FILE_INFORMATION info;
    intptr_t raw=_get_osfhandle(_fileno(file));
    if (raw==-1 || !GetFileInformationByHandle((HANDLE)raw,&info) ||
        (info.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) ||
        GetFileType((HANDLE)raw)!=FILE_TYPE_DISK) return ES_CK_IO;
    id->volume=info.dwVolumeSerialNumber;
    id->index=((uint64_t)info.nFileIndexHigh<<32)|info.nFileIndexLow;
    id->bytes=((uint64_t)info.nFileSizeHigh<<32)|info.nFileSizeLow;
#else
    struct stat info;
    if (fstat(fileno(file),&info) || !S_ISREG(info.st_mode) || info.st_size<0)
        return ES_CK_IO;
    id->volume=(uint64_t)info.st_dev; id->index=(uint64_t)info.st_ino;
    id->bytes=(uint64_t)info.st_size;
#endif
    return id->bytes>INT64_MAX ? ES_CK_NUMBER : 0;
}

int es_ck_write_extent(FILE *file, uint64_t *bytes)
{
    EsCkFileIdentity id;
    uint64_t position;
    int error;
    if (!file || !bytes) return ES_CK_STATE;
    if (ferror(file) || fflush(file) || ferror(file)) return ES_CK_IO;
    error=file_identity(file,&id);
    if (!error) error=es_ck_read_position(file,&position);
    /* Every supported writer is at EOF at a successful step boundary. */
    if (!error && position!=id.bytes) error=ES_CK_STATE;
    if (!error) *bytes=id.bytes;
    return error;
}

static int stage_entry(EsCkWritesStage *s, uint32_t index, uint64_t bytes,
                       EsCkWriteOwner *owners)
{
    struct EsCkWriteEntry *e=&s->entries[index];
    EsCkFileIdentity id, other;
    size_t length;
    uint32_t i;
    int error;
    e->owner=owners[index];
    if (e->owner.name_capacity<2) return ES_CK_STATE;
    e->path=calloc(e->owner.name_capacity,1);
    if (!e->path) return ES_CK_MEMORY;
    error=s->resources->prepare_prefix(s->resources->context,e->owner.role,index,
        e->owner.text,bytes,e->path,e->owner.name_capacity);
    if (error) return error;
    if (!memchr(e->path,'\0',e->owner.name_capacity) || !e->path[0]) return ES_CK_ARGUMENT;
    length=strlen(e->path);
    if (length>=e->owner.name_capacity) return ES_CK_BOUNDS;
    /* Neither mode creates/truncates a path. Text translation is retained on
     * Windows; prefix length always measures physical bytes. */
    e->replacement=fopen(e->path,e->owner.text ? "r+" : "r+b");
    if (!e->replacement) return ES_CK_IO;
    error=file_identity(e->replacement,&id);
    if (error) return error;
    if (id.bytes!=bytes) return ES_CK_MISMATCH;
    for (i=0; i<s->count; i++) if (*owners[i].file)
    {
        error=file_identity(*owners[i].file,&other);
        if (error) return error;
        if (id.volume==other.volume && id.index==other.index) return ES_CK_MISMATCH;
    }
    for (i=0; i<index; i++) if (s->entries[i].replacement)
    {
        error=file_identity(s->entries[i].replacement,&other);
        if (error) return error;
        if (id.volume==other.volume && id.index==other.index) return ES_CK_MISMATCH;
    }
#ifdef _WIN32
    if (_fseeki64(e->replacement,0,SEEK_END)) return ES_CK_IO;
#else
    if (fseeko(e->replacement,0,SEEK_END)) return ES_CK_IO;
#endif
    return 0;
}

void es_ck_writes(EsCkCursor *c, EsCkWritesStage *stage)
{
    EsCkWriteOwner *owners=NULL;
    uint32_t count=0, actual=0, i;
    uint64_t bytes;
    int error, saving;
    size_t begin=c->position;
    if (c->error) return;
    if (c->mode==ES_CK_APPLY)
    {
        if (!stage || !stage->validated || !stage->prepared || stage->committed ||
            stage->begin!=begin || stage->end>c->size)
        { es_ck_fail(c,ES_CK_STATE); return; }
        for (i=0; i<stage->count; i++)
        {
            struct EsCkWriteEntry *e=&stage->entries[i];
            e->retired=*e->owner.file; *e->owner.file=e->replacement; e->replacement=NULL;
            if (e->owner.dynamic_name)
            {
                e->retired_name=*e->owner.dynamic_name;
                *e->owner.dynamic_name=e->path; e->path=NULL;
            }
            else strcpy(e->owner.fixed_name,e->path);
        }
        stage->committed=1; c->position=stage->end;
        return;
    }
    if (c->mode==ES_CK_VALIDATE && (!stage || stage->prepared || stage->committed ||
        (stage->validated && !stage->preparing)))
    { es_ck_fail(c,ES_CK_STATE); return; }
    error=es_ck_write_inventory(NULL,0,&count);
    if (error) { es_ck_fail(c,error); return; }
    if (count && sizeof(*owners)>SIZE_MAX/(size_t)count)
    { es_ck_fail(c,ES_CK_BOUNDS); return; }
    owners=calloc(count ? count : 1,sizeof(*owners));
    if (!owners) { es_ck_fail(c,ES_CK_MEMORY); return; }
    error=es_ck_write_inventory(owners,count,&actual);
    if (error || count!=actual) { es_ck_fail(c,error ? error : ES_CK_STATE); goto done; }
    if (stage && stage->preparing && stage->count!=count)
    { es_ck_fail(c,ES_CK_STATE); goto done; }
    saving=c->mode==ES_CK_MEASURE || c->mode==ES_CK_WRITE;
    es_ck_tag(c,"ESWRITE1",8); es_ck_fixed_u32(c,count);
    for (i=0; i<count && !c->error; i++)
    {
        es_ck_fixed_i32(c,owners[i].role); es_ck_fixed_i32(c,owners[i].text);
        es_ck_fixed_u32(c,owners[i].ordinal);
        es_ck_identity(c,owners[i].identity); es_ck_identity(c,owners[i].component);
        bytes=0;
        if (saving)
        {
            error=es_ck_write_extent(*owners[i].file,&bytes);
            if (error) { es_ck_fail(c,error); break; }
        }
        bytes=es_ck_u64(c,&bytes);
        if (bytes>INT64_MAX) es_ck_fail(c,ES_CK_NUMBER);
        if (!c->error && stage && stage->preparing)
        {
            error=stage_entry(stage,i,bytes,owners);
            if (error) es_ck_fail(c,error);
        }
    }
    if (!c->error && c->mode==ES_CK_VALIDATE && !stage->preparing)
    { stage->begin=begin; stage->end=c->position; stage->count=count; stage->validated=1; }
done:
    free(owners);
}

int es_ck_writes_prepare(EsCkWritesStage *s, const void *data, size_t size,
                         const EsCkWriteResources *resources)
{
    EsCkCursor c;
    int error;
    if (!s || !s->validated || s->preparing || s->prepared || s->committed ||
        s->entries || !data || s->end>size || !resources || !resources->prepare_prefix)
        return ES_CK_STATE;
    if (s->count && sizeof(*s->entries)>SIZE_MAX/(size_t)s->count) return ES_CK_BOUNDS;
    s->entries=calloc(s->count ? s->count : 1,sizeof(*s->entries));
    if (!s->entries) return ES_CK_MEMORY;
    s->resources=resources; s->preparing=1;
    es_ck_init(&c,ES_CK_VALIDATE,data,NULL,s->end); c.position=s->begin;
    es_ck_writes(&c,s); error=es_ck_finish(&c);
    s->resources=NULL; s->preparing=0;
    if (!error) s->prepared=1;
    return error;
}

int es_ck_writes_discard(EsCkWritesStage *s)
{
    uint32_t i;
    int error=0, next;
    if (!s) return 0;
    if (s->entries) for (i=0; i<s->count; i++)
    {
        struct EsCkWriteEntry *e=&s->entries[i];
        next=es_ck_read_close(s->committed ? &e->retired : &e->replacement);
        if (!error) error=next;
        free(e->path); free(e->retired_name);
    }
    free(s->entries); memset(s,0,sizeof(*s));
    return error;
}
