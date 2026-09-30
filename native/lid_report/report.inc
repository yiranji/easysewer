/* Appended to lid.c. Detailed reports own their streams and pending flushes. */
#include <stdarg.h>

static TLidRptFile *LidDirtyReports = NULL;
static int LidReportFailed = 0, LidReportOwnError = 0;
static char LidReportError[MAXMSG];

static void lid_restoreReportError(void)
{
    if (LidReportOwnError) {
        ErrorCode = ERR_RPT_WRITE;
        sstrncpy(ErrorMsg, LidReportError, MAXMSG - 1);
    }
}

static void lid_failReport(TLidRptFile *r)
{
    if (!LidReportFailed) {
        LidReportOwnError = (ErrorCode == 0);
        snprintf(LidReportError, sizeof(LidReportError),
                 "ERROR 306: cannot write LID detailed report: %s",
                 r && r->name ? r->name : "(unknown)");
    }
    LidReportFailed = 1;
    if (r) r->failed = 1;
    lid_restoreReportError();
}

void lid_resetReportFiles(void)
{
    /* Only called after swmm_open's existing-owner guard. */
    LidDirtyReports = NULL;
    LidReportFailed = LidReportOwnError = 0;
    LidReportError[0] = '\0';
}

int lid_reportHasFailed(void) { return LidReportFailed; }

int lid_writeReport(TLidRptFile *r, const char *format, ...)
{
    int n;
    va_list args;
    if (!r || !r->file || r->failed || LidReportFailed) return -1;
    va_start(args, format);
    n = vfprintf(r->file, format, args);
    va_end(args);
    if (n < 0 || ferror(r->file)) { lid_failReport(r); return -1; }
    if (n && !r->dirty) {
        r->dirty = 1;
        r->dirtyNext = LidDirtyReports;
        LidDirtyReports = r;
    }
    return n;
}

int lid_formatReport(TLidRptFile *r, const char *format, ...)
{
    int n;
    va_list args;
    if (!r || r->failed || LidReportFailed) return -1;
    va_start(args, format);
    n = vsnprintf(r->results, sizeof(r->results), format, args);
    va_end(args);
    if (n < 0 || (size_t)n >= sizeof(r->results)) {
        r->results[0] = '\0';
        lid_failReport(r);
        return -1;
    }
    return n;
}

int lid_checkReportFiles(void)
{
    TLidRptFile *r;
    /* Work is proportional to changed reports, not all deployed LID units. */
    while ((r = LidDirtyReports) != NULL) {
        LidDirtyReports = r->dirtyNext;
        r->dirtyNext = NULL;
        r->dirty = 0;
        if (r->file && !r->failed &&
            (fflush(r->file) != 0 || ferror(r->file))) lid_failReport(r);
    }
    lid_restoreReportError();
    return ErrorCode;
}

static int lid_closeReportFile(TLidRptFile *r)
{
    FILE *file;
    if (!r || !r->file) return 0;
    lid_checkReportFiles();
    file = r->file;
    r->file = NULL;
    if (fclose(file) != 0) lid_failReport(r);
    lid_restoreReportError();
    return r->failed ? ERR_RPT_WRITE : 0;
}

int lid_closeReportFiles(void)
{
    int j, code = 0, next;
    TLidList *item;
    if (LidGroups) for (j = 0; j < GroupCount; j++) {
        if (!LidGroups[j]) continue;
        for (item = LidGroups[j]->lidList; item; item = item->nextLidUnit) {
            next = lid_closeReportFile(item->lidUnit->rptFile);
            if (!code) code = next;
        }
    }
    return code;
}
