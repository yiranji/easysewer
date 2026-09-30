/* Checked solver OUT access. Inserted after output.c's private declarations. */
static int OutReady = 0, OutFailed = 0, OutEnded = 0;

static int outFail(int code)
{
    if (code == ERR_OUT_WRITE || code == ERR_OUT_SIZE) OutFailed = 1;
    if (!ErrorCode) report_writeErrorMsg(code, "");
    return 0;
}

static int outSeek(F_OFF position, int code)
{
    if (!Fout.file || position < 0 || F_SEEK(Fout.file, position, SEEK_SET) != 0)
        return outFail(code);
    return 1;
}

static F_OFF outTell(void)
{
    F_OFF position = F_TELL(Fout.file);
    if (position < 0) { outFail(ERR_OUT_WRITE); return -1; }
    /* These three metadata positions are signed INT4 in the fixed OUT footer. */
    if (position > INT32_MAX) { outFail(ERR_OUT_SIZE); return -1; }
    return position;
}

static size_t outWrite(const void *data, size_t size, size_t count, FILE *file)
{
    if (OutFailed) return 0;
    if (!file || !data || !size || count > SIZE_MAX / size ||
        fwrite(data, size, count, file) != count || ferror(file)) {
        outFail(ERR_OUT_WRITE);
        return 0;
    }
    return count;
}

static size_t outFloats(const REAL4 *data, size_t size, size_t count, FILE *file)
{
    size_t i;
    if (OutFailed) return 0;
    if (!data || size != sizeof(REAL4)) return outFail(ERR_OUT_WRITE);
    for (i = 0; i < count; i++)
        if (!isfinite(data[i])) return outFail(ERR_OUT_WRITE);
    return outWrite(data, size, count, file);
}

static size_t outDate(const REAL8 *data, size_t size, size_t count, FILE *file)
{
    if (!data || size != sizeof(REAL8) || count != 1 ||
        !isfinite(*data) || *data < -693593.0 || *data >= 2958466.0)
        return outFail(ERR_OUT_WRITE);
    return outWrite(data, size, count, file);
}

static int outPosition(int64_t period, F_OFF *position)
{
    if (period < 0 || BytesPerPeriod < 8 || OutputStartPos < 0 ||
        period > (INT64_MAX - OutputStartPos - 24) / BytesPerPeriod)
        return outFail(ERR_OUT_SIZE);
    *position = OutputStartPos + period * BytesPerPeriod;
    return 1;
}

static int outRead(void *data, size_t size, size_t count, F_OFF position)
{
    size_t i;
    if (!data || !size || count > SIZE_MAX / size) return outFail(ERR_OUT_READ);
    memset(data, 0, size * count);
    if (ErrorCode || !OutReady || !outSeek(position, ERR_OUT_READ)) return 0;
    if (fread(data, size, count, Fout.file) != count || ferror(Fout.file)) {
        memset(data, 0, size * count);
        return outFail(ERR_OUT_READ);
    }
    if (size == sizeof(REAL4)) {
        for (i = 0; i < count; i++) {
            if (!isfinite(((REAL4 *)data)[i])) {
                memset(data, 0, size * count);
                return outFail(ERR_OUT_READ);
            }
        }
    }
    return 1;
}

int output_closeFile(void)
{
    int code = 0;
    FILE *file = Fout.file;
    if (!file) return 0;
    Fout.file = NULL;
    if (fclose(file) != 0) code = ERR_OUT_WRITE;
    if (Fout.mode == SCRATCH_FILE && remove(Fout.name) != 0 && errno != ENOENT)
        code = ERR_OUT_WRITE;
    if (code) outFail(code);
    return code;
}

static int outReadPosition(long period, F_OFF offset, F_OFF *position)
{
    if (!OutReady || !Fout.file || period < 1 || period > Nperiods ||
        offset < 0 || offset >= BytesPerPeriod) return outFail(ERR_OUT_READ);
    /* Dimensions and period extents were checked before writing. */
    *position = OutputStartPos + ((F_OFF)period - 1) * BytesPerPeriod + offset;
    return 1;
}

void output_readDateTime(long period, DateTime *days)
{
    F_OFF position;
    if (!days) { outFail(ERR_OUT_READ); return; }
    *days = NO_DATE;
    if (!outReadPosition(period, 0, &position)) return;
    if (!outRead(days, sizeof(REAL8), 1, position) ||
        !isfinite(*days) || *days < -693593.0 || *days >= 2958466.0) {
        *days = NO_DATE;
        outFail(ERR_OUT_READ);
    }
}

void output_readSubcatchResults(long period, int index)
{
    F_OFF position;
    if (SubcatchResults) memset(SubcatchResults, 0, (size_t)NumSubcatchVars * sizeof(REAL4));
    if (index < 0 || index >= NumSubcatch) { outFail(ERR_OUT_READ); return; }
    if (!outReadPosition(period, sizeof(REAL8) + (F_OFF)index * NumSubcatchVars * sizeof(REAL4), &position)) return;
    outRead(SubcatchResults, sizeof(REAL4), NumSubcatchVars, position);
}

void output_readNodeResults(long period, int index)
{
    F_OFF position, offset;
    if (NodeResults) memset(NodeResults, 0, (size_t)NumNodeVars * sizeof(REAL4));
    if (index < 0 || index >= NumNodes) { outFail(ERR_OUT_READ); return; }
    offset = (F_OFF)NumSubcatch * NumSubcatchVars + (F_OFF)index * NumNodeVars;
    if (!outReadPosition(period, sizeof(REAL8) + offset * sizeof(REAL4), &position)) return;
    outRead(NodeResults, sizeof(REAL4), NumNodeVars, position);
}

void output_readLinkResults(long period, int index)
{
    F_OFF position, offset;
    if (LinkResults) memset(LinkResults, 0, (size_t)NumLinkVars * sizeof(REAL4));
    if (index < 0 || index >= NumLinks) { outFail(ERR_OUT_READ); return; }
    offset = (F_OFF)NumSubcatch * NumSubcatchVars + (F_OFF)NumNodes * NumNodeVars;
    if (!outReadPosition(period, sizeof(REAL8) + (offset + (F_OFF)index * NumLinkVars) * sizeof(REAL4), &position)) return;
    if (!outRead(LinkResults, sizeof(REAL4), NumLinkVars, position)) return;
    /* System values follow ALL links, not the particular selected link. */
    offset += (F_OFF)NumLinks * NumLinkVars;
    if (!outReadPosition(period, sizeof(REAL8) + offset * sizeof(REAL4), &position) ||
        !outRead(SysResults, sizeof(REAL4), MAX_SYS_RESULTS, position))
        memset(LinkResults, 0, (size_t)NumLinkVars * sizeof(REAL4));
}
