/* Exact shared helpers, with test-only failures at OS/CRT ownership transfer.
 * No macros or hooks from this harness are compiled into the solver DLL/SO. */
#define main syntax_checks_main
#include "native_path_syntax.c"
#undef main
#include <errno.h>
#ifdef WINDOWS
#include <direct.h>
#include <io.h>
#include <fcntl.h>
#else
#include <dirent.h>
#include <unistd.h>
#endif

static char TempDir[MAXFNAME+1];
static int fail_create, fail_transfer, fail_stream, injected;

#ifdef WINDOWS
static HANDLE test_create(LPCWSTR name, DWORD access, DWORD sharing,
                          LPSECURITY_ATTRIBUTES security, DWORD disposition,
                          DWORD flags, HANDLE template)
{
    if (fail_create) { injected++; SetLastError(ERROR_DISK_FULL); return INVALID_HANDLE_VALUE; }
    return CreateFileW(name, access, sharing, security, disposition, flags, template);
}
static int test_transfer(intptr_t handle, int flags)
{
    if (fail_transfer) { injected++; errno = ENOMEM; return -1; }
    return _open_osfhandle(handle, flags);
}
static FILE *test_stream(int fd, const char *mode)
{
    if (fail_stream) { injected++; errno = ENOMEM; return NULL; }
    return _fdopen(fd, mode);
}
#define CreateFileW test_create
#define _open_osfhandle test_transfer
#define _fdopen test_stream
#else
static int test_create(char *name)
{
    if (fail_create) { injected++; errno = ENOSPC; return -1; }
    return mkstemp(name);
}
static FILE *test_stream(int fd, const char *mode)
{
    if (fail_stream) { injected++; errno = ENOMEM; return NULL; }
    return fdopen(fd, mode);
}
#define mkstemp test_create
#define fdopen test_stream
#endif
#include "scratch.c"
#ifdef WINDOWS
#undef CreateFileW
#undef _open_osfhandle
#undef _fdopen
#else
#undef mkstemp
#undef fdopen
#endif

static int handle_count(void)
{
#ifdef WINDOWS
    DWORD count;
    assert(GetProcessHandleCount(GetCurrentProcess(), &count));
    return (int)count;
#else
    int count = 0;
    DIR *directory = opendir("/proc/self/fd");
    assert(directory);
    while (readdir(directory)) count++;
    closedir(directory);
    return count;
#endif
}

static void success(void)
{
    char name[MAXFNAME+1];
    FILE *file = es_openScratchFile(name);
    assert(file && name[0]);
    assert(fwrite("scratch", 1, 7, file) == 7);
    assert(fclose(file) == 0 && remove(name) == 0);
}

int main(int argc, char **argv)
{
    int mode, cycle, before, modes;
    char name[MAXFNAME+1];
    assert(argc == 2 && strlen(argv[1]) < sizeof(TempDir));
    assert(syntax_checks_main() == 0);
    strcpy(TempDir, argv[1]);
    success(); before = handle_count();
    modes = 2;
#ifdef WINDOWS
    modes = 3;
#endif
    for (mode = 0; mode < modes; mode++)
    for (cycle = 0; cycle < 20; cycle++)
    {
        fail_create = mode == 0;
        fail_stream = mode == 1;
        fail_transfer = mode == 2;
        injected = 0;
        strcpy(name, "not-owned");
        assert(es_openScratchFile(name) == NULL && !name[0] && injected == 1);
        assert(handle_count() <= before);
        fail_create = fail_stream = fail_transfer = 0;
        success();
        assert(handle_count() <= before);
    }
    printf("scratch: %d failure/retry cycles; no descriptor/handle growth\n", modes*20);
    return 0;
}
