/* Linux read errors originate in a real stdio cookie, so ferror is set by
 * libc. Link the prepared solver and --wrap=fopen; no production test hooks. */
#define _GNU_SOURCE
#include <assert.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "swmm5.h"

static const char *input_path;
static char data[16384];
static size_t length, position, fault_offset;
static int inject, pass, fault_pass, faults, opens, closes;

FILE *__real_fopen(const char *, const char *);

static ssize_t read_cookie(void *cookie, char *buffer, size_t size)
{
    size_t n;
    (void)cookie;
    if (pass == fault_pass && position >= fault_offset)
    { errno = EIO; faults++; return -1; }
    if (position >= length) return 0;
    n = length-position;
    if (n > size) n = size;
    if (pass == fault_pass && n > fault_offset-position) n = fault_offset-position;
    memcpy(buffer, data+position, n);
    position += n;
    return (ssize_t)n;
}
static int seek_cookie(void *cookie, off64_t *offset, int whence)
{
    (void)cookie;
    if (whence != SEEK_SET || *offset != 0) { errno = EINVAL; return -1; }
    position = 0; pass++;
    return 0;
}
static int close_cookie(void *cookie)
{
    (void)cookie;
    closes++;
    return 0;
}
FILE *__wrap_fopen(const char *path, const char *mode)
{
    if (inject && strcmp(path, input_path) == 0)
    {
        cookie_io_functions_t io = {read_cookie, NULL, seek_cookie, close_cookie};
        FILE *stream;
        assert(strcmp(mode, "rb") == 0);
        position = 0; pass = 1; opens++;
        stream = fopencookie(NULL, "rb", io);
        assert(stream);
        return stream;
    }
    return __real_fopen(path, mode);
}
int main(int argc, char **argv)
{
    FILE *file;
    int round, code;
    size_t offsets[4];
    char sentinel[12];
    assert(argc == 4);
    input_path = argv[1];
    file = __real_fopen(input_path, "rb"); assert(file);
    length = fread(data, 1, sizeof(data), file);
    assert(length > 50 && length < sizeof(data)); fclose(file);
    offsets[0] = 0; offsets[1] = 8; offsets[2] = length/2; offsets[3] = length;
    for (fault_pass = 1; fault_pass <= 2; fault_pass++)
    for (round = 0; round < 4; round++)
    {
        fault_offset = offsets[round]; faults = 0; inject = 1;
        file = __real_fopen(argv[3], "wb"); assert(file);
        assert(fwrite("old-output", 1, 10, file) == 10); fclose(file);
        code = swmm_open(argv[1], argv[2], argv[3]);
        assert(code == 366 && faults > 0);
        assert(swmm_close() == 0);
        assert(swmm_close() == 0);
        assert(opens == closes);
        file = __real_fopen(argv[3], "rb"); assert(file);
        memset(sentinel, 0, sizeof(sentinel));
        assert(fread(sentinel, 1, sizeof(sentinel), file) == 10); fclose(file);
        assert(strcmp(sentinel, "old-output") == 0);
        inject = 0;
        assert(swmm_run(argv[1], argv[2], argv[3]) == 0);
    }
    puts("input: 8 real stdio read faults in two parser passes; close and retry passed");
    return 0;
}
