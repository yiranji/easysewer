/* easysewer SMO reader, preserving the EPA SWMM 5.2.4 public C ABI.
 * All input is local, seekable, little-endian OUT data. A handle owns one FILE
 * and bounded metadata; returned arrays belong to the caller via SMO_free.
 */
#define _FILE_OFFSET_BITS 64
#define _POSIX_C_SOURCE 200809L
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <limits.h>
#include <string.h>
#include <math.h>
#ifdef _WIN32
#include <windows.h>
#endif
#include "swmm_output.h"

#define MAGIC 516114522
#define META_LIMIT (64U * 1024U * 1024U)
#ifdef _WIN32
#define SEEK _fseeki64
#define TELL _ftelli64
#else
#define SEEK fseeko
#define TELL ftello
#endif

typedef struct { const unsigned char *text; int length; } name_t;
typedef struct { int code, column; } code_t;
typedef struct {
    int count, variables;
    name_t *names;
    code_t *codes;                 /* sorted lookup; column is on-disk order */
    int64_t offset;
} group_t;
typedef struct {
    FILE *file;
    unsigned char *metadata;
    group_t groups[5];
    const unsigned char *pollutant_units;
    size_t cursor, allocated;
    int error, parse_error, opened, units, periods, step;
    int64_t size, output, width;
    double start, first, last;
} data_t;

/* These builds intentionally use IEEE binary32/binary64 and 32-bit API ints. */
typedef char require_int32[sizeof(int) == 4 ? 1 : -1];
typedef char require_float32[sizeof(float) == 4 ? 1 : -1];
typedef char require_float64[sizeof(double) == 8 ? 1 : -1];

static int error(data_t *d, int code)
{
    if (d && code) d->error = code;
    return code;
}

static int32_t int32(const unsigned char *p)
{
    uint32_t u = (uint32_t)p[0] | (uint32_t)p[1] << 8 |
                 (uint32_t)p[2] << 16 | (uint32_t)p[3] << 24;
    int32_t value;
    memcpy(&value, &u, 4);
    return value;
}

static float real32(const unsigned char *p)
{
    int32_t bits = int32(p);
    float value;
    memcpy(&value, &bits, 4);
    return value;
}

static double real64(const unsigned char *p)
{
    uint64_t bits = 0;
    double value;
    int i;
    for (i = 0; i < 8; i++) bits |= (uint64_t)p[i] << (8 * i);
    memcpy(&value, &bits, 8);
    return value;
}

static int calendar(double value)
{
    return isfinite(value) && value >= -693593.0 && value < 2958466.0;
}

static int64_t file_size(FILE *file)
{
    if (SEEK(file, 0, SEEK_END) != 0) return -1;
    return (int64_t)TELL(file);
}

static FILE *open_file(const char *path, int *code)
{
#ifdef _WIN32
    int length = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, path, -1, NULL, 0);
    wchar_t *wide;
    FILE *file;
    if (length < 1 || length > 32767) return NULL;
    wide = malloc((size_t)length * sizeof(wchar_t));
    if (!wide) { *code = 411; return NULL; }
    if (!MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, path, -1, wide, length)) {
        free(wide);
        return NULL;
    }
    file = _wfopen(wide, L"rb");
    free(wide);
    return file;
#else
    (void)code;
    return fopen(path, "rb");
#endif
}

static int read_at(data_t *d, int64_t position, void *out, size_t count)
{
    if (position < 0 || position > d->size || count > (uint64_t)(d->size - position) ||
        SEEK(d->file, position, SEEK_SET) != 0 ||
        fread(out, 1, count, d->file) != count || ferror(d->file)) return 435;
    return 0;
}

static int release_file(data_t *d)
{
    int g, code = 0, previous = d->error;
    if (d->file && fclose(d->file) != 0) code = 435;
    for (g = 0; g < 5; g++) {
        free(d->groups[g].names);
        free(d->groups[g].codes);
    }
    free(d->metadata);
    memset(d, 0, sizeof(*d));
    d->error = previous;
    return code;
}

static void *meta_alloc(data_t *d, size_t count, size_t width)
{
    void *p;
    if (!count) return NULL;
    if (width > META_LIMIT || count > (META_LIMIT - d->allocated) / width) {
        d->parse_error = 411;
        return NULL;
    }
    p = calloc(count, width);
    if (!p) d->parse_error = 411;
    else d->allocated += count * width;
    return p;
}

