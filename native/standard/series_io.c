/* External time-series records and independently owned interpolation cursors.
 * Included in table.c. Inline entry lists always remain owned by the project. */
static int tsFail(TTable *table, int code)
{
    if (!ErrorCode) report_writeErrorMsg(code, table->file.name);
    return FALSE;
}

int table_closeFile(TTable *table)
{
    FILE *file;
    TTable *lookup = table->lookup;
    int code = 0, next;
    table->lookup = NULL;
    if (lookup) {
        next = table_closeFile(lookup);
        if (!code) code = next;
        /* Its entry list is borrowed, so do not call table_deleteEntries. */
        free(lookup);
    }
    file = table->file.file;
    table->file.file = NULL;
    table->lookupReady = table->lookupEnded = 0;
    table->fileHasPrevious = 0;
    if (file && fclose(file) != 0) {
        tsFail(table, ERR_TABLE_FILE_READ);
        if (!code) code = ERR_TABLE_FILE_READ;
    }
    return code;
}

int table_closeFiles(void)
{
    int i, code = 0, next;
    if (Tseries) for (i = 0; i < Nobjects[TSERIES]; i++) {
        next = table_closeFile(&Tseries[i]);
        if (!code) code = next;
    }
    if (Curve) for (i = 0; i < Nobjects[CURVE]; i++) {
        next = table_closeFile(&Curve[i]);
        if (!code) code = next;
    }
    return code;
}

static int tsSpace(unsigned char c)
{
    return c == ' ' || c == '\t' || c == '\v' || c == '\f' || c == '\r' || c == '\n';
}

static int tsLine(TTable *table, char *line)
{
    int c, n = 0, i;
    if (!table->file.file) { tsFail(table, ERR_TABLE_FILE_READ); return -1; }
    while ((c = fgetc(table->file.file)) != EOF) {
        if (c == '\n') break;
        if (c == 0 || c == 26 || n >= MAXLINE - 1) {
            tsFail(table, ERR_TABLE_FILE_READ); return -1;
        }
        line[n++] = (char)c;
    }
    if (ferror(table->file.file)) { tsFail(table, ERR_TABLE_FILE_READ); return -1; }
    if (n && line[n-1] == '\r') n--;
    line[n] = 0;
    if (n > MAXLINE - 2) { tsFail(table, ERR_TABLE_FILE_READ); return -1; }
    for (i = 0; i < n; i++) if (line[i] == '\r') {
        tsFail(table, ERR_TABLE_FILE_READ); return -1;
    }
    return c == EOF && n == 0 ? 0 : 1;
}

static int tsNumber(const char *text, double *value)
{
    const char *p;
    char *end;
    if (!*text) return FALSE;
    for (p = text; *p; p++) if (!strchr("0123456789+-.eE", *p)) return FALSE;
    *value = strtod(text, &end);
    return end != text && !*end && isfinite(*value);
}

static int tsWhole(const char *text, int *value)
{
    int digit, n = 0;
    if (*text == '+') text++;
    if (!*text) return FALSE;
    for (; *text; text++) {
        digit = (unsigned char)*text - '0';
        if (digit < 0 || digit > 9 || n > (INT_MAX-digit)/10) return FALSE;
        n = n*10 + digit;
    }
    *value = n;
    return TRUE;
}

/* The fixed SWMM file profile uses month/day/year, with three-letter months
 * also accepted. Validate components before any native integer conversion. */
static int tsDate(char *text, double *date)
{
    static const char *months[] = {"JAN","FEB","MAR","APR","MAY","JUN",
                                  "JUL","AUG","SEP","OCT","NOV","DEC"};
    char *parts[3], *p;
    int n = 1, month = 0, day, year, i;
    parts[0] = text;
    for (p = text; *p; p++) if (*p == '/' || *p == '-') {
        if (n == 3) return FALSE;
        *p = 0; parts[n++] = p+1;
    }
    if (n != 3 || !tsWhole(parts[1], &day) || !tsWhole(parts[2], &year)) return FALSE;
    if (!tsWhole(parts[0], &month)) {
        for (i = 0; i < 12; i++) if (strcomp(parts[0], (char*)months[i])) month = i+1;
    }
    if (year < 1 || year > 9999 || month < 1 || month > 12 ||
        day < 1 || day > datetime_daysPerMonth(year, month)) return FALSE;
    *date = datetime_encodeDate(year, month, day);
    return TRUE;
}

static int tsTime(char *text, double *time)
{
    char *parts[3], *p, *fraction;
    int n = 1, hours, minutes, seconds = 0;
    if (!strchr(text, ':')) {
        if (!tsNumber(text, time)) return FALSE;
        *time /= 24.0;
        return TRUE;
    }
    parts[0] = text;
    for (p = text; *p; p++) if (*p == ':') {
        if (n == 3) return FALSE;
        *p = 0; parts[n++] = p+1;
    }
    if (n < 2 || !tsWhole(parts[0], &hours) || !tsWhole(parts[1], &minutes)) return FALSE;
    if (n == 3) {
        /* Preserve documented native integer-second coercion, without sscanf. */
        fraction = strchr(parts[2], '.');
        if (fraction) {
            *fraction++ = 0;
            if (!*fraction) return FALSE;
            for (p = fraction; *p; p++) if (*p < '0' || *p > '9') return FALSE;
        }
        if (!tsWhole(parts[2], &seconds)) return FALSE;
    }
    if (minutes >= 60 || seconds >= 60) return FALSE;
    *time = (3600.0*hours + 60.0*minutes + seconds)/86400.0;
    return TRUE;
}

