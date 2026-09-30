/* Prepended only to the instrumented writes translation unit. */
#include "es_checkpoint.h"
#include "swmm5.h"
#include <stdlib.h>
#include <sys/types.h>
static int es_write_fail=-1, es_write_calls=0, es_write_hit=0;
void DLLEXPORT es_test_writes_arm(int at)
{ es_write_fail=at; es_write_calls=es_write_hit=0; }
int DLLEXPORT es_test_writes_calls(void) { return es_write_calls; }
int DLLEXPORT es_test_writes_hit(void) { return es_write_hit; }
static int es_test_write_fault(void)
{
    if (es_write_calls++==es_write_fail) { es_write_hit=1; return 1; }
    return 0;
}
static void *es_test_write_calloc(size_t n,size_t size)
{ return es_test_write_fault() ? NULL : calloc(n,size); }
static FILE *es_test_write_open(const char *path,const char *mode)
{ return es_test_write_fault() ? NULL : fopen(path,mode); }
static int es_test_write_flush(FILE *f)
{ return es_test_write_fault() ? EOF : fflush(f); }
static int es_test_write_close(FILE **f)
{
    int fail=es_test_write_fault(),error=es_ck_read_close(f);
    return fail ? ES_CK_IO : error;
}
#ifdef _WIN32
static int es_test_write_seek(FILE *f,__int64 off,int whence)
{ return es_test_write_fault() ? -1 : _fseeki64(f,off,whence); }
#define _fseeki64 es_test_write_seek
#else
static int es_test_write_seek(FILE *f,off_t off,int whence)
{ return es_test_write_fault() ? -1 : fseeko(f,off,whence); }
#define fseeko es_test_write_seek
#endif
#define calloc es_test_write_calloc
#define fopen es_test_write_open
#define fflush es_test_write_flush
#define es_ck_read_close es_test_write_close