static const unsigned char *take(data_t *d, size_t count, size_t limit)
{
    const unsigned char *p;
    if (d->parse_error) return NULL;
    if (d->cursor > limit || count > limit - d->cursor) {
        d->parse_error = 435;
        return NULL;
    }
    p = d->metadata + d->cursor;
    d->cursor += count;
    return p;
}

static int integer(data_t *d, size_t limit)
{
    const unsigned char *p = take(d, 4, limit);
    return p ? int32(p) : 0;
}

static unsigned char fold(unsigned char c)
{
    return c >= 'a' && c <= 'z' ? (unsigned char)(c - 'a' + 'A') : c;
}

static int name_compare(const void *a, const void *b)
{
    const name_t *x = a, *y = b;
    int i, common = x->length < y->length ? x->length : y->length;
    for (i = 0; i < common; i++) {
        int diff = (int)fold(x->text[i]) - (int)fold(y->text[i]);
        if (diff) return diff;
    }
    return (x->length > y->length) - (x->length < y->length);
}

static int code_compare(const void *a, const void *b)
{
    int x = ((const code_t *)a)->code, y = ((const code_t *)b)->code;
    return (x > y) - (x < y);
}

static int parse_names(data_t *d, group_t *group, size_t limit)
{
    int i;
    name_t *sorted;
    group->names = meta_alloc(d, (size_t)group->count, sizeof(name_t));
    if (d->parse_error) return d->parse_error;
    for (i = 0; i < group->count; i++) {
        int length = integer(d, limit);
        const unsigned char *p;
        if (d->parse_error) return d->parse_error;
        if (length < 1 || length > 65536) return 435;
        p = take(d, (size_t)length, limit);
        if (!p) return d->parse_error;
        if (memchr(p, 0, (size_t)length)) return 435;
        group->names[i].text = p;
        group->names[i].length = length;
    }
    if (group->count < 2) return 0;
    sorted = meta_alloc(d, (size_t)group->count, sizeof(name_t));
    if (!sorted) return d->parse_error;
    memcpy(sorted, group->names, (size_t)group->count * sizeof(name_t));
    qsort(sorted, (size_t)group->count, sizeof(name_t), name_compare);
    for (i = 1; i < group->count; i++) {
        if (!name_compare(sorted + i - 1, sorted + i)) break;
    }
    free(sorted);
    d->allocated -= (size_t)group->count * sizeof(name_t);
    return i == group->count ? 0 : 435;
}

