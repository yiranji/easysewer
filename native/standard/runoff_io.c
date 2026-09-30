/* Checked RUNOFF cache I/O, inserted into the two pinned runoff.c sources.
 * The on-disk counts remain int32. File positions are not serialized and use
 * the platform's 64-bit file API, including on Windows (where long is 32-bit).
 */
static float* RunoffFrame = NULL;
static size_t RunoffItems = 0;
static int RunoffResults = 0;
#define RUNOFF_STAMP "SWMM5-RUNOFF"
#define RUNOFF_STAMP_SIZE (sizeof(RUNOFF_STAMP) - 1)
#define RUNOFF_HEADER_SIZE (RUNOFF_STAMP_SIZE + 4 * sizeof(int))

static int runoffFail(int code)
{
    if (!ErrorCode) report_writeErrorMsg(code, Frunoff.name);
    return FALSE;
}

static int runoffSeek(int64_t position, int origin, int writing)
{
    int result;
#ifdef _WIN32
    result = _fseeki64(Frunoff.file, position, origin);
#else
    result = fseeko(Frunoff.file, (off_t)position, origin);
#endif
    if (result != 0)
        return runoffFail(writing ? ERR_RUNOFF_FILE_WRITE : ERR_RUNOFF_FILE_READ);
    return TRUE;
}

static int64_t runoffTell(int writing)
{
    int64_t position;
#ifdef _WIN32
    position = _ftelli64(Frunoff.file);
#else
    position = ftello(Frunoff.file);
#endif
    if (position < 0)
        runoffFail(writing ? ERR_RUNOFF_FILE_WRITE : ERR_RUNOFF_FILE_READ);
    return position;
}

static int runoffRead(void* data, size_t size, size_t count)
{
    if (fread(data, size, count, Frunoff.file) != count || ferror(Frunoff.file))
        return runoffFail(ERR_RUNOFF_FILE_READ);
    return TRUE;
}

static int runoffWrite(const void* data, size_t size, size_t count)
{
    int64_t position;
    if (ErrorCode) return FALSE;
    position = runoffTell(TRUE);
    if (position < 0) return FALSE;
    if (count && size > (uint64_t)(INT64_MAX - position) / count)
        return runoffFail(ERR_RUNOFF_FILE_WRITE);
    if (fwrite(data, size, count, Frunoff.file) != count || ferror(Frunoff.file))
        return runoffFail(ERR_RUNOFF_FILE_WRITE);
    return TRUE;
}

static int runoffCheckFrame(double previous, double* next, int writing)
{
    size_t i;
    int j;
    double flow;
    int code = writing ? ERR_RUNOFF_FILE_WRITE : ERR_RUNOFF_FILE_FORMAT;
    for (i = 0; i < RunoffItems; i++)
        if (!isfinite(RunoffFrame[i])) return runoffFail(code);
    *next = previous + (double)RunoffFrame[0] * 1000.0;
    if (RunoffFrame[0] <= 0.0f || !isfinite(*next) || *next <= previous)
        return runoffFail(code);
    if (!writing) for (j = 0; j < Nobjects[SUBCATCH]; j++)
    {
        if (!Subcatch[j].groundwater) continue;
        flow = RunoffFrame[1 + (size_t)j * RunoffResults + SUBCATCH_GW_FLOW];
        /* SAVE stores volumetric flow. A zero-area catchment can only
         * represent zero groundwater flow; never silently drop a supplied
         * nonzero flow or divide it by zero. Check before committing a frame. */
        if (Subcatch[j].area == 0.0)
        {
            if (flow != 0.0) return runoffFail(code);
        }
        else if (!isfinite(flow / UCF(FLOW) / Subcatch[j].area))
            return runoffFail(code);
    }
    return TRUE;
}

