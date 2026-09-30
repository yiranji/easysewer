/* Fail each real table-stream close after libc releases its descriptor.
 * ASan verifies diagnostic filenames remain alive until every borrower closes. */
#include <assert.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "swmm5.h"

static const char *series_path;
static FILE *streams[32];
static int armed, seen, fail_at, faults;
FILE *__real_fopen(const char *, const char *);
int __real_fclose(FILE *);

FILE *__wrap_fopen(const char *path, const char *mode)
{
    FILE *file = __real_fopen(path, mode);
    int i;
    if (file && strcmp(path, series_path) == 0)
    {
        for (i = 0; i < 32 && streams[i]; i++);
        assert(i < 32);
        streams[i] = file;
    }
    return file;
}

int __wrap_fclose(FILE *file)
{
    int i, tracked = 0, result;
    for (i = 0; i < 32; i++) if (streams[i] == file)
    { streams[i] = NULL; tracked = 1; }
    result = __real_fclose(file);
    if (tracked && armed && ++seen == fail_at)
    { faults++; errno = EIO; return EOF; }
    return result;
}

int main(int argc, char **argv)
{
    char report[65536], reference[65536], actual[65536];
    FILE *file;
    size_t count, expected;
    int i, available = 0;
    assert(argc == 6);
    series_path = argv[4];
    fail_at = atoi(argv[5]);
    assert(swmm_run(argv[1], argv[2], argv[3]) == 0);
    file = __real_fopen(argv[3], "rb"); assert(file);
    expected = fread(reference, 1, sizeof(reference), file);
    assert(expected > 100 && expected < sizeof(reference));
    __real_fclose(file);
    assert(swmm_open(argv[1], argv[2], argv[3]) == 0);
    assert(swmm_start(1) == 0);
    for (i = 0; i < 32; i++) available += streams[i] != NULL;
    assert(fail_at > 0 && fail_at <= available);
    armed = 1;
    assert(swmm_close() != 0);
    armed = 0;
    assert(faults == 1);
    assert(swmm_close() == 0);
    for (i = 0; i < 32; i++) assert(streams[i] == NULL);
    file = __real_fopen(argv[2], "rb"); assert(file);
    count = fread(report, 1, sizeof(report)-1, file);
    report[count] = 0; __real_fclose(file);
    assert(strstr(report, series_path));
    assert(swmm_run(argv[1], argv[2], argv[3]) == 0);
    file = __real_fopen(argv[3], "rb"); assert(file);
    count = fread(actual, 1, sizeof(actual), file); __real_fclose(file);
    assert(count == expected && memcmp(actual, reference, count) == 0);
    for (i = 0; i < 32; i++) assert(streams[i] == NULL);
    printf("table close %d/%d: diagnostic path, repeated close and retry passed\n", fail_at, available);
    return 0;
}
