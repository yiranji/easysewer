/* Complete, transactional RDII frames for both pinned native families.
 * Inserted after rdii.c's declarations; no change to RTK convolution math.
 */
static int RdiiEof, RdiiWriting;
static DateTime RdiiPreviousDate;
static REAL4* RdiiPendingFlow;

static int rdiiFail(int code)
{
    if (!ErrorCode) report_writeErrorMsg(code, Frdii.name);
    return FALSE;
}

static int rdiiDateValid(DateTime date)
{
    return isfinite(date) && date >= NO_DATE + 1.0 && date < 2958466.0;
}

static int rdiiRead(void* data, size_t size, size_t count)
{
    if (fread(data, size, count, Frdii.file) != count)
        return rdiiFail(ferror(Frdii.file) ? ERR_RDII_FILE_READ : ERR_RDII_FILE_FORMAT);
    return TRUE;
}

static int rdiiWrite(const void* data, size_t size, size_t count)
{
    if (ErrorCode) return FALSE;
    if (fwrite(data, size, count, Frdii.file) != count)
        return rdiiFail(ERR_RDII_FILE_WRITE);
    return TRUE;
}

static void rdiiCloseFile(void)
{
    if (Frdii.file)
    {
        if (fclose(Frdii.file) != 0)
            rdiiFail(RdiiWriting ? ERR_RDII_FILE_WRITE : ERR_RDII_FILE_READ);
        Frdii.file = NULL;
    }
    RdiiWriting = FALSE;
}

/* 1: complete line, 0: clean EOF, -1: failed/overlong line. */
static int rdiiLine(char* line)
{
    int ch, size = 0;
    while ((ch = fgetc(Frdii.file)) != EOF)
    {
        if (ch == 0 || size == MAXLINE)
        { rdiiFail(ERR_RDII_FILE_FORMAT); return -1; }
        line[size++] = (char)ch;
        if (ch == '\n') break;
    }
    line[size] = '\0';
    if (ferror(Frdii.file)) { rdiiFail(ERR_RDII_FILE_READ); return -1; }
    return size ? 1 : 0;
}

static int rdiiTokens(char* line, char** tokens, int capacity)
{
    int count = 0;
    char* p = line;
    while (*p)
    {
        while (*p && isspace((unsigned char)*p)) p++;
        if (!*p) break;
        if (count == capacity) return capacity + 1;
        tokens[count++] = p;
        while (*p && !isspace((unsigned char)*p)) p++;
        if (*p) *p++ = '\0';
    }
    return count;
}

static int rdiiInteger(const char* token, int* value)
{
    char* end;
    long x;
    errno = 0; x = strtol(token, &end, 10);
    if (end == token || *end || errno || x < INT_MIN || x > INT_MAX) return FALSE;
    *value = (int)x;
    return TRUE;
}

static int rdiiAllocate(void)
{
    if (NumRdiiNodes <= 0 || NumRdiiNodes > Nobjects[NODE])
        return rdiiFail(ERR_RDII_FILE_FORMAT);
    if ((size_t)NumRdiiNodes > SIZE_MAX / sizeof(REAL4) ||
        (size_t)NumRdiiNodes > SIZE_MAX / sizeof(int)) return rdiiFail(ERR_MEMORY);
    RdiiNodeIndex = calloc((size_t)NumRdiiNodes, sizeof(int));
    RdiiNodeFlow = calloc((size_t)NumRdiiNodes, sizeof(REAL4));
    RdiiPendingFlow = calloc((size_t)NumRdiiNodes, sizeof(REAL4));
    if (!RdiiNodeIndex || !RdiiNodeFlow || !RdiiPendingFlow) return rdiiFail(ERR_MEMORY);
    return TRUE;
}

static int rdiiCheckIndices(int binary)
{
    int i, j;
    unsigned char* seen = calloc((size_t)Nobjects[NODE], 1);
    if (!seen) return rdiiFail(ERR_MEMORY);
    for (i = 0; i < NumRdiiNodes; i++)
    {
        j = RdiiNodeIndex[i];
        if (j < 0 || j >= Nobjects[NODE] || seen[j] ||
            (binary && !Node[j].rdiiInflow))
        { free(seen); return rdiiFail(ERR_RDII_FILE_FORMAT); }
        seen[j] = TRUE;
    }
    free(seen);
    return TRUE;
}

int readRdiiFileHeader(void)
{
    if (!rdiiRead(&RdiiStep, sizeof(INT4), 1) || RdiiStep <= 0 ||
        !rdiiRead(&NumRdiiNodes, sizeof(INT4), 1)) goto invalid;
    if (!rdiiAllocate() || !rdiiRead(RdiiNodeIndex, sizeof(INT4), NumRdiiNodes) ||
        !rdiiCheckIndices(TRUE)) goto invalid;
    return 0;
invalid:
    rdiiFail(ERR_RDII_FILE_FORMAT);
    return ErrorCode;
}