static void runoffCloseFile(void)
{
    if (Frunoff.file)
    {
        if (Frunoff.mode == SAVE_FILE && !ErrorCode)
        {
            if (runoffSeek(MaxStepsPos, SEEK_SET, TRUE))
                runoffWrite(&Nsteps, sizeof(int), 1);
            if (fflush(Frunoff.file) != 0) runoffFail(ERR_RUNOFF_FILE_WRITE);
        }
        if (fclose(Frunoff.file) != 0)
            runoffFail(Frunoff.mode == SAVE_FILE ? ERR_RUNOFF_FILE_WRITE : ERR_RUNOFF_FILE_READ);
        Frunoff.file = NULL;
        if (Frunoff.mode == SAVE_FILE && ErrorCode) remove(Frunoff.name);
    }
    FREE(RunoffFrame);
    runoffRainClose();
    RunoffItems = 0;
    RunoffResults = 0;
}

static void runoff_initFile(void)
{
    char stamp[RUNOFF_STAMP_SIZE];
    int header[4], i, tail;
    uint64_t items, frameBytes, payload;
    int64_t length;
    double previous = 0.0, next;
    MaxSteps = 0;
    MaxStepsPos = RUNOFF_STAMP_SIZE + 3 * sizeof(int);
    if (ErrorCode || !Frunoff.file) return;
    if (Nobjects[SUBCATCH] < 0 || Nobjects[POLLUT] < 0 ||
        Nobjects[POLLUT] > INT_MAX - (MAX_SUBCATCH_RESULTS - 1))
    { runoffFail(ERR_RUNOFF_FILE_FORMAT); return; }
    RunoffResults = MAX_SUBCATCH_RESULTS - 1 + Nobjects[POLLUT];
    items = 1 + (uint64_t)Nobjects[SUBCATCH] * (uint64_t)RunoffResults;
    if (items > SIZE_MAX / sizeof(float) || items > INT64_MAX / sizeof(float))
    { runoffFail(ERR_MEMORY); return; }
    RunoffItems = (size_t)items;
    frameBytes = items * sizeof(float);
    if (Frunoff.mode == USE_FILE)
    {
        if (!runoffSeek(0, SEEK_END, FALSE)) return;
        length = runoffTell(FALSE);
        if (length < 0) return;
        if ((uint64_t)length < RUNOFF_HEADER_SIZE) { runoffFail(ERR_RUNOFF_FILE_FORMAT); return; }
        if (!runoffSeek(0, SEEK_SET, FALSE) ||
            !runoffRead(stamp, 1, RUNOFF_STAMP_SIZE) || !runoffRead(header, sizeof(int), 4)) return;
        if (memcmp(stamp, RUNOFF_STAMP, RUNOFF_STAMP_SIZE) != 0 ||
            header[0] != Nobjects[SUBCATCH] || header[1] != Nobjects[POLLUT] ||
            header[2] != FlowUnits || header[3] <= 0)
        { runoffFail(ERR_RUNOFF_FILE_FORMAT); return; }
        payload = (uint64_t)length - RUNOFF_HEADER_SIZE;
        if (payload % frameBytes || payload / frameBytes != (uint64_t)header[3])
        { runoffFail(ERR_RUNOFF_FILE_FORMAT); return; }
        MaxSteps = header[3];
    }
    RunoffFrame = calloc(RunoffItems, sizeof(float));
    if (!RunoffFrame) { runoffFail(ERR_MEMORY); return; }
    if (Frunoff.mode == SAVE_FILE)
    {
        header[0] = Nobjects[SUBCATCH]; header[1] = Nobjects[POLLUT];
        header[2] = FlowUnits; header[3] = 0;
        if (!runoffWrite(RUNOFF_STAMP, 1, RUNOFF_STAMP_SIZE) ||
            !runoffWrite(header, sizeof(int), 4)) return;
        if (fflush(Frunoff.file) != 0) runoffFail(ERR_RUNOFF_FILE_WRITE);
        return;
    }
    /* Validate all declared frames, including those after the consumer's run.
     * Buffering one full frame also makes runtime state updates transactional. */
    for (i = 0; i < MaxSteps; i++)
    {
        if (!runoffRead(RunoffFrame, sizeof(float), RunoffItems) ||
            !runoffCheckFrame(previous, &next, FALSE)) return;
        previous = next;
    }
    tail = fgetc(Frunoff.file);
    if (ferror(Frunoff.file)) { runoffFail(ERR_RUNOFF_FILE_READ); return; }
    if (tail != EOF) { runoffFail(ERR_RUNOFF_FILE_FORMAT); return; }
    runoffSeek(RUNOFF_HEADER_SIZE, SEEK_SET, FALSE);
}

