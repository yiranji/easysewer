/* Complete routing-interface frames and checked output for the pinned engines. */
static char **IfaceNodeNames, **IfacePollutNames;
static double **IfacePendingValues;
static DateTime IfacePreviousDate;
static uint64_t IfaceFileBytes;
static int IfaceEof, IfaceOutputLine;

static int ifaceFail(int code, const char* path)
{
    if (!ErrorCode) report_writeErrorMsg(code, (char*)path);
    return FALSE;
}

static int ifaceInvalid(void)
{
    return ifaceFail(ERR_ROUTING_FILE_FORMAT, Finflows.name);
}

/* A physical line has the same bound as the portable interface reader. */
static int ifaceLine(char* line)
{
    int ch, n = 0;
    while ((ch = fgetc(Finflows.file)) != EOF)
    {
        if (!ch || n == MAXLINE) { ifaceInvalid(); return -1; }
        if (ch == '\n') break;
        line[n++] = (char)ch;
    }
    if (ferror(Finflows.file))
    { ifaceFail(ERR_ROUTING_FILE_READ, Finflows.name); return -1; }
    if (n && line[n-1] == '\r') n--;
    line[n] = '\0';
    if (n >= MAXLINE-1 || strchr(line, '\r')) { ifaceInvalid(); return -1; }
    return n || ch != EOF ? 1 : 0;
}

static int ifaceTokens(char* line, char** tokens, int capacity)
{
    int n = 0;
    char* p = line;
    while (*p)
    {
        while (*p && isspace((unsigned char)*p)) p++;
        if (!*p) break;
        if (n == capacity) return capacity + 1;
        tokens[n++] = p;
        while (*p && !isspace((unsigned char)*p)) p++;
        if (*p) *p++ = '\0';
    }
    return n;
}

static int ifaceInteger(const char* token, int* value)
{
    char* end;
    long n;
    errno = 0; n = strtol(token, &end, 10);
    if (end == token || *end || errno || n < INT_MIN || n > INT_MAX) return FALSE;
    *value = (int)n;
    return TRUE;
}

static int ifaceReadCount(int* value)
{
    char line[MAXLINE+1];
    char* tokens[2];
    if (ifaceLine(line) != 1 || ifaceTokens(line, tokens, 2) < 1 ||
        !ifaceInteger(tokens[0], value) || *value <= 0) return ifaceInvalid();
    return TRUE;
}

static char* ifaceName(const char* text)
{
    size_t n = strlen(text) + 1;
    char* copy = malloc(n);
    if (!copy) ifaceFail(ERR_MEMORY, "");
    else memcpy(copy, text, n);
    return copy;
}

static int ifaceNameCompare(const void* left, const void* right)
{
    const unsigned char* a = (const unsigned char*)*(const char* const*)left;
    const unsigned char* b = (const unsigned char*)*(const char* const*)right;
    int x, y;
    do
    {
        x = *a++; y = *b++;
        if (x >= 'a' && x <= 'z') x -= 'a'-'A';
        if (y >= 'a' && y <= 'z') y -= 'a'-'A';
        if (x != y) return x-y;
    } while (x);
    return 0;
}

static int ifaceUnique(char** names, int count)
{
    int i;
    char** sorted;
    if ((size_t)count > SIZE_MAX / sizeof(char*)) return ifaceFail(ERR_MEMORY, "");
    sorted = malloc((size_t)count * sizeof(char*));
    if (!sorted) return ifaceFail(ERR_MEMORY, "");
    memcpy(sorted, names, (size_t)count * sizeof(char*));
    qsort(sorted, count, sizeof(char*), ifaceNameCompare);
    for (i = 1; i < count; i++)
        if (!ifaceNameCompare(&sorted[i-1], &sorted[i]))
        { free(sorted); return ifaceInvalid(); }
    free(sorted);
    return TRUE;
}

static int ifaceSize(void)
{
#ifdef _WIN32
    struct _stat64 status;
    if (_fstat64(_fileno(Finflows.file), &status))
#else
    struct stat status;
    if (fstat(fileno(Finflows.file), &status))
#endif
        return ifaceFail(ERR_ROUTING_FILE_READ, Finflows.name);
    if (status.st_size < 0) return ifaceFail(ERR_ROUTING_FILE_READ, Finflows.name);
    IfaceFileBytes = (uint64_t)status.st_size;
    return TRUE;
}