int readRdiiTextFileHeader(void)
{
    int i, n, constituents;
    char line[MAXLINE + 1];
    char* tokens[MAXLINE + 1];
    if (rdiiLine(line) != 1 || rdiiTokens(line, tokens, MAXLINE) < 1 ||
        strcmp(tokens[0], "SWMM5")) goto invalid;
    if (rdiiLine(line) != 1) goto invalid; /* title */
    if (rdiiLine(line) != 1 || rdiiTokens(line, tokens, MAXLINE) < 1 ||
        !rdiiInteger(tokens[0], &RdiiStep) || RdiiStep <= 0) goto invalid;
    if (rdiiLine(line) != 1 || rdiiTokens(line, tokens, MAXLINE) < 1 ||
        !rdiiInteger(tokens[0], &constituents) || constituents != 1) goto invalid;
    if (rdiiLine(line) != 1 || rdiiTokens(line, tokens, MAXLINE) < 2 ||
        !strcomp(tokens[0], "FLOW")) goto invalid;
    RdiiFlowUnits = findmatch(tokens[1], FlowUnitWords);
    if (RdiiFlowUnits < 0) goto invalid;
    if (rdiiLine(line) != 1 || rdiiTokens(line, tokens, MAXLINE) < 1 ||
        !rdiiInteger(tokens[0], &NumRdiiNodes) || !rdiiAllocate()) goto invalid;
    for (i = 0; i < NumRdiiNodes; i++)
    {
        if (rdiiLine(line) != 1) goto invalid;
        n = rdiiTokens(line, tokens, MAXLINE);
        if (n < 1) goto invalid;
        RdiiNodeIndex[i] = project_findObject(NODE, tokens[0]);
    }
    if (!rdiiCheckIndices(FALSE) || rdiiLine(line) != 1) goto invalid;
    return 0;
invalid:
    rdiiFail(ERR_RDII_FILE_FORMAT);
    return ErrorCode;
}

static int rdiiPublishFrame(DateTime date)
{
    int i;
    DateTime end;
    if (!rdiiDateValid(date) || (RdiiPreviousDate != NO_DATE &&
        /* Native calendar days lose sub-microsecond precision when adding
         * adjacent integer-second steps. Match the portable RDII reader's
         * 1e-9 day tolerance; this is far below one RDII time step. */
        date < RdiiPreviousDate + (double)RdiiStep / SECperDAY - 1e-9))
        return rdiiFail(ERR_RDII_FILE_FORMAT);
    end = datetime_addSeconds(date, RdiiStep);
    if (!rdiiDateValid(end) || end <= date) return rdiiFail(ERR_RDII_FILE_FORMAT);
    for (i = 0; i < NumRdiiNodes; i++)
        if (!isfinite(RdiiPendingFlow[i])) return rdiiFail(ERR_RDII_FILE_FORMAT);
    memcpy(RdiiNodeFlow, RdiiPendingFlow, sizeof(REAL4) * (size_t)NumRdiiNodes);
    RdiiPreviousDate = RdiiStartDate = date;
    RdiiEndDate = end;
    return TRUE;
}

void readRdiiTextFlows(void)
{
    int i, j, status, values[6];
    double flow;
    DateTime date, first = NO_DATE;
    char line[MAXLINE + 1];
    char* tokens[9];
    char* end;
    for (i = 0; i < NumRdiiNodes; i++)
    {
        status = rdiiLine(line);
        if (status == 0 && i == 0) { RdiiEof = TRUE; return; }
        if (status != 1 || rdiiTokens(line, tokens, 9) != 8) goto invalid;
        if (!strcomp(tokens[0], Node[RdiiNodeIndex[i]].ID)) goto invalid;
        for (j = 0; j < 6; j++) if (!rdiiInteger(tokens[j + 1], &values[j])) goto invalid;
        if (values[0] < 1 || values[0] > 9999 || values[1] < 1 || values[1] > 12 ||
            values[2] < 1 || values[2] > 31 || values[3] < 0 || values[3] > 23 ||
            values[4] < 0 || values[4] > 59 || values[5] < 0 || values[5] > 59) goto invalid;
        date = datetime_encodeDate(values[0], values[1], values[2]);
        if (date == NO_DATE) goto invalid;
        date += datetime_encodeTime(values[3], values[4], values[5]);
        if (i == 0) first = date;
        else if (date != first) goto invalid;
        flow = strtod(tokens[7], &end);
        if (end == tokens[7] || *end || !isfinite(flow)) goto invalid;
        flow /= Qcf[RdiiFlowUnits];
        if (!isfinite(flow) || fabs(flow) > FLT_MAX) goto invalid;
        RdiiPendingFlow[i] = (REAL4)flow;
    }
    rdiiPublishFrame(first);
    return;
invalid:
    rdiiFail(ERR_RDII_FILE_FORMAT);
}

