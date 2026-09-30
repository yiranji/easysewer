/* Shared RAIN interface boundary checks for the two pinned source families.
 * This fragment is inserted into rain.c, after its local declarations.
 * Serialized offsets remain signed 32-bit; in-memory arithmetic is checked.
 */
typedef struct
{
    char id[MAXMSG+1];
    int interval, start, end, ordinal;
} RainStation;

static int rainFail(int code)
{
    if (!ErrorCode) report_writeErrorMsg(code,
        code == ERR_RAIN_FILE_FORMAT ? Gage[GageIndex].fname : Frain.name);
    return FALSE;
}

static int rainSeek(long position, int writing)
{
    if (position < 0 || position > INT_MAX ||
        fseek(Frain.file, position, SEEK_SET) != 0)
        return rainFail(writing ? ERR_RAIN_FILE_WRITE : ERR_RAIN_FILE_READ);
    return TRUE;
}

static long rainTell(int writing)
{
    long position = ftell(Frain.file);
    if (position < 0 || position > INT_MAX)
    {
        rainFail(writing ? ERR_RAIN_FILE_WRITE : ERR_RAIN_FILE_READ);
        return -1;
    }
    return position;
}

static int rainRead(void* data, size_t size, size_t count)
{
    if (fread(data, size, count, Frain.file) != count)
        return rainFail(ERR_RAIN_FILE_READ);
    return TRUE;
}

static int rainWrite(const void* data, size_t size, size_t count)
{
    long position;
    if (ErrorCode) return FALSE;
    position = rainTell(TRUE);
    if (position < 0) return FALSE;
    if (count && size > (size_t)(INT_MAX - position) / count)
        return rainFail(ERR_RAIN_FILE_WRITE);
    if (fwrite(data, size, count, Frain.file) != count)
        return rainFail(ERR_RAIN_FILE_WRITE);
    return TRUE;
}

static int rainDateValid(DateTime date)
{
    /* datetime_encodeDate's domain, before any native float-to-int cast. */
    return isfinite(date) && date >= NO_DATE + 1.0 && date < 2958466.0;
}

static int rainSample(DateTime* date, float* value)
{
    if (!rainRead(date, sizeof(DateTime), 1) ||
        !rainRead(value, sizeof(float), 1)) return FALSE;
    if (!rainDateValid(*date) || !isfinite(*value))
        return rainFail(ERR_RAIN_IFACE_FORMAT);
    return TRUE;
}

int rain_readRecord(int j, long position, DateTime previous,
                    DateTime* date, float* value)
{
    DateTime sample;
    float depth;
    long next;
    if (ErrorCode || !Frain.file) return FALSE;
    if (position < Gage[j].startFilePos || position > Gage[j].endFilePos - 12 ||
        (position - Gage[j].startFilePos) % 12 != 0)
        return rainFail(ERR_RAIN_IFACE_FORMAT);
    if (!rainSeek(position, FALSE) || !rainSample(&sample, &depth)) return FALSE;
    if (previous != NO_DATE && sample <= previous)
        return rainFail(ERR_RAIN_IFACE_FORMAT);
    next = rainTell(FALSE);
    if (next < 0) return FALSE;
    if (next != position + 12) return rainFail(ERR_RAIN_FILE_READ);
    *date = sample;
    *value = depth;
    Gage[j].currentFilePos = next;
    return TRUE;
}

static int rainByName(const void* a, const void* b)
{
    const RainStation* x = a;
    const RainStation* y = b;
    int order = strcmp(x->id, y->id);
    if (order) return order;
    return (x->ordinal > y->ordinal) - (x->ordinal < y->ordinal);
}

static int rainByRange(const void* a, const void* b)
{
    const RainStation* x = a;
    const RainStation* y = b;
    int xp = x->start % 12, yp = y->start % 12;
    if (xp != yp) return xp - yp;
    if (x->start != y->start) return (x->start > y->start) ? 1 : -1;
    return (x->end > y->end) - (x->end < y->end);
}

static int rainCheckRange(long start, long end)
{
    DateTime date, previous = NO_DATE;
    float value;
    long position;
    if (!rainSeek(start, FALSE)) return FALSE;
    for (position = start; position < end; position += 12)
    {
        if (!rainSample(&date, &value)) return FALSE;
        if (previous != NO_DATE && date <= previous)
            return rainFail(ERR_RAIN_IFACE_FORMAT);
        previous = date;
    }
    return TRUE;
}

