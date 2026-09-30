/* Paths follow the host filesystem syntax. On POSIX, ':' and '\\' are
 * ordinary filename bytes. On Windows, drive-relative paths retain the
 * drive's current-directory meaning; ordinary relative resources use InpDir. */
int isRelativePath(const char *fname)
{
    if (!fname || !fname[0]) return 1;
#ifdef WINDOWS
    if (fname[0] == '\\' || fname[0] == '/') return 0;
    if (((fname[0] >= 'A' && fname[0] <= 'Z') ||
         (fname[0] >= 'a' && fname[0] <= 'z')) && fname[1] == ':') return 0;
#else
    if (fname[0] == '/') return 0;
#endif
    return 1;
}

static void reportAbsolutePathError(void)
{
    report_writeErrorMsg(ERR_INP_FILE, "");
    sstrncpy(ErrorMsg, "\n  ERROR 303: input path cannot be resolved or exceeds native path buffer.", MAXMSG);
    report_writeLine(ErrorMsg);
}

void getAbsolutePath(const char *fname, char *absPath, size_t size)
{
    char *endOfDir;
    if (!absPath || !size) { reportAbsolutePathError(); return; }
    absPath[0] = 0;
    if (!fname || !fname[0]) { reportAbsolutePathError(); return; }
#ifdef WINDOWS
    /* Use the wide resolver: GetFullPathNameA can fail above MAX_PATH even
     * when this process's CRT can open the same long name. The conversion
     * uses the ANSI code page, matching the existing narrow native API. */
    {
        WCHAR wide[MAXFNAME+1], full[MAXFNAME+1];
        DWORD count, flags;
        BOOL usedDefault = FALSE;
        BOOL *used = GetACP() == CP_UTF8 ? NULL : &usedDefault;
        if (!MultiByteToWideChar(CP_ACP, MB_ERR_INVALID_CHARS, fname, -1,
                                 wide, MAXFNAME+1))
        { reportAbsolutePathError(); return; }
        count = GetFullPathNameW(wide, MAXFNAME+1, full, NULL);
        flags = GetACP() == CP_UTF8 ? WC_ERR_INVALID_CHARS : WC_NO_BEST_FIT_CHARS;
        if (!count || count > MAXFNAME ||
            !WideCharToMultiByte(CP_ACP, flags, full, -1, absPath, (int)size,
                                 NULL, used) || usedDefault)
        {
            absPath[0] = 0;
            reportAbsolutePathError();
            return;
        }
    }
    endOfDir = strrchr(absPath, '\\');
#else
    if (isRelativePath(fname))
    {
        char *resolved = realpath(fname, NULL);
        if (!resolved || strlen(resolved) >= size)
        {
            free(resolved);
            reportAbsolutePathError();
            return;
        }
        strcpy(absPath, resolved);
        free(resolved);
    }
    else
    {
        if (strlen(fname) >= size) { reportAbsolutePathError(); return; }
        strcpy(absPath, fname);
    }
    endOfDir = strrchr(absPath, '/');
#endif
    if (!endOfDir)
    {
        absPath[0] = 0;
        reportAbsolutePathError();
        return;
    }
    endOfDir[1] = 0;
}

int es_makeFileName(char *dest, const char *source, int absolute)
{
    char result[MAXFNAME+1];
    size_t prefix = 0, length;
    if (!source) { dest[0] = 0; return 0; }
    length = strlen(source);
    if (absolute && length && isRelativePath(source)) prefix = strlen(InpDir);
    if (length > MAXFNAME || prefix > MAXFNAME-length)
    { dest[0] = 0; return 0; }
    if (prefix) memcpy(result, InpDir, prefix);
    memcpy(result+prefix, source, length+1);
    memcpy(dest, result, prefix+length+1);
    return 1;
}

#include "mempool.h"

/* Cursors borrow immutable input names. The project pool is released only
 * after those cursors, streams and model objects have been destroyed. */
int es_storeInputPath(char **dest, const char *source)
{
    char name[MAXFNAME+1], *copy;
    size_t length;
    if (!es_makeFileName(name, source, 1)) return ERR_FILE_PATH;
    length = strlen(name);
    copy = Alloc((long)length+1);
    if (!copy) return ERR_MEMORY;
    memcpy(copy, name, length+1);
    *dest = copy;
    return 0;
}