void readRdiiFlows(void)
{
    DateTime date;
    size_t n;
    RdiiStartDate = RdiiEndDate = NO_DATE;
    if (ErrorCode || RdiiEof || !Frdii.file) return;
    if (RdiiFileType == TEXT) { readRdiiTextFlows(); return; }
    n = fread(&date, 1, sizeof(date), Frdii.file);
    if (n == 0 && !ferror(Frdii.file)) { RdiiEof = TRUE; return; }
    if (n != sizeof(date))
    { rdiiFail(ferror(Frdii.file) ? ERR_RDII_FILE_READ : ERR_RDII_FILE_FORMAT); return; }
    if (!rdiiRead(RdiiPendingFlow, sizeof(REAL4), NumRdiiNodes)) return;
    rdiiPublishFrame(date);
}

void rdii_openRdii(void)
{
    char stamp[sizeof(FILE_STAMP) - 1];
    size_t n;
    fpos_t first;
    RdiiNodeIndex = NULL; RdiiNodeFlow = RdiiPendingFlow = NULL; UHGroup = NULL;
    NumRdiiNodes = 0;
    RdiiEof = RdiiWriting = FALSE;
    RdiiPreviousDate = RdiiStartDate = RdiiEndDate = NO_DATE;
    if (IgnoreRDII) return;
    if (Frdii.mode != USE_FILE) createRdiiFile();
    if (Frdii.mode == NO_FILE || ErrorCode) return;
    Frdii.file = fopen(Frdii.name, "rb");
    if (!Frdii.file)
    { rdiiFail(Frdii.mode == SCRATCH_FILE ? ERR_RDII_FILE_SCRATCH : ERR_RDII_FILE_OPEN); return; }
    n = fread(stamp, 1, sizeof(stamp), Frdii.file);
    if (ferror(Frdii.file)) { rdiiFail(ERR_RDII_FILE_READ); goto failed; }
    if (n == sizeof(stamp) && !memcmp(stamp, FILE_STAMP, sizeof(stamp)))
    { RdiiFileType = BINARY; readRdiiFileHeader(); }
    else
    {
        if (fseek(Frdii.file, 0, SEEK_SET)) { rdiiFail(ERR_RDII_FILE_READ); goto failed; }
        RdiiFileType = TEXT; readRdiiTextFileHeader();
    }
    if (ErrorCode) goto failed;
    if (fgetpos(Frdii.file, &first)) { rdiiFail(ERR_RDII_FILE_READ); goto failed; }
    /* Validate all frames, including data beyond the requested run window. */
    while (!RdiiEof && !ErrorCode) readRdiiFlows();
    if (ErrorCode) goto failed;
    if (fsetpos(Frdii.file, &first)) { rdiiFail(ERR_RDII_FILE_READ); goto failed; }
    RdiiEof = FALSE; RdiiPreviousDate = NO_DATE;
    readRdiiFlows();
    if (!ErrorCode) return;
failed:
    rdiiCloseFile();
    freeRdiiMemory();
}

void rdii_closeRdii(void)
{
    rdiiCloseFile();
    /* A caller's SAVE path can be a device or have changed ownership. Only
     * remove a scratch name reserved by this engine; Runner owns publication. */
    if (Frdii.mode == SCRATCH_FILE) remove(Frdii.name);
    freeRdiiMemory();
    NumRdiiNodes = 0;
}

int rdii_getNumRdiiFlows(DateTime date)
{
    if (ErrorCode || NumRdiiNodes == 0 || !Frdii.file) return 0;
    while (!RdiiEof && !ErrorCode)
    {
        if (RdiiStartDate == NO_DATE || date < RdiiStartDate) return 0;
        if (date < RdiiEndDate) return NumRdiiNodes;
        readRdiiFlows();
    }
    return 0;
}

void rdii_getRdiiFlow(int i, int* j, double* q)
{
    if (ErrorCode || i < 0 || i >= NumRdiiNodes) return;
    *j = RdiiNodeIndex[i]; *q = RdiiNodeFlow[i];
}

static int rdiiParamsValid(double x[])
{
    int i;
    double base;
    for (i = 0; i < 6; i++) if (!isfinite(x[i]) || x[i] < 0.0) return FALSE;
    base = x[1] * (1.0 + x[2]) * 3600.0;
    return isfinite(base) && base <= INT_MAX && x[1] * 3600.0 <= INT_MAX;
}
