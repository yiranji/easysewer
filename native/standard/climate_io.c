/* Checked, bounded climate records. Included in climate.c after declarations. */
static int ClPending, ClEof, ClLastMonth, ClRowYear, ClRowMonth, ClHaveRow;
static int ClColumns[6], ClEnds[6]; /* DATE, TMAX, TMIN, EVAP, WDMV, AWND */
static double ClValues[4][32];
static DateTime ClLastState = NO_DATE;
static double ClEvapRate;
static TTable ClEvapSeries, ClTempSeries;
int table_getNextFileEntry(TTable *table, double *x, double *y);

/* Entries belong to the project; traversal state and external streams belong
 * to the individual consumer. Never delete the shared inline entry list. */
static int clCloseSeries(TTable *series)
{
    FILE *file = series->file.file;
    series->file.file = NULL;
    if (file && fclose(file) != 0) {
        if (!ErrorCode) report_writeErrorMsg(ERR_TABLE_FILE_READ, series->file.name);
        return ERR_TABLE_FILE_READ;
    }
    return 0;
}

static int clSeriesError(TTable *series)
{
    if (series->file.file && ferror(series->file.file)) {
        if (!ErrorCode) report_writeErrorMsg(ERR_TABLE_FILE_READ, series->file.name);
        return 1;
    }
    return 0;
}

static int clInitSeries(TTable *series, int index)
{
    if (clCloseSeries(series)) return 0;
    *series = Tseries[index];
    series->file.file = NULL;
    series->thisEntry = series->firstEntry;
    if (series->file.mode == USE_FILE) {
        series->file.file = fopen(series->file.name, "rb");
        if (!series->file.file) {
            if (!ErrorCode) report_writeErrorMsg(ERR_TABLE_FILE_OPEN, series->file.name);
            return 0;
        }
    }
    return 1;
}

static int clFirstEntry(TTable *series, double *date, double *value)
{
    int found;
    if (series->file.mode != USE_FILE) return table_getFirstEntry(series, date, value);
    if (!series->file.file || fseek(series->file.file, 0, SEEK_SET)) {
        if (!ErrorCode) report_writeErrorMsg(ERR_TABLE_FILE_READ, series->file.name);
        return 0;
    }
    clearerr(series->file.file);
    found = table_getNextFileEntry(series, date, value);
    if ((!found || clSeriesError(series)) && !ErrorCode)
        report_writeErrorMsg(ERR_TABLE_FILE_READ, series->file.name);
    return found && !ErrorCode;
}

/* Keep an unadjusted current rate separate from the next series entry. A
 * caller can pass several events in one step; each must be consumed once. */
static void clAdvanceEvap(DateTime date)
{
    double next, rate;
    if (Evap.tSeries < 0) return;
    while (NextEvapDate != NO_DATE && NextEvapDate <= date) {
        ClEvapRate = NextEvapRate;
        if (!table_getNextEntry(&ClEvapSeries, &next, &rate) || next > EndDateTime) {
            clSeriesError(&ClEvapSeries);
            NextEvapDate = NO_DATE;
            return;
        }
        NextEvapDate = next;
        NextEvapRate = rate;
    }
}

static int clFail(int code)
{
    if (!ErrorCode) report_writeErrorMsg(code, Fclimate.name);
    return -1;
}

int climate_closeFile(void)
{
    FILE *file = Fclimate.file;
    int code = 0, next;
    Fclimate.file = NULL;
    ClPending = 0;
    ClHaveRow = 0;
    ClEof = 1;
    if (file && fclose(file) != 0) {
        clFail(ERR_CLIMATE_FILE_READ);
        code = ERR_CLIMATE_FILE_READ;
    }
    next = clCloseSeries(&ClTempSeries);
    if (!code) code = next;
    next = clCloseSeries(&ClEvapSeries);
    if (!code) code = next;
    return code;
}