static void runoff_saveToFile(float tStep)
{
    int j;
    double next;
    if (ErrorCode || !Frunoff.file || !RunoffFrame) return;
    RunoffFrame[0] = tStep;
    for (j = 0; j < Nobjects[SUBCATCH]; j++)
    {
        subcatch_getResults(j, 1.0, SubcatchResults);
        memcpy(RunoffFrame + 1 + (size_t)j * RunoffResults,
               SubcatchResults, (size_t)RunoffResults * sizeof(float));
    }
    if (!runoffCheckFrame(OldRunoffTime, &next, TRUE)) return;
    runoffWrite(RunoffFrame, sizeof(float), RunoffItems);
}

static void runoff_readFromFile(void)
{
    int i, j, tail;
    float* values;
    double next;
    TGroundwater* gw;
    if (ErrorCode || !Frunoff.file || !RunoffFrame) return;
    if (Nsteps >= MaxSteps) { runoffFail(ERR_RUNOFF_FILE_END); return; }
    if (!runoffRead(RunoffFrame, sizeof(float), RunoffItems) ||
        !runoffCheckFrame(NewRunoffTime, &next, FALSE)) return;
    if (Nsteps == MaxSteps - 1)
    {
        tail = fgetc(Frunoff.file);
        if (ferror(Frunoff.file)) { runoffFail(ERR_RUNOFF_FILE_READ); return; }
        if (tail != EOF) { runoffFail(ERR_RUNOFF_FILE_FORMAT); return; }
    }
    /* Invert the dimensions used by subcatch_getResults at SAVE. Groundwater
     * uses a depth-rate internally, but the cache stores volumetric flow. */
    for (j = 0; j < Nobjects[SUBCATCH]; j++) subcatch_setOldState(j);
    for (j = 0; j < Nobjects[SUBCATCH]; j++)
    {
        values = RunoffFrame + 1 + (size_t)j * RunoffResults;
        Subcatch[j].newSnowDepth = values[SUBCATCH_SNOWDEPTH] / UCF(RAINDEPTH);
        Subcatch[j].evapLoss = values[SUBCATCH_EVAP] / UCF(EVAPRATE);
        Subcatch[j].infilLoss = values[SUBCATCH_INFIL] / UCF(RAINFALL);
        Subcatch[j].newRunoff = values[SUBCATCH_RUNOFF] / UCF(FLOW);
        gw = Subcatch[j].groundwater;
        if (gw)
        {
            /* HOTSTART initializes oldFlow, while newFlow starts at zero.
             * Retain that first boundary; later frames advance both ends. */
            if (Nsteps > 0) gw->oldFlow = gw->newFlow;
            gw->newFlow = Subcatch[j].area > 0.0 ?
                values[SUBCATCH_GW_FLOW] / UCF(FLOW) / Subcatch[j].area : 0.0;
            gw->lowerDepth = values[SUBCATCH_GW_ELEV] / UCF(LENGTH) - gw->bottomElev;
            gw->theta = values[SUBCATCH_SOIL_MOIST];
        }
        for (i = 0; i < Nobjects[POLLUT]; i++)
            Subcatch[j].newQual[i] = values[SUBCATCH_WASHOFF + i];
    }
    OldRunoffTime = NewRunoffTime;
    NewRunoffTime = MIN(next, TotalDuration);
    Nsteps++;
}