static void initRainFile(void)
{
    RainStation* stations = NULL;
    char stamp[10];
    int count, i, low, high, middle;
    long length, tableEnd, start, end;
    if (ErrorCode || !Frain.file) return;
    if (fseek(Frain.file, 0, SEEK_END) != 0)
    {
        rainFail(ERR_RAIN_FILE_READ);
        return;
    }
    length = rainTell(FALSE);
    if (length < 0) return;
    if (length < 14) { rainFail(ERR_RAIN_IFACE_FORMAT); return; }
    if (!rainSeek(0, FALSE) || !rainRead(stamp, 1, 10) ||
        !rainRead(&count, sizeof(int), 1)) return;
    if (memcmp(stamp, "SWMM5-RAIN", 10) != 0 || count < 0 ||
        count > (length - 14) / (MAXMSG + 1 + 12))
    {
        rainFail(ERR_RAIN_IFACE_FORMAT);
        return;
    }
    tableEnd = 14 + (long)count * (MAXMSG + 1 + 12);
    if (count)
    {
        stations = calloc((size_t)count, sizeof(RainStation));
        if (!stations) { rainFail(ERR_MEMORY); return; }
    }
    for (i = 0; i < count; i++)
    {
        RainStation* s = &stations[i];
        if (!rainRead(s->id, 1, MAXMSG+1) ||
            !rainRead(&s->interval, sizeof(int), 1) ||
            !rainRead(&s->start, sizeof(int), 1) ||
            !rainRead(&s->end, sizeof(int), 1)) goto done;
        if (!memchr(s->id, '\0', MAXMSG+1) || !s->id[0] || s->interval <= 0 ||
            s->start < tableEnd || s->end < s->start || s->end > length ||
            (s->end - s->start) % 12 != 0)
        {
            rainFail(ERR_RAIN_IFACE_FORMAT);
            goto done;
        }
        s->ordinal = i;
    }
    /* Map case-sensitive station IDs; duplicate IDs retain the first row. */
    if (count) qsort(stations, (size_t)count, sizeof(RainStation), rainByName);
    for (i = 0; i < Nobjects[GAGE]; i++)
    {
        if (Gage[i].dataSource != RAIN_FILE) continue;
        low = 0; high = count;
        while (low < high)
        {
            middle = low + (high - low) / 2;
            if (strcmp(stations[middle].id, Gage[i].staID) < 0) low = middle + 1;
            else high = middle;
        }
        if (low == count || strcmp(stations[low].id, Gage[i].staID) != 0 ||
            stations[low].start == stations[low].end)
        {
            if (!ErrorCode) report_writeErrorMsg(ERR_RAIN_FILE_GAGE, Gage[i].ID);
            goto done;
        }
        Gage[i].rainType = RAINFALL_VOLUME;
        Gage[i].rainInterval = stations[low].interval;
        Gage[i].startFilePos = stations[low].start;
        Gage[i].endFilePos = stations[low].end;
        Gage[i].currentFilePos = stations[low].start;
    }
    /* Shared and overlapping aligned spans are scanned once. Different
     * alignments are independent (at most 12 linear scans of actual bytes).
     * Adjacent stations are not merged: their calendars can start over. */
    if (count) qsort(stations, (size_t)count, sizeof(RainStation), rainByRange);
    for (i = 0; i < count; )
    {
        start = stations[i].start; end = stations[i].end; i++;
        if (start == end) continue;
        while (i < count && stations[i].start % 12 == start % 12 &&
               stations[i].start < end)
        {
            if (stations[i].end > end) end = stations[i].end;
            i++;
        }
        if (!rainCheckRange(start, end)) goto done;
    }
done:
    free(stations);
}

static void createRainFile(int count)
{
    int i, k, interval, dummy = -1;
    long header, data, next;
    int first, last;
    char id[MAXMSG+1] = {0};
    if (ErrorCode || !Frain.file) return;
    if (count < 0 || count > (INT_MAX - 14) / (MAXMSG+1+12))
    { rainFail(ERR_RAIN_FILE_WRITE); goto failed; }
    if (!rainWrite("SWMM5-RAIN", 1, 10) ||
        !rainWrite(&count, sizeof(int), 1)) goto failed;
    header = rainTell(TRUE);
    if (header < 0) goto failed;
    if (count > 0) report_writeRainStats(-1, &RainStats);
    id[0] = ' ';
    for (i = 0; i < count; i++)
    {
        if (!rainWrite(id, 1, MAXMSG+1)) goto failed;
        for (k = 0; k < 3; k++)
            if (!rainWrite(&dummy, sizeof(int), 1)) goto failed;
    }
    data = rainTell(TRUE);
    if (data < 0) goto failed;
    for (i = 0; i < Nobjects[GAGE]; i++)
    {
        if (Gage[i].dataSource != RAIN_FILE) continue;
        if (rainFileConflict(i) || !rainSeek(data, TRUE) || !addGageToRainFile(i))
            goto failed;
        next = rainTell(TRUE);
        if (next < 0 || !rainSeek(header, TRUE)) goto failed;
        memset(id, 0, sizeof(id));
        sstrncpy(id, Gage[i].staID, MAXMSG);
        interval = Interval; first = (int)data; last = (int)next;
        if (!rainWrite(id, 1, MAXMSG+1) ||
            !rainWrite(&interval, sizeof(int), 1) ||
            !rainWrite(&first, sizeof(int), 1) ||
            !rainWrite(&last, sizeof(int), 1)) goto failed;
        header = rainTell(TRUE);
        if (header < 0) goto failed;
        data = next;
        report_writeRainStats(i, &RainStats);
    }
    if (fflush(Frain.file) != 0) { rainFail(ERR_RAIN_FILE_WRITE); goto failed; }
    return;
failed:
    if (fclose(Frain.file) != 0) rainFail(ERR_RAIN_FILE_WRITE);
    Frain.file = NULL;
    remove(Frain.name);
}