/* A complete last record is data even when reading it also reaches EOF. */
static int clLine(void)
{
    int ch, count = 0, i;
    FileLine[0] = 0;
    if (!Fclimate.file) return clFail(ERR_CLIMATE_FILE_READ);
    while ((ch = fgetc(Fclimate.file)) != EOF) {
        if (ch == '\n') break;
        if (ch == 0 || ch == 26 || count >= MAXLINE - 1)
            return clFail(ERR_CLIMATE_FILE_READ);
        FileLine[count++] = (char)ch;
    }
    if (ferror(Fclimate.file)) return clFail(ERR_CLIMATE_FILE_READ);
    if (count && FileLine[count-1] == '\r') count--;
    FileLine[count] = 0;
    if (count > MAXLINE - 2) return clFail(ERR_CLIMATE_FILE_READ);
    for (i = 0; i < count; i++)
        if (FileLine[i] == '\r') return clFail(ERR_CLIMATE_FILE_READ);
    return ch == EOF && count == 0 ? 0 : 1;
}

static int clSpace(int ch)
{
    return ch == ' ' || ch == '\t' || ch == '\v' || ch == '\f';
}

/* Split at most capacity tokens; later USER columns remain unconsumed. */
static int clTokens(char *line, char **tokens, int capacity)
{
    int count = 0;
    while (*line && count < capacity) {
        while (clSpace((unsigned char)*line)) line++;
        if (!*line) break;
        tokens[count++] = line;
        while (*line && !clSpace((unsigned char)*line)) line++;
        if (*line) *line++ = 0;
    }
    return count;
}

static int clInteger(const char *text, int width, int *value)
{
    int i = 0, end = width, found = 0, number = 0;
    while (i < end && clSpace((unsigned char)text[i])) i++;
    while (end > i && clSpace((unsigned char)text[end-1])) end--;
    for (; i < end; i++) {
        int digit = (unsigned char)text[i] - '0';
        if (digit < 0 || digit > 9 || number > (INT_MAX-digit)/10) return 0;
        number = number*10 + digit;
        found = 1;
    }
    if (found) *value = number;
    return found;
}

static int clNumber(const char *text, int width, double *value)
{
    char buffer[MAXLINE+1], *end;
    int i;
    if (width < 1 || width > MAXLINE) return 0;
    memcpy(buffer, text, width);
    buffer[width] = 0;
    for (i = 0; i < width; i++)
        if (!strchr("0123456789+-.eE \t\v\f", buffer[i])) return 0;
    *value = strtod(buffer, &end);
    if (end == buffer || !isfinite(*value)) return 0;
    while (clSpace((unsigned char)*end)) end++;
    return *end == 0;
}

static int clDate(int year, int month, int day)
{
    return year >= 1 && year <= 9999 && month >= 1 && month <= 12 &&
        day >= 1 && day <= datetime_daysPerMonth(year, month);
}

static int clStore(int variable, int day, double value)
{
    if (!isfinite(value)) return clFail(ERR_CLIMATE_FILE_READ);
    if (value != MISSING) ClValues[variable][day] = value;
    return 1;
}

static int clHeader(void)
{
    const char *labels[] = {"DATE", "TMAX", "TMIN", "EVAP", "WDMV", "AWND"};
    char copy[MAXLINE+1], *tokens[(MAXLINE+1)/2];
    int i, j, count, position;
    for (i = 0; FileLine[i]; i++)
        if ((unsigned char)FileLine[i] > 127) return 0;
    strcpy(copy, FileLine);
    count = clTokens(copy, tokens, (MAXLINE+1)/2);
    for (i = 0; i < 6; i++) ClColumns[i] = ClEnds[i] = -1;
    for (i = 0; i < count; i++) {
        for (j = 0; j < i; j++) if (!strcmp(tokens[i], tokens[j])) return 0;
        position = (int)(tokens[i] - copy);
        for (j = 0; j < 6; j++) if (!strcmp(tokens[i], labels[j])) {
            ClColumns[j] = position;
            ClEnds[j] = i+1 < count ? (int)(tokens[i+1]-copy) : MAXLINE;
        }
    }
    if (ClColumns[0] < 0) return 0;
    for (i = 1; i < 6; i++) if (ClColumns[i] >= 0) {
        FileWindType = ClColumns[4] >= 0 ? WDMV : AWND;
        return 1;
    }
    return 0;
}

