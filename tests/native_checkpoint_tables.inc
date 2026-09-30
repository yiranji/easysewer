/* Only instrumented development libraries export these fault/test hooks. */
#include "swmm5.h"

static int es_test_table_fail = -1, es_test_table_calls, es_test_table_hit;
static int es_test_table_fault(int kind)
{
    int hit = es_test_table_calls++ == es_test_table_fail;
    if (hit) es_test_table_hit = kind;
    return hit;
}
void es_test_table_arm(int fail)
{ es_test_table_fail = fail; es_test_table_calls = es_test_table_hit = 0; }
int DLLEXPORT es_test_tables_calls(void) { return es_test_table_calls; }
int DLLEXPORT es_test_tables_hit(void) { return es_test_table_hit; }
void *es_test_table_calloc(size_t n, size_t size)
{ return es_test_table_fault(1) ? NULL : calloc(n, size); }
int es_test_table_close(FILE *file)
{ int failed = es_test_table_fault(5), result = fclose(file); return failed ? EOF : result; }
#ifdef _WIN32
int es_test_table_seek(FILE *file, __int64 offset, int origin)
{ return es_test_table_fault(3) ? -1 : _fseeki64(file, offset, origin); }
__int64 es_test_table_tell(FILE *file)
{ return es_test_table_fault(4) ? -1 : _ftelli64(file); }
#else
int es_test_table_seek(FILE *file, off_t offset, int origin)
{ return es_test_table_fault(3) ? -1 : fseeko(file, offset, origin); }
off_t es_test_table_tell(FILE *file)
{ return es_test_table_fault(4) ? -1 : ftello(file); }
#endif
FILE *DLLEXPORT es_test_tables_open_read(const char *path, int *error)
{
    FILE *file;
    if (es_test_table_fault(2)) { *error = ES_CK_IO; return NULL; }
    file = fopen(path, "rb");
    *error = file ? 0 : ES_CK_IO;
    return file;
}

static TTable *es_test_table_root(int index)
{
    int i;
    if (index < 0) return NULL;
    if (index < Nobjects[CURVE]) return &Curve[index];
    index -= Nobjects[CURVE];
    if (index < Nobjects[TSERIES]) return &Tseries[index];
    index -= Nobjects[TSERIES];
    for (i = 0; i < 2; i++) if (es_ck_climate_table(i))
    { if (!index--) return es_ck_climate_table(i); }
    return NULL;
}
const char *DLLEXPORT es_test_tables_resource(int index)
{
    TTable *table = es_test_table_root(index);
    return table ? table->file.name : NULL;
}
void DLLEXPORT es_test_tables_drop(void)
{
    TTable *table;
    int i;
    es_test_table_arm(-1);
    for (i = 0; (table = es_test_table_root(i)) != NULL; i++)
    {
        es_ck_table_release(table);
        table->lastDate = table->x1 = table->x2 = NAN;
        table->y1 = table->y2 = table->filePrevious = NAN;
        table->thisEntry = NULL;
        table->lookupReady = table->lookupEnded = table->fileHasPrevious = 0;
    }
}
double DLLEXPORT es_test_tables_lookup(int index, double date, int extend)
{
    TTable *table = es_test_table_root(index);
    return table ? table_tseriesLookup(table, date, (char)extend) : NAN;
}
int DLLEXPORT es_test_tables_next(int index, double *x, double *y)
{
    TTable *table = es_test_table_root(index);
    return table ? table_getNextEntry(table, x, y) : -1;
}