static int ifaceAliases(void)
{
    if (!Finflows.file || Foutflows.mode != SAVE_FILE) return FALSE;
#ifdef _WIN32
    BY_HANDLE_FILE_INFORMATION input, output;
    HANDLE h = CreateFileA(Foutflows.name, 0,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        NULL, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
    int same = FALSE;
    if (h == INVALID_HANDLE_VALUE) return FALSE;
    if (GetFileInformationByHandle((HANDLE)_get_osfhandle(_fileno(Finflows.file)), &input) &&
        GetFileInformationByHandle(h, &output))
        same = input.dwVolumeSerialNumber == output.dwVolumeSerialNumber &&
            input.nFileIndexHigh == output.nFileIndexHigh &&
            input.nFileIndexLow == output.nFileIndexLow;
    CloseHandle(h);
    return same;
#else
    struct stat input, output;
    return fstat(fileno(Finflows.file), &input) == 0 &&
        stat(Foutflows.name, &output) == 0 &&
        input.st_dev == output.st_dev && input.st_ino == output.st_ino;
#endif
}

static void ifaceClose(FILE** file, int code, const char* path)
{
    if (*file && fclose(*file) != 0) ifaceFail(code, path);
    *file = NULL;
}

void iface_closeRoutingFiles(void)
{
    int i;
    FREE(IfacePolluts); FREE(IfaceNodes);
    if (IfaceNodeNames)
        for (i = 0; i < NumIfaceNodes; i++) free(IfaceNodeNames[i]);
    if (IfacePollutNames)
        for (i = 0; i <= NumIfacePolluts; i++) free(IfacePollutNames[i]);
    FREE(IfaceNodeNames); FREE(IfacePollutNames);
    project_freeMatrix(OldIfaceValues); OldIfaceValues = NULL;
    project_freeMatrix(NewIfaceValues); NewIfaceValues = NULL;
    project_freeMatrix(IfacePendingValues); IfacePendingValues = NULL;
    ifaceClose(&Finflows.file, ERR_ROUTING_FILE_READ, Finflows.name);
    ifaceClose(&Foutflows.file, ERR_ROUTING_FILE_WRITE, Foutflows.name);
    NumIfaceNodes = NumIfacePolluts = 0;
    OldIfaceDate = NewIfaceDate = IfacePreviousDate = NO_DATE;
    IfaceEof = TRUE;
}

void iface_openRoutingFiles(void)
{
    NumIfacePolluts = NumIfaceNodes = 0;
    IfacePolluts = IfaceNodes = NULL;
    IfaceNodeNames = IfacePollutNames = NULL;
    OldIfaceValues = NewIfaceValues = IfacePendingValues = NULL;
    OldIfaceDate = NewIfaceDate = IfacePreviousDate = NO_DATE;
    IfaceEof = FALSE; IfaceFrac = 0.0; IfaceOutputLine = 0;
    if (Foutflows.mode != NO_FILE && Finflows.mode != NO_FILE &&
        strcomp(Foutflows.name, Finflows.name))
    { ifaceFail(ERR_ROUTING_FILE_NAMES, ""); return; }
    /* Input must be valid before opening a caller's output for truncation. */
    if (Finflows.mode == USE_FILE) openFileForInput();
    if (!ErrorCode && ifaceAliases()) ifaceFail(ERR_ROUTING_FILE_NAMES, "");
    if (!ErrorCode && Foutflows.mode == SAVE_FILE) openFileForOutput();
    if (ErrorCode) iface_closeRoutingFiles();
}

int getIfaceFilePolluts(void)
{
    int count, i, j, n;
    char line[MAXLINE+1];
    char* tokens[3];
    if (!ifaceReadCount(&count)) return ErrorCode;
    /* Each data row must fit the native/portable physical line limit. */
    if (count > MAXLINE-8 || (uint64_t)count > IfaceFileBytes/2)
    { ifaceInvalid(); return ErrorCode; }
    NumIfacePolluts = count - 1;
    IfacePollutNames = calloc((size_t)count, sizeof(char*));
    if (!IfacePollutNames) { ifaceFail(ERR_MEMORY, ""); return ErrorCode; }
    if (Nobjects[POLLUT] > 0)
    {
        IfacePolluts = calloc((size_t)Nobjects[POLLUT], sizeof(int));
        if (!IfacePolluts) { ifaceFail(ERR_MEMORY, ""); return ErrorCode; }
        for (i = 0; i < Nobjects[POLLUT]; i++) IfacePolluts[i] = -1;
    }
    for (i = 0; i < count; i++)
    {
        if (ifaceLine(line) != 1 || (n = ifaceTokens(line, tokens, 3)) < 2)
        { ifaceInvalid(); return ErrorCode; }
        IfacePollutNames[i] = ifaceName(tokens[0]);
        if (ErrorCode) return ErrorCode;
        if (!i)
        {
            IfaceFlowUnits = findmatch(tokens[1], FlowUnitWords);
            if (!strcomp(tokens[0], "FLOW") || IfaceFlowUnits < 0)
            { ifaceInvalid(); return ErrorCode; }
        }
        else
        {
            if (!strcomp(tokens[1], "MG/L") && !strcomp(tokens[1], "UG/L") &&
                !strcomp(tokens[1], "#/L")) { ifaceInvalid(); return ErrorCode; }
            j = project_findObject(POLLUT, tokens[0]);
            if (j < 0) continue; /* All header rows are still consumed. */
            if (!strcomp(tokens[1], QualUnitsWords[Pollut[j].units]))
            { ifaceFail(ERR_ROUTING_FILE_NOMATCH, Finflows.name); return ErrorCode; }
            IfacePolluts[j] = i - 1;
        }
    }
    ifaceUnique(IfacePollutNames, count);
    return ErrorCode;
}

int getIfaceFileNodes(void)
{
    int i;
    char line[MAXLINE+1];
    char* tokens[2];
    if (!ifaceReadCount(&NumIfaceNodes)) return ErrorCode;
    if ((uint64_t)NumIfaceNodes > IfaceFileBytes/2 ||
        (size_t)NumIfaceNodes > SIZE_MAX/sizeof(char*))
    { ifaceInvalid(); return ErrorCode; }
    IfaceNodes = calloc((size_t)NumIfaceNodes, sizeof(int));
    IfaceNodeNames = calloc((size_t)NumIfaceNodes, sizeof(char*));
    if (!IfaceNodes || !IfaceNodeNames) { ifaceFail(ERR_MEMORY, ""); return ErrorCode; }
    for (i = 0; i < NumIfaceNodes; i++)
    {
        if (ifaceLine(line) != 1 || ifaceTokens(line, tokens, 2) < 1)
        { ifaceInvalid(); return ErrorCode; }
        IfaceNodeNames[i] = ifaceName(tokens[0]);
        if (ErrorCode) return ErrorCode;
        IfaceNodes[i] = project_findObject(NODE, tokens[0]);
    }
    if (!ifaceUnique(IfaceNodeNames, NumIfaceNodes)) return ErrorCode;
    if (ifaceLine(line) != 1) ifaceInvalid(); /* required heading, possibly blank */
    return ErrorCode;
}

void readNewIfaceValues(void)
{
    int i, j, p, consumed, status, count, stamp[6];
    double value;
    DateTime date, first = NO_DATE;
    char line[MAXLINE+1];
    char* tokens[MAXLINE+1];
    char* end;
    if (ErrorCode || IfaceEof) return;
    for (i = 0; i < NumIfaceNodes; i++)
    {
        status = ifaceLine(line);
        if (!i && status == 0)
        { IfaceEof = TRUE; NewIfaceDate = NO_DATE; return; }
        if (status != 1) { ifaceInvalid(); return; }
        count = ifaceTokens(line, tokens, MAXLINE);
        if (!i && !count)
        {
            /* Only trailing blank lines are permitted, matching the text reader. */
            while ((status = ifaceLine(line)) == 1)
                if (ifaceTokens(line, tokens, MAXLINE)) { ifaceInvalid(); return; }
            if (status < 0) return;
            IfaceEof = TRUE; NewIfaceDate = NO_DATE; return;
        }
        if (count != 8 + NumIfacePolluts || !strcomp(tokens[0], IfaceNodeNames[i]))
        { ifaceInvalid(); return; }
        for (j = 0; j < 6; j++)
            if (!ifaceInteger(tokens[j+1], &stamp[j])) { ifaceInvalid(); return; }
        if (stamp[0] < 1 || stamp[0] > 9999 || stamp[1] < 1 || stamp[1] > 12 ||
            stamp[2] < 1 || stamp[2] > 31 || stamp[3] < 0 || stamp[3] > 23 ||
            stamp[4] < 0 || stamp[4] > 59 || stamp[5] < 0 || stamp[5] > 59)
        { ifaceInvalid(); return; }
        date = datetime_encodeDate(stamp[0], stamp[1], stamp[2]);
        if (date == NO_DATE) { ifaceInvalid(); return; }
        date += datetime_encodeTime(stamp[3], stamp[4], stamp[5]);
        if (!i) first = date;
        else if (date != first) { ifaceInvalid(); return; }
        for (j = 0; j <= NumIfacePolluts; j++)
        {
            value = strtod(tokens[j+7], &end);
            if (end == tokens[j+7] || *end || !isfinite(value))
            { ifaceInvalid(); return; }
            if (!j) value /= Qcf[IfaceFlowUnits];
            if (!isfinite(value))
            { ifaceInvalid(); return; }
            if (IfaceNodes[i] >= 0 && (fabs(value) > FLT_MAX ||
                (!j && fabs(value * UCF(FLOW)) > FLT_MAX)))
            {
                consumed = !j;
                for (p = 0; j && p < Nobjects[POLLUT]; p++)
                    if (IfacePolluts[p] == j-1) { consumed = TRUE; break; }
                if (consumed) { ifaceInvalid(); return; }
            }
            IfacePendingValues[i][j] = value;
        }
    }
    if (IfacePreviousDate != NO_DATE && first <= IfacePreviousDate)
    { ifaceInvalid(); return; }
    for (i = 0; i < NumIfaceNodes; i++)
        memcpy(NewIfaceValues[i], IfacePendingValues[i],
            ((size_t)NumIfacePolluts+1)*sizeof(double));
    IfacePreviousDate = NewIfaceDate = first;
}

void setOldIfaceValues(void)
{
    int i;
    OldIfaceDate = NewIfaceDate;
    for (i = 0; i < NumIfaceNodes; i++)
        memcpy(OldIfaceValues[i], NewIfaceValues[i],
            ((size_t)NumIfacePolluts+1)*sizeof(double));
}

void openFileForInput(void)
{
    int count, hasFrame = FALSE;
    char line[MAXLINE+1];
    char* tokens[2];
    fpos_t first;
    Finflows.file = fopen(Finflows.name, "rb");
    if (!Finflows.file) { ifaceFail(ERR_ROUTING_FILE_OPEN, Finflows.name); return; }
    if (!ifaceSize()) return;
    if (ifaceLine(line) != 1 || ifaceTokens(line, tokens, 2) < 1 ||
        !strcomp(tokens[0], "SWMM5") || ifaceLine(line) != 1 || !ifaceReadCount(&IfaceStep))
    { ifaceInvalid(); return; }
    if (getIfaceFilePolluts() || getIfaceFileNodes()) return;
    count = NumIfacePolluts+1;
    OldIfaceValues = project_createMatrix(NumIfaceNodes, count);
    NewIfaceValues = project_createMatrix(NumIfaceNodes, count);
    IfacePendingValues = project_createMatrix(NumIfaceNodes, count);
    if (!OldIfaceValues || !NewIfaceValues || !IfacePendingValues)
    { ifaceFail(ERR_MEMORY, ""); return; }
    if (fgetpos(Finflows.file, &first))
    { ifaceFail(ERR_ROUTING_FILE_READ, Finflows.name); return; }
    while (!IfaceEof && !ErrorCode)
    {
        readNewIfaceValues();
        if (!IfaceEof && !ErrorCode) hasFrame = TRUE;
    }
    if (ErrorCode) return;
    if (!hasFrame) { ifaceInvalid(); return; }
    if (fsetpos(Finflows.file, &first))
    { ifaceFail(ERR_ROUTING_FILE_READ, Finflows.name); return; }
    IfaceEof = FALSE; IfacePreviousDate = NO_DATE;
    readNewIfaceValues();
    if (!ErrorCode) setOldIfaceValues(); /* first sample is actual data, not heap contents */
}

int iface_getNumIfaceNodes(DateTime currentDate)
{
    if (ErrorCode || !NumIfaceNodes || IfaceEof || OldIfaceDate > currentDate) return 0;
    while (NewIfaceDate < currentDate && !IfaceEof && !ErrorCode)
    { setOldIfaceValues(); readNewIfaceValues(); }
    if (ErrorCode || IfaceEof) return 0;
    IfaceFrac = NewIfaceDate == OldIfaceDate ? 0.0 :
        (currentDate - OldIfaceDate) / (NewIfaceDate - OldIfaceDate);
    IfaceFrac = MAX(0.0, IfaceFrac);
    IfaceFrac = MIN(IfaceFrac, 1.0);
    return NumIfaceNodes;
}

int iface_getIfaceNode(int index)
{
    return index >= 0 && index < NumIfaceNodes ? IfaceNodes[index] : -1;
}

double iface_getIfaceFlow(int index)
{
    if (ErrorCode || index < 0 || index >= NumIfaceNodes) return 0.0;
    return (1.0-IfaceFrac)*OldIfaceValues[index][0] + IfaceFrac*NewIfaceValues[index][0];
}

double iface_getIfaceQual(int index, int pollut)
{
    int col;
    if (ErrorCode || index < 0 || index >= NumIfaceNodes || pollut < 0 ||
        pollut >= Nobjects[POLLUT] || !IfacePolluts) return 0.0;
    col = IfacePolluts[pollut];
    if (col < 0) return 0.0;
    return (1.0-IfaceFrac)*OldIfaceValues[index][col+1] + IfaceFrac*NewIfaceValues[index][col+1];
}

static int ifacePrint(FILE* file, const char* format, ...)
{
    char buffer[MAXLINE+1];
    int size, i;
    va_list args;
    if (ErrorCode) return FALSE;
    va_start(args, format); size = vsnprintf(buffer, sizeof(buffer), format, args); va_end(args);
    if (size < 0 || size >= (int)sizeof(buffer))
        return ifaceFail(ERR_ROUTING_FILE_WRITE, Foutflows.name);
    for (i = 0; i < size; i++)
    {
        if (buffer[i] == '\n') IfaceOutputLine = 0;
        else if (++IfaceOutputLine >= MAXLINE-1)
            return ifaceFail(ERR_ROUTING_FILE_WRITE, Foutflows.name);
    }
    if (!file || fwrite(buffer, 1, (size_t)size, file) != (size_t)size)
        return ifaceFail(ERR_ROUTING_FILE_WRITE, Foutflows.name);
    return TRUE;
}

void iface_saveOutletResults(DateTime reportDate, FILE* file)
{
    int i, p, yr, mon, day, hr, min, sec;
    double flow;
    char theDate[26];
    if (ErrorCode) return;
    datetime_decodeDate(reportDate, &yr, &mon, &day);
    datetime_decodeTime(reportDate, &hr, &min, &sec);
    snprintf(theDate, 26, " %04d %02d  %02d  %02d  %02d  %02d ", yr, mon, day, hr, min, sec);
    for (i = 0; i < Nobjects[NODE]; i++)
    {
        if (!isOutletNode(i)) continue;
        flow = Node[i].inflow * UCF(FLOW);
        if (!isfinite(flow)) { ifaceFail(ERR_ROUTING_FILE_WRITE, Foutflows.name); return; }
        for (p = 0; p < Nobjects[POLLUT]; p++)
            if (!isfinite(Node[i].newQual[p]))
            { ifaceFail(ERR_ROUTING_FILE_WRITE, Foutflows.name); return; }
        if (!ifacePrint(file, "\n%-16s", Node[i].ID) ||
            !ifacePrint(file, "%s", theDate) || !ifacePrint(file, " %-10f", flow)) return;
        for (p = 0; p < Nobjects[POLLUT]; p++)
            if (!ifacePrint(file, " %-10f", Node[i].newQual[p])) return;
    }
}

void openFileForOutput(void)
{
    int i, n = 0;
    for (i = 0; i < Nobjects[NODE]; i++) if (isOutletNode(i)) n++;
    if (!n) { ifaceFail(ERR_ROUTING_FILE_FORMAT, Foutflows.name); return; }
    Foutflows.file = fopen(Foutflows.name, "wt");
    if (!Foutflows.file) { ifaceFail(ERR_ROUTING_FILE_OPEN, Foutflows.name); return; }
    if (!ifacePrint(Foutflows.file, "SWMM5 Interface File") ||
        !ifacePrint(Foutflows.file, "\n%s", Title[0]) ||
        !ifacePrint(Foutflows.file, "\n%-4d - reporting time step in sec", ReportStep) ||
        !ifacePrint(Foutflows.file, "\n%-4d - number of constituents as listed below:", Nobjects[POLLUT]+1) ||
        !ifacePrint(Foutflows.file, "\nFLOW %s", FlowUnitWords[FlowUnits])) return;
    for (i = 0; i < Nobjects[POLLUT]; i++)
        if (!ifacePrint(Foutflows.file, "\n%s %s", Pollut[i].ID, QualUnitsWords[Pollut[i].units])) return;
    if (!ifacePrint(Foutflows.file, "\n%-4d - number of nodes as listed below:", n)) return;
    for (i = 0; i < Nobjects[NODE]; i++)
        if (isOutletNode(i) && !ifacePrint(Foutflows.file, "\n%s", Node[i].ID)) return;
    if (!ifacePrint(Foutflows.file, "\nNode             Year Mon Day Hr  Min Sec FLOW      ")) return;
    for (i = 0; i < Nobjects[POLLUT]; i++)
        if (!ifacePrint(Foutflows.file, " %-10s", Pollut[i].ID)) return;
    if (ReportStart == StartDateTime) iface_saveOutletResults(ReportStart, Foutflows.file);
}
