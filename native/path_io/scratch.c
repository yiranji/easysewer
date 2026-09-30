/* Keep the exclusively created file open until its final owner closes it.
 * An explicit TEMPDIR is never replaced by an environment or CWD fallback. */
#include <errno.h>
#include <stdint.h>
#include <sys/stat.h>
#ifdef WINDOWS
#include <io.h>
#include <fcntl.h>
#endif

FILE *es_openScratchFile(char *fname)
{
    char directory[MAXFNAME+1], name[MAXFNAME+1];
    size_t length;
    int separator, fd;
    FILE *file;
    fname[0] = 0;
    if (!es_makeFileName(directory, TempDir[0] ? TempDir : ".", 0)) return NULL;
    length = strlen(directory);
    separator = length && directory[length-1] != '/';
#ifdef WINDOWS
    {
        WCHAR wide[MAXFNAME+1];
        LARGE_INTEGER tick;
        HANDLE handle;
        DWORD error;
        int attempt, count;
        if (length && directory[length-1] == '\\') separator = 0;
        if (!MultiByteToWideChar(CP_ACP, MB_ERR_INVALID_CHARS, directory, -1,
                                 wide, MAXFNAME+1)) return NULL;
        if (_wmkdir(wide) != 0 && errno != EEXIST) return NULL;
        for (attempt = 0; attempt < 128; attempt++)
        {
            if (!QueryPerformanceCounter(&tick)) return NULL;
            count = snprintf(name, sizeof(name), "%s%sswmm-%08lx-%016llx-%02x.tmp",
                             directory, separator ? "\\" : "",
                             (unsigned long)GetCurrentProcessId(),
                             (unsigned long long)tick.QuadPart, attempt);
            if (count < 0 || count > MAXFNAME) return NULL;
            if (!MultiByteToWideChar(CP_ACP, MB_ERR_INVALID_CHARS, name, -1,
                                     wide, MAXFNAME+1)) return NULL;
            handle = CreateFileW(wide, GENERIC_READ | GENERIC_WRITE,
                                 FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                                 NULL, CREATE_NEW, FILE_ATTRIBUTE_TEMPORARY, NULL);
            if (handle == INVALID_HANDLE_VALUE)
            {
                error = GetLastError();
                if (error == ERROR_FILE_EXISTS || error == ERROR_ALREADY_EXISTS) continue;
                return NULL;
            }
            fd = _open_osfhandle((intptr_t)handle, _O_BINARY | _O_RDWR);
            if (fd < 0)
            {
                CloseHandle(handle);
                DeleteFileW(wide);
                return NULL;
            }
            file = _fdopen(fd, "w+b");
            if (!file)
            {
                _close(fd);
                DeleteFileW(wide);
                return NULL;
            }
            strcpy(fname, name);
            return file;
        }
        return NULL;
    }
#else
    if (mkdir(directory, 0700) != 0 && errno != EEXIST) return NULL;
    if (length + (size_t)separator + 10 > MAXFNAME) return NULL;
    memcpy(name, directory, length);
    if (separator) name[length++] = '/';
    memcpy(name+length, "swmmXXXXXX", 11);
    fd = mkstemp(name);
    if (fd < 0) return NULL;
    file = fdopen(fd, "w+b");
    if (!file)
    {
        int saved = errno;
        close(fd);
        remove(name);
        errno = saved;
        return NULL;
    }
    strcpy(fname, name);
    return file;
#endif
}
