/* Shared RPT stream ownership and buffered-write failure state. */
static int RptFailed = 0;
static int RptDirty = 0;
static int RptOwnError = 0;

static void rptRestoreError(void)
{
    if (RptOwnError) {
        ErrorCode = ERR_RPT_WRITE;
        error_getMsg(ERR_RPT_WRITE, ErrorMsg);
    }
}

static void rptFail(void)
{
    /* Reporting a failure must not write recursively to the damaged stream.
     * A native error that preceded this I/O failure remains the primary one. */
    if (!RptFailed) RptOwnError = (ErrorCode == 0);
    RptFailed = 1;
    rptRestoreError();
}

void report_resetFile(void)
{
    RptFailed = RptDirty = RptOwnError = 0;
}

int report_writeFormat(const char *format, ...)
{
    int result;
    va_list args;
    if (!Frpt.file) return 0;
    if (RptFailed) return -1;
    va_start(args, format);
    result = vfprintf(Frpt.file, format, args);
    va_end(args);
    if (result < 0 || ferror(Frpt.file)) {
        rptFail();
        return -1;
    }
    if (result) RptDirty = 1;
    return result;
}

int report_checkFile(void)
{
    if (Frpt.file && !RptFailed && RptDirty) {
        if (fflush(Frpt.file) != 0 || ferror(Frpt.file)) rptFail();
        else RptDirty = 0;
    }
    rptRestoreError();
    return ErrorCode;
}

int report_closeFile(void)
{
    FILE *file = Frpt.file;
    if (!file) return 0;
    report_checkFile();
    Frpt.file = NULL;
    RptDirty = 0;
    if (fclose(file) != 0) rptFail();
    rptRestoreError();
    return RptFailed ? ERR_RPT_WRITE : 0;
}