static int parse_layout(data_t *d)
{
    unsigned char footer[24], header[28], stamp[8];
    int ids, input, status, g, j, code;
    int64_t total_names = 0, width = 8;
    size_t limit;
    if (d->size < 52 || read_at(d, d->size - 24, footer, 24) ||
        read_at(d, 0, header, 28)) return 435;
    ids = int32(footer); input = int32(footer + 4);
    d->output = int32(footer + 8); d->periods = int32(footer + 12);
    status = int32(footer + 16);
    if (int32(header) != MAGIC || int32(footer + 20) != MAGIC ||
        int32(header + 4) != 52004 || ids != 28 || input < ids ||
        d->output <= input || d->output > d->size - 24 ||
        d->periods < 0 || status < 0) return 435;
    if (d->output > META_LIMIT) return 411;
    d->units = int32(header + 8);
    if (d->units < 0 || d->units > 5) return 435;
    for (g = 0; g < 5; g++) {
        int count = g == 3 ? 1 : int32(header + (g == 4 ? 24 : 12 + 4 * g));
        if (count < 0) return 435;
        d->groups[g].count = count;
        if (g != 3) total_names += count;
    }
    if (5 * total_names + 4 * (int64_t)d->groups[4].count > input - ids) return 435;
    limit = (size_t)d->output;
    d->metadata = meta_alloc(d, limit, 1);
    if (!d->metadata) return d->parse_error;
    if (read_at(d, 0, d->metadata, limit)) return 435;
    /* Detect a concurrently replaced header during this initial read. */
    if (memcmp(header, d->metadata, 28)) return 435;
    d->cursor = 28;
    for (g = 0; g < 5; g++) {
        if (g == 3) continue;
        code = parse_names(d, d->groups + g, (size_t)input);
        if (code) return code;
    }
    d->pollutant_units = take(d, (size_t)d->groups[4].count * 4, (size_t)input);
    if (d->parse_error) return d->parse_error;
    for (j = 0; j < d->groups[4].count; j++) {
        code = int32(d->pollutant_units + 4 * (size_t)j);
        if (code < 0 || code > 2) return 435;
    }
    if (d->cursor != (size_t)input) return 435;
    for (g = 0; g < 3; g++) {
        int count = integer(d, limit), i;
        const unsigned char *codes, *values;
        size_t cells;
        if (d->parse_error) return d->parse_error;
        if (count < 0 || count > 64) return 435;
        codes = take(d, (size_t)count * 4, limit);
        /* Actual metadata bounds precede the multiplication/allocation. */
        if ((uint64_t)count * d->groups[g].count > limit / 4) return 435;
        cells = (size_t)count * d->groups[g].count;
        values = take(d, cells * 4, limit);
        if (d->parse_error) return d->parse_error;
        for (i = 0; i < count; i++) {
            if (int32(codes + 4 * i) == 0) continue;
            for (j = 0; j < d->groups[g].count; j++) {
                if (!isfinite(real32(values + 4 * ((size_t)j * count + i)))) return 435;
            }
        }
    }
    for (g = 0; g < 4; g++) {
        group_t *group = d->groups + g;
        const unsigned char *codes;
        int count = integer(d, limit);
        int64_t bytes;
        if (d->parse_error) return d->parse_error;
        if (count < 1 || (int64_t)count > (int64_t)d->groups[4].count + 64) return 435;
        if ((uint64_t)count > limit / 4) return 435;
        codes = take(d, (size_t)count * 4, limit);
        if (!codes) return d->parse_error;
        group->variables = count;
        group->codes = meta_alloc(d, (size_t)count, sizeof(code_t));
        if (!group->codes) return d->parse_error;
        for (j = 0; j < count; j++) {
            code = int32(codes + (size_t)j * 4);
            if (code < 0) return 435;
            group->codes[j].code = code;
            group->codes[j].column = j;
        }
        qsort(group->codes, (size_t)count, sizeof(code_t), code_compare);
        for (j = 1; j < count; j++) {
            if (group->codes[j - 1].code == group->codes[j].code) return 435;
        }
        group->offset = width;
        if (group->count && (int64_t)count > (INT64_MAX - width) / 4 / group->count) return 435;
        bytes = (int64_t)count * group->count * 4;
        width += bytes;
    }
    {
        const unsigned char *start = take(d, 8, limit);
        if (!start) return d->parse_error;
        d->start = real64(start);
        d->step = integer(d, limit);
    }
    if (d->parse_error) return d->parse_error;
    if (d->cursor != limit || d->step <= 0 || !calendar(d->start)) return 435;
    d->width = width;
    if ((d->size - 24 - d->output) % width ||
        (d->size - 24 - d->output) / width != d->periods) return 435;
    if (!d->periods) return 436; /* Retained diagnostic C API convention. */
    if (read_at(d, d->output, stamp, 8)) return 435;
    d->first = real64(stamp);
    if (read_at(d, d->output + (d->periods - 1) * width, stamp, 8)) return 435;
    d->last = real64(stamp);
    if (!calendar(d->first) || !calendar(d->last) || d->last < d->first) return 435;
    d->opened = 1;
    return status ? 10 : 0; /* Diagnostic data; not proof of a successful run. */
}

static int ready(data_t *d)
{
    if (!d) return -1;
    if (!d->opened || !d->file) return 434;
    if (file_size(d->file) != d->size) return 435;
    return 0;
}

static int column(group_t *group, int code)
{
    code_t key, *found;
    key.code = code; key.column = 0;
    found = bsearch(&key, group->codes, (size_t)group->variables, sizeof(code_t), code_compare);
    return found ? found->column : -1;
}

static int time_at(data_t *d, int period, double *time)
{
    unsigned char bytes[8];
    if (read_at(d, d->output + (int64_t)period * d->width, bytes, 8)) return 435;
    *time = real64(bytes);
    return calendar(*time) && *time >= d->first && *time <= d->last ? 0 : 435;
}

static int value_at(data_t *d, int group, int period, int index, int col, float *value)
{
    unsigned char bytes[4];
    group_t *g = d->groups + group;
    int64_t position = d->output + (int64_t)period * d->width + g->offset +
                       ((int64_t)index * g->variables + col) * 4;
    if (read_at(d, position, bytes, 4)) return 435;
    *value = real32(bytes);
    return isfinite(*value) ? 0 : 435;
}

