/* Explicit LE integers / IEEE binary64, never C structs, pointers or padding.
 * VALIDATE is read-only. APPLY is internal: callers must first validate ALL
 * modules, stage all allocations, and hold both input bytes and model fixed.
 * A partial module is never a publicly usable simulation checkpoint.
 */
#include "es_checkpoint.h"
#include <float.h>
#include <limits.h>
#include <math.h>
#include <string.h>

typedef char es_ck_byte_width[(CHAR_BIT == 8) ? 1 : -1];
typedef char es_ck_double_width[(sizeof(double) == 8 && DBL_MANT_DIG == 53 &&
                               DBL_MAX_EXP == 1024) ? 1 : -1];
typedef char es_ck_int_width[(sizeof(int) == 4 && INT_MAX == 2147483647) ? 1 : -1];

void es_ck_fail(EsCkCursor *c, int error)
{
    if (!c->error) c->error = error;
}

const char *es_ck_stream_key(int role)
{
    static const char *keys[] = {"swmm:input:climate", "swmm:input:rain",
        "swmm:input:runoff", "swmm:input:rdii", "swmm:input:routing"};
    return role >= 0 && role < ES_CK_READ_STREAMS ? keys[role] : NULL;
}

void es_ck_table_key(char *key, size_t capacity, int group, int index)
{
    /* IDs and ordered object identities are independently bound in the state.
     * Role namespaces cannot collide with each other or with physical paths. */
    snprintf(key, capacity, "swmm:table:%d:%d", group, index);
}

void es_ck_init(EsCkCursor *c, enum EsCkMode mode,
               const void *input, void *output, size_t size)
{
    memset(c, 0, sizeof(*c));
    c->mode = mode;
    c->input = input;
    c->output = output;
    c->size = size;
    if (mode < ES_CK_MEASURE || mode > ES_CK_APPLY ||
        (mode == ES_CK_WRITE && !output) ||
        ((mode == ES_CK_VALIDATE || mode == ES_CK_APPLY) && !input))
        c->error = ES_CK_ARGUMENT;
}

static int reserve(EsCkCursor *c, size_t length)
{
    if (c->error) return 0;
    if (length > SIZE_MAX - c->position ||
        (c->mode != ES_CK_MEASURE &&
         (c->position > c->size || length > c->size - c->position)))
    {
        es_ck_fail(c, ES_CK_BOUNDS);
        return 0;
    }
    return 1;
}

static uint64_t integer(EsCkCursor *c, uint64_t value, size_t length)
{
    size_t i;
    uint64_t result = value;
    if (!reserve(c, length)) return value;
    if (c->mode == ES_CK_WRITE)
        for (i = 0; i < length; i++)
            c->output[c->position + i] = (unsigned char)(value >> (8*i));
    else if (c->mode == ES_CK_VALIDATE || c->mode == ES_CK_APPLY)
    {
        result = 0;
        for (i = 0; i < length; i++)
            result |= (uint64_t)c->input[c->position + i] << (8*i);
    }
    c->position += length;
    return result;
}

int es_ck_finish(EsCkCursor *c)
{
    if (c->mode != ES_CK_MEASURE && c->position != c->size)
        es_ck_fail(c, ES_CK_BOUNDS);
    return c->error;
}

void es_ck_tag(EsCkCursor *c, const char *tag, size_t length)
{
    if (!reserve(c, length)) return;
    if (c->mode == ES_CK_WRITE) memcpy(c->output + c->position, tag, length);
    else if ((c->mode == ES_CK_VALIDATE || c->mode == ES_CK_APPLY) &&
             memcmp(c->input + c->position, tag, length))
        es_ck_fail(c, ES_CK_MISMATCH);
    c->position += length;
}

void es_ck_fixed_u32(EsCkCursor *c, uint32_t value)
{
    if (integer(c, value, 4) != value) es_ck_fail(c, ES_CK_MISMATCH);
}

void es_ck_fixed_i32(EsCkCursor *c, int value)
{
    es_ck_fixed_u32(c, (uint32_t)value);
}

void es_ck_identity(EsCkCursor *c, const char *value)
{
    size_t length;
    if (!value) { es_ck_fail(c, ES_CK_STATE); return; }
    length = strlen(value);
    if (length > UINT32_MAX) { es_ck_fail(c, ES_CK_BOUNDS); return; }
    es_ck_fixed_u32(c, (uint32_t)length);
    es_ck_tag(c, value, length);
}

uint32_t es_ck_u32(EsCkCursor *c, uint32_t *value)
{
    uint32_t result = (uint32_t)integer(c, *value, 4);
    if (!c->error && c->mode == ES_CK_APPLY) *value = result;
    return result;
}

uint64_t es_ck_u64(EsCkCursor *c, uint64_t *value)
{
    uint64_t result = integer(c, *value, 8);
    if (!c->error && c->mode == ES_CK_APPLY) *value = result;
    return result;
}

void es_ck_fixed_f64(EsCkCursor *c, double value)
{
    uint64_t bits;
    if (!isfinite(value)) { es_ck_fail(c, ES_CK_NUMBER); return; }
    memcpy(&bits, &value, 8);
    if (integer(c, bits, 8) != bits) es_ck_fail(c, ES_CK_MISMATCH);
}

double es_ck_f64_value(EsCkCursor *c, double *value)
{
    uint64_t bits;
    double result;
    /* Validation must inspect the input even if the destination differs. */
    memcpy(&bits, value, 8);
    bits = integer(c, bits, 8);
    memcpy(&result, &bits, 8);
    if (!isfinite(result)) es_ck_fail(c, ES_CK_NUMBER);
    if (!c->error && c->mode == ES_CK_APPLY) *value = result;
    return result;
}

void es_ck_f64(EsCkCursor *c, double *value)
{
    (void)es_ck_f64_value(c, value);
}

int es_ck_i32(EsCkCursor *c, int *value, int minimum, int maximum)
{
    uint32_t bits = (uint32_t)integer(c, (uint32_t)*value, 4);
    int64_t result = bits <= INT32_MAX ? (int64_t)bits : (int64_t)bits - 4294967296LL;
    if (minimum > maximum || result < minimum || result > maximum)
        es_ck_fail(c, ES_CK_NUMBER);
    if (!c->error && c->mode == ES_CK_APPLY) *value = (int)result;
    return (int)result;
}

uint64_t es_ck_count(EsCkCursor *c, long *value)
{
    uint64_t result = integer(c, (uint64_t)*value, 8);
    /* Windows long is 32-bit; Linux long is 64-bit. Never truncate. */
    if (result > (uint64_t)LONG_MAX) es_ck_fail(c, ES_CK_NUMBER);
    if (!c->error && c->mode == ES_CK_APPLY) *value = (long)result;
    return result;
}
