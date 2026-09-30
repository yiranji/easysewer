/* Appended to swmm5.c, where DoRunoff/DoRouting identify opened processes.
 * Output and prefix transactions are separate owners; never reopen/truncate
 * an output path here. Rain/RDII generation has completed before this gate. */
static TFile *es_ck_stream_file(int role)
{
    switch (role)
    {
    case 0: return &Fclimate;
    case 1: return &Frain;
    case 2: return &Frunoff;
    case 3: return &Frdii;
    case 4: return &Finflows;
    default: return NULL;
    }
}

static int es_ck_stream_active(int role)
{
    switch (role)
    {
    case 0: return Fclimate.mode == USE_FILE;
    case 1: return !IgnoreRainfall && Frain.mode != NO_FILE;
    case 2: return DoRunoff && Frunoff.mode == USE_FILE;
    case 3: return !IgnoreRainfall && !IgnoreRDII && Frdii.mode != NO_FILE;
    case 4: return DoRouting && Finflows.mode == USE_FILE;
    default: return 0;
    }
}

static const char *es_ck_stream_identity(int role)
{
    TFile *file = es_ck_stream_file(role);
    /* Scratch names change on project reconstruction. The container binds
     * captured content to this stable role, independently of that temp name. */
    if (role == 1 && file->mode == SCRATCH_FILE) return "swmm:rain-scratch";
    if (role == 3 && file->mode == SCRATCH_FILE) return "swmm:rdii-scratch";
    return file->name;
}

void es_ck_input_frames(EsCkCursor *c)
{
    if (es_ck_engine_boundary()) { es_ck_fail(c, ES_CK_STATE); return; }
    es_ck_rdii(c, es_ck_stream_active(3));
    es_ck_iface(c, es_ck_stream_active(4));
}

void es_ck_streams(EsCkCursor *c, EsCkStreamsStage *stage)
{
    int role, active, error, saving;
    uint64_t position;
    size_t begin = c->position;
    TFile *file;
    if (c->error) return;
    if (es_ck_engine_boundary()) { es_ck_fail(c, ES_CK_STATE); return; }
    if (c->mode == ES_CK_APPLY)
    {
        if (!stage || !stage->validated || !stage->prepared || stage->committed ||
            stage->begin != c->position || stage->end > c->size)
        { es_ck_fail(c, ES_CK_STATE); return; }
        for (role = 0; role < ES_CK_READ_STREAMS; role++) if (stage->active[role])
        {
            file = es_ck_stream_file(role);
            stage->retired[role] = file->file;
            file->file = stage->replacement[role];
            stage->replacement[role] = NULL;
        }
        stage->committed = 1; c->position = stage->end;
        return;
    }
    if (c->mode == ES_CK_VALIDATE)
    {
        if (!stage || stage->validated || stage->prepared || stage->committed)
        { es_ck_fail(c, ES_CK_STATE); return; }
    }
    saving = c->mode == ES_CK_MEASURE || c->mode == ES_CK_WRITE;
    es_ck_tag(c, "ESREAD01", 8);
    es_ck_fixed_u32(c, ES_CK_READ_STREAMS);
    for (role = 0; role < ES_CK_READ_STREAMS && !c->error; role++)
    {
        file = es_ck_stream_file(role); active = es_ck_stream_active(role);
        es_ck_fixed_i32(c, role); es_ck_fixed_i32(c, file->mode);
        es_ck_fixed_i32(c, active);
        position = 0;
        if (active)
        {
            es_ck_identity(c, c->logical_resources ? es_ck_stream_key(role) : es_ck_stream_identity(role));
            if (saving)
            {
                if (!file->file) { es_ck_fail(c, ES_CK_STATE); return; }
                error = es_ck_read_position(file->file, &position);
                if (error) { es_ck_fail(c, error); return; }
            }
            position = es_ck_u64(c, &position);
            if (position > INT64_MAX) es_ck_fail(c, ES_CK_NUMBER);
        }
        if (stage && c->mode == ES_CK_VALIDATE)
        { stage->active[role] = active; stage->position[role] = position; }
    }
    if (!c->error && c->mode == ES_CK_VALIDATE)
    { stage->begin = begin; stage->end = c->position; stage->validated = 1;
      stage->logical_resources = c->logical_resources; }
}

int es_ck_streams_prepare(EsCkStreamsStage *stage, const EsCkReadResources *resources)
{
    int role, error;
    if (!stage || !stage->validated || stage->prepared || stage->committed ||
        !resources || !resources->open_read) return ES_CK_STATE;
    for (role = 0; role < ES_CK_READ_STREAMS; role++)
        if (stage->replacement[role] || stage->retired[role]) return ES_CK_STATE;
    for (role = 0; role < ES_CK_READ_STREAMS; role++) if (stage->active[role])
    {
        error = es_ck_read_prepare(&stage->replacement[role], resources,
                                   stage->logical_resources ? es_ck_stream_key(role) : es_ck_stream_identity(role),
                                   stage->position[role]);
        if (error) return error;
    }
    stage->prepared = 1;
    return 0;
}

int es_ck_streams_discard(EsCkStreamsStage *stage)
{
    int role, error = 0, next;
    if (!stage) return 0;
    for (role = 0; role < ES_CK_READ_STREAMS; role++)
    {
        next = es_ck_read_close(stage->committed ? &stage->retired[role] : &stage->replacement[role]);
        if (!error) error = next;
    }
    memset(stage, 0, sizeof(*stage));
    return error;
}