static int query(data_t *d, int group, int index, int attr, int start, int end,
                 int mode, float **out, int *length)
{
    /* mode: 0 = one attribute over time; 1 = all elements at one time;
     *       2 = all saved variables for one element at one time. */
    int code, col = 0, count, i;
    float *values;
    double previous = 0, time;
    group_t *g;
    if (out) *out = NULL;
    if (length) *length = 0;
    if (!d) return -1;
    if (!out || !length) return error(d, 424);
    code = ready(d);
    if (code) return error(d, code);
    g = d->groups + group;
    if (mode != 1 && (index < 0 || index >= g->count)) return error(d, 423);
    if (start < 0 || start >= d->periods || end <= start || end > d->periods) return error(d, 422);
    if (mode != 2) {
        col = column(g, attr);
        if (col < 0) return error(d, 421);
    }
    count = mode == 0 ? end - start : (mode == 1 ? g->count : g->variables);
    if ((uint64_t)count > SIZE_MAX / sizeof(float)) return error(d, 411);
    /* Empty groups legitimately return an empty attribute array. */
    if (!count) return 0;
    values = malloc((size_t)count * sizeof(float));
    if (!values) return error(d, 411);
    if (mode != 0) code = time_at(d, start, &time);
    for (i = 0; i < count && !code; i++) {
        int period = mode == 0 ? start + i : start;
        if (mode == 0) {
            code = time_at(d, period, &time);
            if (!code && i && time <= previous) code = 435;
            if (!code) previous = time;
        }
        if (!code) code = value_at(d, group, period, mode == 1 ? i : index,
                                   mode == 2 ? i : col, values + i);
    }
    if (code) { free(values); return error(d, code); }
    *out = values; *length = count;
    return 0;
}

int EXPORT_OUT_API swmm_getEasySewerOutputIO(void) { return 1; }

int EXPORT_OUT_API SMO_init(SMO_Handle *handle)
{
    if (!handle) return -1;
    *handle = calloc(1, sizeof(data_t));
    return *handle ? 0 : 411;
}

int EXPORT_OUT_API SMO_close(SMO_Handle *handle)
{
    int code;
    if (!handle) return -1;
    if (!*handle) return 0;
    code = release_file((data_t *)*handle);
    free(*handle); *handle = NULL;
    return code;
}

int EXPORT_OUT_API SMO_open(SMO_Handle handle, const char *path)
{
    data_t *d = handle;
    int code;
    if (!d) return -1;
    if (!path || !*path) return error(d, 421);
    code = release_file(d);
    if (code) return error(d, code);
    code = 434;
    d->file = open_file(path, &code);
    if (!d->file) return error(d, code);
    d->size = file_size(d->file);
    code = parse_layout(d);
    if (code != 0 && code != 10) {
        /* Preserve the primary error, and leave the caller's handle alive. */
        release_file(d);
    }
    return error(d, code);
}

static int scalar(data_t *d, int *out, int what)
{
    int code;
    if (out) *out = 0;
    if (!d) return -1;
    if (!out) return error(d, 424);
    code = ready(d);
    if (code) return error(d, code);
    *out = what == 0 ? 52004 : (what == 1 ? d->units : (what == 2 ? d->step : d->periods));
    return 0;
}

int EXPORT_OUT_API SMO_getVersion(SMO_Handle h, int *out) { return scalar(h, out, 0); }
int EXPORT_OUT_API SMO_getFlowUnits(SMO_Handle h, int *out) { return scalar(h, out, 1); }
int EXPORT_OUT_API SMO_getTimes(SMO_Handle h, SMO_time code, int *out)
{
    if (out) *out = 0;
    if (!h) return -1;
    if (code != SMO_reportStep && code != SMO_numPeriods) return error(h, 421);
    return scalar(h, out, code == SMO_reportStep ? 2 : 3);
}

int EXPORT_OUT_API SMO_getStartDate(SMO_Handle h, double *out)
{
    data_t *d = h;
    int code;
    if (out) *out = 0;
    if (!d) return -1;
    if (!out) return error(d, 424);
    code = ready(d);
    if (code) return error(d, code);
    *out = d->start;
    return 0;
}

static int integers(data_t *d, int **out, int *length, int mode)
{
    int code, count, i, *values;
    if (out) *out = NULL;
    if (length) *length = 0;
    if (!d) return -1;
    if (!out || !length) return error(d, 424);
    code = ready(d);
    if (code) return error(d, code);
    count = mode == 0 ? 5 : d->groups[4].count;
    if (mode == 1) count = count ? count + 2 : 3;
    if (!count) return 0;
    values = malloc((size_t)count * sizeof(int));
    if (!values) return error(d, 411);
    for (i = 0; i < count; i++) {
        if (mode == 0) values[i] = d->groups[i].count;
        else if (mode == 1 && i < 2) values[i] = i ? d->units : d->units >= 3;
        else if (!d->groups[4].count) values[i] = SMO_NONE;
        else values[i] = int32(d->pollutant_units + (size_t)(i - (mode == 1 ? 2 : 0)) * 4);
    }
    *out = values; *length = count;
    return 0;
}

