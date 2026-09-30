/* Rain resources advance with the consumer clock during USE RUNOFF. Cache
 * frame lengths never advance control history into the future. */
static double* ReplayRawRain = NULL;
static double ReplayRainTime = 0.0;  /* elapsed milliseconds */
static int ReplayRainActive = FALSE;

static void runoffRainClose(void)
{
    FREE(ReplayRawRain);
    ReplayRainTime = 0.0;
    ReplayRainActive = FALSE;
}

static void runoffRainInit(void)
{
    int j;
    double factor;
    if (ErrorCode || Frunoff.mode != USE_FILE) return;
    if ((uint64_t)Nobjects[GAGE] > SIZE_MAX / sizeof(double))
    {
        report_writeErrorMsg(ERR_MEMORY, "");
        return;
    }
    if (Nobjects[GAGE] > 0)
    {
        ReplayRawRain = calloc((size_t)Nobjects[GAGE], sizeof(double));
        if (!ReplayRawRain) { report_writeErrorMsg(ERR_MEMORY, ""); return; }
    }
    /* Rewind the consumer's gages after RDII preparation/project initialization.
     * Keep lookahead samples unadjusted: an interval can cross a month, and a
     * zero factor must not discard next month's nonzero rain. */
    factor = Adjust.rainFactor;
    Adjust.rainFactor = 1.0;
    for (j = 0; j < Nobjects[GAGE] && !ErrorCode; j++)
    {
        gage_initState(j);
        memset(Gage[j].pastRain, 0, sizeof(Gage[j].pastRain));
        Gage[j].pastInterval = 0;
        ReplayRawRain[j] = Gage[j].rainfall;
    }
    Adjust.rainFactor = factor;
    ReplayRainTime = 0.0;
    ReplayRainActive = !ErrorCode;
}

static int runoffRainState(DateTime date)
{
    int j, owner;
    double factor = Adjust.rainFactor;
    double monthly = Adjust.rain[datetime_monthOfYear(date) - 1];
    Adjust.rainFactor = 1.0;
    for (j = 0; j < Nobjects[GAGE]; j++) Gage[j].rainfall = ReplayRawRain[j];
    for (j = 0; j < Nobjects[GAGE] && !ErrorCode; j++)
        gage_setReplayState(j, date);
    Adjust.rainFactor = factor;
    if (ErrorCode) return FALSE;
    for (j = 0; j < Nobjects[GAGE]; j++)
    {
        ReplayRawRain[j] = Gage[j].rainfall;
        owner = Gage[j].coGage >= 0 ? Gage[j].coGage : j;
        if (!Gage[j].isUsed || IgnoreRainfall) Gage[j].rainfall = 0.0;
        else if (Gage[owner].apiRainfall == MISSING) Gage[j].rainfall *= monthly;
        if (!isfinite(Gage[j].rainfall))
        {
            report_writeErrorMsg(ERR_NUMBER, Gage[j].ID);
            return FALSE;
        }
    }
    return TRUE;
}

static DateTime runoffRainNext(DateTime date, DateTime limit)
{
    int j, year, month, day;
    DateTime next = limit, candidate;
    if (IgnoreRainfall) return limit;
    for (j = 0; j < Nobjects[GAGE]; j++)
    {
        if (!Gage[j].isUsed || Gage[j].coGage >= 0 || Gage[j].apiRainfall != MISSING)
            continue;
        candidate = NO_DATE;
        if (Gage[j].startDate != NO_DATE && date < Gage[j].startDate)
            candidate = Gage[j].startDate;
        else if (date < Gage[j].endDate) candidate = Gage[j].endDate;
        else if (Gage[j].nextDate != NO_DATE && date < Gage[j].nextDate)
            candidate = Gage[j].nextDate;
        if (candidate != NO_DATE && candidate < next) next = candidate;
    }
    /* Monthly multipliers apply to the time being consumed, not lookahead's
     * read time. At most one event per month, regardless of cache frame size. */
    datetime_decodeDate(date, &year, &month, &day);
    if (month < 12 || year < 9999)
    {
        candidate = datetime_encodeDate(year + (month == 12), month == 12 ? 1 : month + 1, 1);
        if (candidate > date && candidate < next) next = candidate;
    }
    return next;
}

static int runoffRainAccumulate(int j, double start, double end)
{
    int i, shift;
    double values[MAXPASTRAIN + 1];
    double rate = Gage[j].rainfall;
    double firstHour = floor(start / 3600000.0);
    double lastHour = floor(end / 3600000.0);
    double crossed = lastHour - firstHour;
    double before = start - firstHour * 3600000.0;
    double after = end - lastHour * 3600000.0;
    memcpy(values, Gage[j].pastRain, sizeof(values));
    if (crossed == 0.0) values[0] += rate * ((end - start) / 3600000.0);
    else
    {
        /* No hour-by-hour loop or unbounded float-to-int cast. Anything older
         * than the 48 completed bins cannot affect an available control. */
        shift = crossed > MAXPASTRAIN ? MAXPASTRAIN + 1 : (int)crossed;
        for (i = MAXPASTRAIN; i > 0; i--)
        {
            if (i > shift) values[i] = values[i - shift];
            else if (i == shift) values[i] = values[0] + rate * ((3600000.0 - before) / 3600000.0);
            else values[i] = rate;
        }
        values[0] = rate * (after / 3600000.0);
    }
    for (i = 0; i <= MAXPASTRAIN; i++) if (!isfinite(values[i]))
    {
        report_writeErrorMsg(ERR_NUMBER, Gage[j].ID);
        return FALSE;
    }
    memcpy(Gage[j].pastRain, values, sizeof(values));
    Gage[j].pastInterval = (int)(after / 1000.0); /* bounded legacy inspection */
    return TRUE;
}

void runoff_updateReplayRain(double time)
{
    int j;
    double nextTime;
    DateTime date, nextDate, endDate;
    if (!ReplayRainActive || ErrorCode) return;
    time = MIN(time, TotalDuration);
    if (!isfinite(time) || time < ReplayRainTime)
    {
        report_writeErrorMsg(ERR_TIMESTEP, "");
        return;
    }
    date = datetime_addSeconds(StartDateTime, ReplayRainTime / 1000.0);
    endDate = datetime_addSeconds(StartDateTime, time / 1000.0);
    if (!runoffRainState(date)) return;
    while (ReplayRainTime < time)
    {
        nextDate = runoffRainNext(date, endDate);
        nextTime = nextDate < endDate ? (nextDate - StartDateTime) * MSECperDAY : time;
        if (nextTime <= ReplayRainTime)
        {
            report_writeErrorMsg(ERR_TIMESTEP, "");
            return;
        }
        for (j = 0; j < Nobjects[GAGE]; j++)
            if (!runoffRainAccumulate(j, ReplayRainTime, nextTime)) return;
        ReplayRainTime = nextTime;
        date = nextDate;
        if (!runoffRainState(date)) return;
    }
}