static int getFileFormat(void)
{
    char copy[MAXLINE+1], *tokens[5];
    int length = (int)strlen(FileLine), count, y, m, d, parameter;
    if (length >= 27 && !strncmp(FileLine, "DLY", 3) &&
        !strncmp(FileLine+23, "9999", 4)) return TD3200;
    if (length >= 233 && clInteger(FileLine+13, 3, &parameter) &&
        (parameter == 1 || parameter == 2 || parameter == 151)) return DLY0204;
    strcpy(copy, FileLine);
    count = clTokens(copy, tokens, 5);
    if (count >= 5 && clInteger(tokens[1], (int)strlen(tokens[1]), &y) &&
        clInteger(tokens[2], (int)strlen(tokens[2]), &m) &&
        clInteger(tokens[3], (int)strlen(tokens[3]), &d)) return USER_PREPARED;
    return clHeader() ? GHCND : UNKNOWN_FORMAT;
}

static int clUser(void)
{
    char copy[MAXLINE+1], *tokens[8];
    int count, day, i;
    const int variables[] = {TMAX, TMIN, EVAP, WIND};
    double value;
    strcpy(copy, FileLine);
    count = clTokens(copy, tokens, 8);
    if (count < 4 || !clInteger(tokens[1], (int)strlen(tokens[1]), &ClRowYear) ||
        !clInteger(tokens[2], (int)strlen(tokens[2]), &ClRowMonth) ||
        !clInteger(tokens[3], (int)strlen(tokens[3]), &day) ||
        !clDate(ClRowYear, ClRowMonth, day)) return clFail(ERR_CLIMATE_FILE_READ);
    for (i = 4; i < count; i++) {
        if (*tokens[i] == '*') continue;
        if (!clNumber(tokens[i], (int)strlen(tokens[i]), &value))
            return clFail(ERR_CLIMATE_FILE_READ);
        if (i < 6 && UnitSystem == SI) value = 9./5.*value + 32.0;
        if (clStore(variables[i-4], day, value) < 0) return -1;
    }
    return 1;
}

static int clMonthly(void)
{
    int length = (int)strlen(FileLine), count, parameter = -1, day, i, offset, magnitude, code;
    char sign, quality;
    double value;
    if (FileFormat == TD3200) {
        if (length < 30 || strncmp(FileLine, "DLY", 3) || strncmp(FileLine+23, "9999", 4) ||
            !clInteger(FileLine+17, 4, &ClRowYear) || !clInteger(FileLine+21, 2, &ClRowMonth) ||
            !clInteger(FileLine+27, 3, &count) || count > 31 || length < 30+12*count)
            return clFail(ERR_CLIMATE_FILE_READ);
        for (i = 0; i < 4; i++) if (!strncmp(FileLine+11, ClimateVarWords[i], 4)) parameter = i;
    } else {
        if (length < 233 || !clInteger(FileLine+7, 4, &ClRowYear) ||
            !clInteger(FileLine+11, 2, &ClRowMonth) || !clInteger(FileLine+13, 3, &code))
            return clFail(ERR_CLIMATE_FILE_READ);
        count = 31;
        if (code == 1) parameter = TMAX;
        else if (code == 2) parameter = TMIN;
        else if (code == 151) parameter = EVAP;
    }
    if (!clDate(ClRowYear, ClRowMonth, 1)) return clFail(ERR_CLIMATE_FILE_READ);
    for (i = 0; i < count; i++) {
        offset = FileFormat == TD3200 ? 30+12*i : 16+7*i;
        if (FileFormat == TD3200) {
            if (!clInteger(FileLine+offset, 2, &day) || day < 1 || day > 31)
                return clFail(ERR_CLIMATE_FILE_READ);
            sign = FileLine[offset+4]; quality = FileLine[offset+11]; offset += 5;
        } else {
            day = i+1; sign = FileLine[offset]; quality = FileLine[offset+6]; offset++;
        }
        if (sign != '+' && sign != '-' && sign != ' ') return clFail(ERR_CLIMATE_FILE_READ);
        if (FileFormat == DLY0204 && !strncmp(FileLine+offset, "     ", 5)) continue;
        if (!clInteger(FileLine+offset, 5, &magnitude)) return clFail(ERR_CLIMATE_FILE_READ);
        if (magnitude == 99999 || parameter < 0) continue;
        value = magnitude;
        if (FileFormat == TD3200) {
            if (quality != '0' && quality != '1') continue;
            if (sign == '-') value = -value;
            if (parameter == EVAP) {
                value /= 100.0;
                if (UnitSystem == SI) value *= MMperINCH;
            } else if (parameter == WIND) value /= 24.0;
        } else {
            value /= 10.0;
            if (parameter == EVAP) {
                if (UnitSystem == US) value /= MMperINCH;
            } else {
                if (sign == '-') value = -value;
                value = 9./5.*value + 32.0;
            }
        }
        if (clStore(parameter, day, value) < 0) return -1;
    }
    return 1;
}

