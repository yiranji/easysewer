/* Compile with -I native/path_io and -I prepared/src/solver. This exercises
 * the exact shared path implementation without contacting any UNC server. */
#define _GNU_SOURCE
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#ifdef WINDOWS
#include <windows.h>
#endif
#define MAXFNAME 4095
#define MAXMSG 1024
#define ERR_INP_FILE 303
#define ERR_FILE_PATH 364
#define ERR_MEMORY 101
static char InpDir[MAXFNAME+1], ErrorMsg[MAXMSG+1];
static int ErrorCode;
static void report_writeErrorMsg(int code, const char *message)
{ (void)message; ErrorCode = code; }
static void report_writeLine(const char *message) { (void)message; }
static void sstrncpy(char *dest, const char *source, size_t n)
{ size_t length = strlen(source); if (length > n) length = n;
  memcpy(dest, source, length); dest[length] = 0; }
#include "paths.c"
#include "mempool.c"

static void parent(const char *name, const char *expected)
{
    char actual[MAXFNAME+1];
    ErrorCode = 0;
    getAbsolutePath(name, actual, sizeof(actual));
    assert(ErrorCode == 0 && strcmp(actual, expected) == 0);
}
int main(void)
{
    char source[MAXFNAME+2], *stored, *prior;
    int length, i, code;
    struct { char name[MAXFNAME+1]; unsigned char guard[8]; } result;
    assert(isRelativePath("rain.dat") && isRelativePath("a/rain.dat"));
    assert(!isRelativePath("/a/rain.dat"));
#ifdef WINDOWS
    assert(!isRelativePath("C:rain.dat"));
    assert(!isRelativePath("\\rain.dat"));
    assert(!isRelativePath("\\\\server\\share\\rain.dat"));
    assert(isRelativePath("rain:2026.dat"));
    parent("C:/alpha/beta/model.inp", "C:\\alpha\\beta\\");
    parent("C:\\alpha/beta/model.inp", "C:\\alpha\\beta\\");
    parent("\\\\server\\share\\alpha\\model.inp", "\\\\server\\share\\alpha\\");
    parent("\\\\?\\C:\\alpha\\model.inp", "\\\\?\\C:\\alpha\\");
#else
    assert(isRelativePath("C:rain.dat"));
    assert(isRelativePath("\\rain.dat"));
    assert(isRelativePath("rain:2026.dat"));
    parent("/alpha/beta/model.inp", "/alpha/beta/");
    parent("/alpha/beta\\model.inp", "/alpha/");
    parent("/alpha/beta:model.inp", "/alpha/");
#endif
    for (length = 4094; length <= 4096; length++)
    {
        memset(source, 'x', length); source[length] = 0;
        memset(&result, 0x5a, sizeof(result));
        code = es_makeFileName(result.name, source, 0);
        assert(code == (length <= 4095));
        assert(strlen(result.name) == (length <= 4095 ? (size_t)length : 0));
        for (i = 0; i < 8; i++) assert(result.guard[i] == 0x5a);
    }
    strcpy(InpDir, "/a/");
    memset(source, 'x', 4092); source[4092] = 0;
    assert(es_makeFileName(result.name, source, 1));
    assert(strlen(result.name) == 4095);
    source[4092] = 'x'; source[4093] = 0;
    assert(!es_makeFileName(result.name, source, 1) && !result.name[0]);
    strcpy(result.name, "relative.dat");
    assert(es_makeFileName(result.name, result.name, 1));
    assert(strcmp(result.name, "/a/relative.dat") == 0);
    assert(AllocInit());
    stored = "original";
    assert(es_storeInputPath(&stored, "series.dat") == 0);
    assert(strcmp(stored, "/a/series.dat") == 0);
    prior = stored;
    memset(source, 'x', 4096); source[4096] = 0;
    assert(es_storeInputPath(&stored, source) == ERR_FILE_PATH && stored == prior);
    AllocFreePool();
    stored = "original";
    assert(es_storeInputPath(&stored, "series.dat") == ERR_MEMORY);
    assert(strcmp(stored, "original") == 0);
    puts("path syntax, capacity, aliasing and input-name ownership passed");
    return 0;
}