int EXPORT_OUT_API SMO_getProjectSize(SMO_Handle h, int **out, int *n) { return integers(h, out, n, 0); }
int EXPORT_OUT_API SMO_getUnits(SMO_Handle h, int **out, int *n) { return integers(h, out, n, 1); }
int EXPORT_OUT_API SMO_getPollutantUnits(SMO_Handle h, int **out, int *n) { return integers(h, out, n, 2); }

int EXPORT_OUT_API SMO_getElementName(SMO_Handle h, SMO_elementType type, int index, char **out, int *size)
{
    data_t *d = h;
    int code;
    name_t *name;
    char *value;
    if (out) *out = NULL;
    if (size) *size = 0;
    if (!d) return -1;
    if (!out || !size) return error(d, 424);
    code = ready(d);
    if (code) return error(d, code);
    if ((int)type < 0 || type > SMO_pollut || type == SMO_sys) return error(d, 421);
    if (index < 0 || index >= d->groups[type].count) return error(d, 423);
    name = d->groups[type].names + index;
    value = malloc((size_t)name->length + 1);
    if (!value) return error(d, 411);
    memcpy(value, name->text, (size_t)name->length);
    value[name->length] = 0;
    *out = value; *size = name->length;
    return 0;
}

#define GROUP_API(Name, Type, G) \
int EXPORT_OUT_API SMO_get##Name##Series(SMO_Handle h, int index, Type attr, int start, int end, float **out, int *n) \
{ return query(h, G, index, (int)attr, start, end, 0, out, n); } \
int EXPORT_OUT_API SMO_get##Name##Attribute(SMO_Handle h, int time, Type attr, float **out, int *n) \
{ return query(h, G, 0, (int)attr, time, time == INT_MAX ? time : time + 1, 1, out, n); } \
int EXPORT_OUT_API SMO_get##Name##Result(SMO_Handle h, int time, int index, float **out, int *n) \
{ return query(h, G, index, 0, time, time == INT_MAX ? time : time + 1, 2, out, n); }

GROUP_API(Subcatch, SMO_subcatchAttribute, 0)
GROUP_API(Node, SMO_nodeAttribute, 1)
GROUP_API(Link, SMO_linkAttribute, 2)

int EXPORT_OUT_API SMO_getSystemSeries(SMO_Handle h, SMO_systemAttribute attr, int start, int end, float **out, int *n)
{ return query(h, 3, 0, (int)attr, start, end, 0, out, n); }
int EXPORT_OUT_API SMO_getSystemAttribute(SMO_Handle h, int time, SMO_systemAttribute attr, float **out, int *n)
{ return query(h, 3, 0, (int)attr, time, time == INT_MAX ? time : time + 1, 1, out, n); }
int EXPORT_OUT_API SMO_getSystemResult(SMO_Handle h, int time, int dummy, float **out, int *n)
{
    (void)dummy; /* Legacy API explicitly ignores this argument. */
    return query(h, 3, 0, 0, time, time == INT_MAX ? time : time + 1, 2, out, n);
}

void EXPORT_OUT_API SMO_free(void **array)
{
    if (array) { free(*array); *array = NULL; }
}

void EXPORT_OUT_API SMO_clearError(SMO_Handle h)
{
    if (h) ((data_t *)h)->error = 0;
}

int EXPORT_OUT_API SMO_checkError(SMO_Handle h, char **message)
{
    data_t *d = h;
    const char *text;
    if (message) *message = NULL;
    if (!d) return -1;
    if (!message) return error(d, 424);
    switch (d->error) {
    case 0: return 0;
    case 10: text = "Warning 10: OUT footer records a simulation error"; break;
    case 411: text = "Error 411: memory allocation or metadata budget exceeded"; break;
    case 421: text = "Error 421: invalid parameter or unavailable variable code"; break;
    case 422: text = "Error 422: reporting period range is invalid"; break;
    case 423: text = "Error 423: element index is out of range"; break;
    case 424: text = "Error 424: missing output pointer"; break;
    case 434: text = "Error 434: no readable OUT file is open"; break;
    case 435: text = "Error 435: invalid OUT data or failed file I/O"; break;
    case 436: text = "Error 436: OUT contains no result periods"; break;
    default: text = "Error 440: unspecified output reader error"; break;
    }
    *message = malloc(strlen(text) + 1);
    if (!*message) return 411; /* Keep the original sticky error retrievable. */
    strcpy(*message, text);
    return d->error;
}