static int clGhcnd(void)
{
    int i, begin, end, length = (int)strlen(FileLine), day, variable, width;
    char buffer[MAXLINE+1], *after;
    double value;
    begin = ClColumns[0]; end = ClEnds[0] < length ? ClEnds[0] : length;
    while (begin < end && clSpace((unsigned char)FileLine[begin])) begin++;
    while (end > begin && clSpace((unsigned char)FileLine[end-1])) end--;
    if (end-begin != 8 || !clInteger(FileLine+begin, 4, &ClRowYear) ||
        !clInteger(FileLine+begin+4, 2, &ClRowMonth) ||
        !clInteger(FileLine+begin+6, 2, &day) || !clDate(ClRowYear, ClRowMonth, day))
        return clFail(ERR_CLIMATE_FILE_READ);
    for (i = 1; i < 6; i++) {
        if (ClColumns[i] < 0) continue;
        begin = ClColumns[i]; end = ClEnds[i] < length ? ClEnds[i] : length;
        while (begin < end && clSpace((unsigned char)FileLine[begin])) begin++;
        if (begin >= end) return clFail(ERR_CLIMATE_FILE_READ);
        width = end-begin < 8 ? end-begin : 8;
        memcpy(buffer, FileLine+begin, width); buffer[width] = 0;
        value = strtod(buffer, &after);
        if (after == buffer || !isfinite(value)) return clFail(ERR_CLIMATE_FILE_READ);
        width = (int)(after-buffer);
        if (!clNumber(buffer, width, &value)) return clFail(ERR_CLIMATE_FILE_READ);
        if (begin+width < end && strchr(".0123456789eE+-", FileLine[begin+width]))
            return clFail(ERR_CLIMATE_FILE_READ);
        if (fabs(value) >= 9999.0 || (i == 5 && FileWindType == WDMV)) continue;
        variable = i == 1 ? TMAX : i == 2 ? TMIN : i == 3 ? EVAP : WIND;
        if (clStore(variable, day, convertGhcndValue(variable, value)) < 0) return -1;
    }
    return 1;
}

