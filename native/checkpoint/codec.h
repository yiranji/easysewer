/* Internal checkpoint building blocks. No complete solver checkpoint API yet. */
#ifndef EASYSEWER_CHECKPOINT_CODEC_H
#define EASYSEWER_CHECKPOINT_CODEC_H

#include <stddef.h>
#include <stdint.h>
#include <stdio.h>

enum EsCkMode { ES_CK_MEASURE, ES_CK_WRITE, ES_CK_VALIDATE, ES_CK_APPLY };
enum EsCkError {
    ES_CK_OK, ES_CK_ARGUMENT, ES_CK_BOUNDS, ES_CK_MISMATCH,
    ES_CK_NUMBER, ES_CK_STATE, ES_CK_MEMORY, ES_CK_IO
};

typedef struct {
    enum EsCkMode mode;
    const unsigned char *input;
    unsigned char *output;
    size_t size, position;
    int error;
    /* Explicitly selected by the versioned coordinator; standalone historical
     * owner probes retain their original path-bound wire format. */
    int logical_resources;
} EsCkCursor;

void es_ck_init(EsCkCursor *c, enum EsCkMode mode,
               const void *input, void *output, size_t size);
void es_ck_fail(EsCkCursor *c, int error);
int es_ck_finish(EsCkCursor *c);
void es_ck_tag(EsCkCursor *c, const char *tag, size_t length);
void es_ck_identity(EsCkCursor *c, const char *value);
void es_ck_fixed_u32(EsCkCursor *c, uint32_t value);
void es_ck_fixed_i32(EsCkCursor *c, int value);
void es_ck_fixed_f64(EsCkCursor *c, double value);
void es_ck_f64(EsCkCursor *c, double *value);
double es_ck_f64_value(EsCkCursor *c, double *value);
int es_ck_i32(EsCkCursor *c, int *value, int minimum, int maximum);
uint64_t es_ck_count(EsCkCursor *c, long *value);
/* Returns the serialized value while validating, without changing *value. */
uint32_t es_ck_u32(EsCkCursor *c, uint32_t *value);
uint64_t es_ck_u64(EsCkCursor *c, uint64_t *value);

struct TActionList;
typedef struct {
    uint32_t capacity;
    int validated, prepared;
    struct TActionList *actions;
} EsCkControlsStage;

void es_ck_controls(EsCkCursor *c, EsCkControlsStage *stage);
int es_ck_controls_prepare(EsCkControlsStage *stage);
void es_ck_controls_discard(EsCkControlsStage *stage);

/* Only the engine owner can decide whether a native step is complete. */
int es_ck_engine_boundary(void);
void es_ck_engine(EsCkCursor *c);
void es_ck_routing(EsCkCursor *c, int active, double routing_time);
void es_ck_dynwave(EsCkCursor *c, int routing_active);
void es_ck_network(EsCkCursor *c);
void es_ck_inlet(EsCkCursor *c);
void es_ck_infiltration(EsCkCursor *c);
void es_ck_subcatchment(EsCkCursor *c);
void es_ck_gage(EsCkCursor *c);
void es_ck_runoff(EsCkCursor *c, int active);
void es_ck_climate(EsCkCursor *c);
void es_ck_groundwater(EsCkCursor *c);
void es_ck_snow(EsCkCursor *c);
void es_ck_lid(EsCkCursor *c);
void es_ck_massbal(EsCkCursor *c);
void es_ck_stats(EsCkCursor *c);
void es_ck_output(EsCkCursor *c);
void es_ck_ponding(EsCkCursor *c);

/* The coordinator must resolve captured, immutable, content-verified resources.
 * Each call returns an independently owned binary read stream at any position.
 * NULL returns an EsCkError. It must not change any live solver owner. */
typedef struct {
    void *context;
    FILE *(*open_read)(void *context, const char *identity, int *error);
} EsCkReadResources;
typedef EsCkReadResources EsCkTableResources;
int es_ck_read_position(FILE *file, uint64_t *position);
int es_ck_read_prepare(FILE **file, const EsCkReadResources *resources,
                       const char *identity, uint64_t position);
int es_ck_read_close(FILE **file);
struct EsCkTableOwner;
typedef struct {
    size_t begin, end;
    uint32_t count;
    int validated, preparing, prepared, committed, logical_resources;
    struct EsCkTableOwner *owners;
    const EsCkTableResources *resources;
} EsCkTablesStage;
void es_ck_tables(EsCkCursor *c, EsCkTablesStage *stage);
int es_ck_tables_prepare(EsCkTablesStage *stage, const void *data, size_t size,
                         const EsCkTableResources *resources);
/* Close staged resources on failure, or retired resources after commit.
 * A post-commit cleanup error MUST NOT be reported as an uncommitted restore. */
int es_ck_tables_discard(EsCkTablesStage *stage);
struct Table *es_ck_climate_table(int consumer);

/* Fixed role order: climate, rain, runoff USE, RDII, routing inflows.
 * Generated rain/RDII files are immutable inputs after successful start. */
#define ES_CK_READ_STREAMS 5
typedef struct {
    size_t begin, end;
    int validated, prepared, committed, logical_resources;
    int active[ES_CK_READ_STREAMS];
    uint64_t position[ES_CK_READ_STREAMS];
    FILE *replacement[ES_CK_READ_STREAMS], *retired[ES_CK_READ_STREAMS];
} EsCkStreamsStage;
void es_ck_streams(EsCkCursor *c, EsCkStreamsStage *stage);
int es_ck_streams_prepare(EsCkStreamsStage *stage, const EsCkReadResources *resources);
int es_ck_streams_discard(EsCkStreamsStage *stage);
const char *es_ck_stream_key(int role);
void es_ck_table_key(char *key, size_t capacity, int group, int index);
void es_ck_input_frames(EsCkCursor *c);
void es_ck_rdii(EsCkCursor *c, int active);
void es_ck_iface(EsCkCursor *c, int active);

/* Output providers create private, content-verified copies of captured prefixes.
 * They own temporary path cleanup. prepare_prefix must never alter a live or
 * external file. Paths/FILEs are process-local, never serialized. */
typedef struct {
    void *context;
    int (*prepare_prefix)(void *context, int role, uint32_t ordinal, int text,
                          uint64_t bytes, char *path, size_t capacity);
} EsCkWriteResources;
typedef struct {
    FILE **file;
    char *fixed_name;
    char **dynamic_name;
    size_t name_capacity;
    const char *identity, *component;
    int role, text;
    uint32_t ordinal;
} EsCkWriteOwner;
struct EsCkWriteEntry;
typedef struct {
    size_t begin, end;
    uint32_t count;
    int validated, preparing, prepared, committed;
    struct EsCkWriteEntry *entries;
    const EsCkWriteResources *resources;
} EsCkWritesStage;
/* Inventory is rebuilt from the project; NULL owners only counts entries. */
int es_ck_write_inventory(EsCkWriteOwner *owners, uint32_t capacity, uint32_t *count);
int es_ck_lid_write_inventory(EsCkWriteOwner *owners, uint32_t capacity, uint32_t *count);
int es_ck_report_boundary(void);
int es_ck_write_extent(FILE *file, uint64_t *bytes);
void es_ck_writes(EsCkCursor *c, EsCkWritesStage *stage);
int es_ck_writes_prepare(EsCkWritesStage *stage, const void *data, size_t size,
                         const EsCkWriteResources *resources);
int es_ck_writes_discard(EsCkWritesStage *stage);

#endif