int table_parseFileLine(char *line, TTable *table, double *x, double *y)
{
    char *parts[3], *p = line;
    int n = 0;
    double date = table->lastDate, time, value, stamp;
    while (tsSpace((unsigned char)*p)) p++;
    if (!*p || *p == ';') return -1;
    /* Extra columns retain their documented ignored-column semantics. */
    while (*p && n < 3) {
        while (tsSpace((unsigned char)*p)) p++;
        if (!*p) break;
        parts[n++] = p;
        while (*p && !tsSpace((unsigned char)*p)) p++;
        if (*p) *p++ = 0;
    }
    if (n < 2 || (n == 3 && !tsDate(parts[0], &date))) return FALSE;
    if (!tsTime(parts[n-2], &time) || !tsNumber(parts[n-1], &value)) return FALSE;
    stamp = date + time;
    if (!isfinite(stamp) || stamp < datetime_encodeDate(1,1,1) ||
        stamp >= datetime_encodeDate(9999,12,31)+1.0) return FALSE;
    table->lastDate = date;
    *x = stamp; *y = value;
    return TRUE;
}

int table_getNextFileEntry(TTable *table, double *x, double *y)
{
    char line[MAXLINE+1];
    double date, value;
    int status;
    if (ErrorCode) return FALSE;
    while ((status = tsLine(table, line)) > 0) {
        status = table_parseFileLine(line, table, &date, &value);
        if (status < 0) continue;
        if (!status) return tsFail(table, ERR_TABLE_FILE_READ);
        if (!table->fileValidating && table->fileHasPrevious && date <= table->filePrevious)
            return tsFail(table, ERR_TABLE_FILE_READ);
        table->filePrevious = date;
        table->fileHasPrevious = TRUE;
        *x = date; *y = value;
        return TRUE;
    }
    return FALSE;
}

static int tsBracket(TTable *cursor)
{
    cursor->lookupReady = cursor->lookupEnded = FALSE;
    if (!table_getFirstEntry(cursor, &cursor->x1, &cursor->y1)) return FALSE;
    cursor->x2 = cursor->x1; cursor->y2 = cursor->y1;
    if (!table_getNextEntry(cursor, &cursor->x2, &cursor->y2)) cursor->lookupEnded = TRUE;
    if (ErrorCode) return FALSE;
    cursor->lookupReady = TRUE;
    return TRUE;
}

void table_tseriesInit(TTable *table)
{
    TTable *cursor = table->lookup;
    if (cursor) {
        table->lookup = NULL;
        table_closeFile(cursor);
        free(cursor);
    }
    if (ErrorCode) return;
    cursor = (TTable*)calloc(1, sizeof(TTable));
    if (!cursor) { tsFail(table, ERR_MEMORY); return; }
    *cursor = *table;
    cursor->lookup = NULL;
    cursor->file.file = NULL;
    cursor->fileValidating = FALSE;
    table->lookup = cursor;
    if (cursor->file.mode == USE_FILE) {
        cursor->file.file = fopen(cursor->file.name, "rb");
        if (!cursor->file.file) { tsFail(cursor, ERR_TABLE_FILE_OPEN); return; }
    }
    if (!tsBracket(cursor) && cursor->file.mode == USE_FILE && !ErrorCode)
        tsFail(cursor, ERR_TABLE_FILE_READ);
}

double table_tseriesLookup(TTable *table, double x, char extend)
{
    TTable *cursor;
    double value, weight;
    if (ErrorCode) return 0.0;
    if (!isfinite(x) || x == NO_DATE) { tsFail(table, ERR_TIMESTEP); return 0.0; }
    if (!table->lookup) table_tseriesInit(table);
    cursor = table->lookup;
    if (ErrorCode || !cursor || !cursor->lookupReady) return 0.0;
    /* Rewind must precede the EOF shortcut, even after an earlier query went
     * beyond the last point. Sequential iteration uses the original table. */
    if (x < cursor->x1 && !tsBracket(cursor)) return 0.0;
    if (x < cursor->x1) return extend ? cursor->y1 : 0.0;
    while (!cursor->lookupEnded && x > cursor->x2) {
        cursor->x1 = cursor->x2; cursor->y1 = cursor->y2;
        if (!table_getNextEntry(cursor, &cursor->x2, &cursor->y2)) cursor->lookupEnded = TRUE;
        if (ErrorCode) return 0.0;
    }
    if (x > cursor->x2) return extend ? cursor->y2 : 0.0;
    if (cursor->x1 == cursor->x2) return cursor->y1;
    value = table_interpolate(x, cursor->x1, cursor->y1, cursor->x2, cursor->y2);
    if (!isfinite(value)) {
        /* Preserve reference arithmetic normally; avoid an overflowing
         * difference between opposite, individually finite endpoint values. */
        weight = (x-cursor->x1)/(cursor->x2-cursor->x1);
        value = (1.0-weight)*cursor->y1 + weight*cursor->y2;
    }
    if (!isfinite(value)) { tsFail(cursor, ERR_TABLE_FILE_READ); return 0.0; }
    return value;
}