static int clNext(void)
{
    int status, i, j, blank, month;
    if (ErrorCode) return -1;
    if (ClEof) return 0;
    for (;;) {
        if (ClPending) { ClPending = 0; status = 1; }
        else status = clLine();
        if (status <= 0) { if (!status) ClEof = 1; return status; }
        blank = 1;
        for (i = 0; FileLine[i]; i++) {
            unsigned char ch = (unsigned char)FileLine[i];
            if (FileFormat != USER_PREPARED && ch > 127) return clFail(ERR_CLIMATE_FILE_READ);
            if (!clSpace(ch) && !(FileFormat == GHCND && ch == '-')) blank = 0;
        }
        if (!blank) break;
    }
    for (i = 0; i < 4; i++) for (j = 0; j < 32; j++) ClValues[i][j] = MISSING;
    status = FileFormat == USER_PREPARED ? clUser() : FileFormat == GHCND ? clGhcnd() : clMonthly();
    if (status < 0) return status;
    month = ClRowYear*12 + ClRowMonth;
    if (month < ClLastMonth) return clFail(ERR_CLIMATE_FILE_READ);
    ClLastMonth = month;
    return 1;
}

static int clReset(void)
{
    int status;
    ClEof = ClPending = ClLastMonth = 0;
    if (!Fclimate.file || fseek(Fclimate.file, 0, SEEK_SET)) return clFail(ERR_CLIMATE_FILE_READ);
    clearerr(Fclimate.file);
    status = clLine();
    if (status != 1) return clFail(ERR_CLIMATE_FILE_READ);
    if (!strncmp(FileLine, "\xef\xbb\xbf", 3)) return clFail(ERR_CLIMATE_FILE_READ);
    FileFormat = getFileFormat();
    if (FileFormat == UNKNOWN_FORMAT) return clFail(ERR_CLIMATE_FILE_READ);
    ClPending = FileFormat != GHCND;
    return 1;
}

static void readFileValues(void)
{
    int i, j, wanted = FileYear*12 + FileMonth, month;
    for (i = 0; i < 4; i++) for (j = 0; j < 32; j++) FileData[i][j] = MISSING;
    while (!ErrorCode) {
        if (!ClHaveRow) {
            if (clNext() != 1) return;
            ClHaveRow = 1;
        }
        month = ClRowYear*12 + ClRowMonth;
        if (month > wanted) return;
        if (month == wanted) {
            for (i = 0; i < 4; i++) for (j = 1; j < 32; j++)
                if (ClValues[i][j] != MISSING) FileData[i][j] = ClValues[i][j];
        }
        ClHaveRow = 0;
    }
}

static int clStart(void)
{
    int status, i, wanted;
    ClHaveRow = 0;
    FileValue[TMIN] = FileValue[TMAX] = 70.0;
    FileValue[EVAP] = FileValue[WIND] = 0.0;
    if (clReset() < 0) return -1;
    datetime_decodeDate(Temp.fileStartDate == NO_DATE ? StartDate : Temp.fileStartDate,
                       &FileYear, &FileMonth, &FileDay);
    wanted = FileYear*12 + FileMonth;
    while ((status = clNext()) == 1) {
        if (ClRowYear*12 + ClRowMonth >= wanted) break;
    }
    if (status < 0) return -1;
    if (!status || ClRowYear*12 + ClRowMonth != wanted) return clFail(ERR_CLIMATE_END_OF_FILE);
    ClHaveRow = 1;
    FileElapsedDays = 0;
    FileLastDay = datetime_daysPerMonth(FileYear, FileMonth);
    readFileValues();
    if (ErrorCode) return -1;
    for (i = 0; i < 4; i++) if (FileData[i][FileDay] != MISSING) FileValue[i] = FileData[i][FileDay];
    return 1;
}

void climate_openFile(void)
{
    int status, records = 0;
    ClLastState = NO_DATE;
    if (climate_closeFile()) return;
    if (ErrorCode) return;
    Fclimate.file = fopen(Fclimate.name, "rb");
    if (!Fclimate.file) { clFail(ERR_CLIMATE_FILE_OPEN); return; }
    if (clReset() > 0) {
        while ((status = clNext()) == 1) records = 1;
        if (status == 0 && !records) clFail(ERR_CLIMATE_FILE_READ);
        if (!ErrorCode) clStart();
    }
    if (ErrorCode) climate_closeFile();
}
