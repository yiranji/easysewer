/* Reuse actual stdio failures, then inject allocation failures specifically
 * while parsing long table/gage names. No test hooks enter released libraries. */
#define main read_faults_main
#include "native_path_read_faults.c"
#undef main

char *__real_Alloc(long);
void *__real_malloc(size_t);
static int path_count, fail_path, failure_mode, in_pool, malloc_count, hit;

void *__wrap_malloc(size_t size)
{
    if (in_pool && ++malloc_count == failure_mode)
    { hit++; return NULL; }
    return __real_malloc(size);
}

char *__wrap_Alloc(long size)
{
    char *result;
    if (size > 100 && ++path_count == fail_path)
    {
        if (failure_mode == 0) { hit++; return NULL; }
        /* Fill a pool block, so this real filename request must extend it.
         * The two allocation failures exercise both partial-block branches. */
        assert(__real_Alloc(64000));
        malloc_count = 0; in_pool = 1;
        result = __real_Alloc(size);
        in_pool = 0;
        return result;
    }
    return __real_Alloc(size);
}

static unsigned char *load_file(const char *path, size_t *size)
{
    unsigned char *bytes;
    FILE *file = fopen(path, "rb");
    long length;
    assert(file && fseek(file, 0, SEEK_END) == 0);
    length = ftell(file); assert(length >= 0 && length < 32*1024*1024);
    assert(fseek(file, 0, SEEK_SET) == 0);
    bytes = malloc((size_t)length+1); assert(bytes);
    assert(fread(bytes, 1, (size_t)length, file) == (size_t)length);
    bytes[length] = 0; assert(fclose(file) == 0);
    *size = (size_t)length;
    return bytes;
}

int main(int argc, char **argv)
{
    unsigned char *expected, *actual, *report;
    size_t expected_size, actual_size, report_size;
    int count, index, mode, code;
    FILE *file;
    assert(read_faults_main(argc, argv) == 0);
    path_count = 0;
    assert(swmm_run(argv[1], argv[2], argv[3]) == 0);
    count = path_count;
    assert(count >= 3 && count <= 32);
    expected = load_file(argv[3], &expected_size);
    for (index = 1; index <= count; index++)
    for (mode = 0; mode <= 2; mode++)
    {
        file = fopen(argv[3], "wb"); assert(file);
        assert(fwrite("old-output", 1, 10, file) == 10); fclose(file);
        path_count = hit = 0; fail_path = index; failure_mode = mode;
        code = swmm_open(argv[1], argv[2], argv[3]);
        assert(code == 200 && hit == 1);
        assert(swmm_close() == 0 && swmm_close() == 0);
        fail_path = 0;
        report = load_file(argv[2], &report_size);
        assert(strstr((char *)report, "ERROR 101") != NULL); free(report);
        actual = load_file(argv[3], &actual_size);
        assert(actual_size == 10 && memcmp(actual, "old-output", 10) == 0);
        free(actual);
        assert(swmm_run(argv[1], argv[2], argv[3]) == 0);
        actual = load_file(argv[3], &actual_size);
        assert(actual_size == expected_size && memcmp(actual, expected, expected_size) == 0);
        free(actual);
    }
    free(expected);
    printf("parsing: %d long input names, %d allocation faults; errors, cleanup, retry and full OUT passed\n",
           count, count*3);
    return 0;
}
