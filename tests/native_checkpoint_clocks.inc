/* Test-only partial bundle: engine/routing clocks plus controls. No resume API. */
void es_test_controls_poison(void);
void es_test_poison_routing(void);
int es_test_prepare_controls(EsCkControlsStage *, int);

int DLLEXPORT es_test_clocks_save(void *data, size_t size, size_t *used)
{
    EsCkCursor c;
    es_ck_init(&c, data ? ES_CK_WRITE : ES_CK_MEASURE, NULL, data, size);
    es_ck_engine(&c);
    es_ck_controls(&c, NULL);
    *used = c.position;
    return es_ck_finish(&c);
}

int DLLEXPORT es_test_clocks_restore(const void *data, size_t size, int fail_at)
{
    EsCkCursor c;
    EsCkControlsStage stage = {0};
    unsigned char *owned;
    int error;
    if (!data || size > 16*1024*1024) return ES_CK_ARGUMENT;
    owned = malloc(size ? size : 1);
    if (!owned) return ES_CK_MEMORY;
    memcpy(owned, data, size);
    es_ck_init(&c, ES_CK_VALIDATE, owned, NULL, size);
    es_ck_engine(&c);
    es_ck_controls(&c, &stage);
    error = es_ck_finish(&c);
    if (!error) error = es_test_prepare_controls(&stage, fail_at);
    if (!error)
    {
        es_ck_init(&c, ES_CK_APPLY, owned, NULL, size);
        es_ck_engine(&c);
        es_ck_controls(&c, &stage);
        error = es_ck_finish(&c);
    }
    es_ck_controls_discard(&stage);
    free(owned);
    return error;
}

void DLLEXPORT es_test_clocks_poison(void)
{
    ReportTime = 123.0;
    OldRunoffTime = NewRunoffTime = 0;
    OldRoutingTime = NewRoutingTime = ElapsedTime = 0;
    RouteStep = .123; CourantFactor = .123;
    TotalStepCount = ReportStepCount = NonConvergeCount = 0;
    Warnings = 0;
    es_test_poison_routing();
    es_test_controls_poison();
}

int DLLEXPORT es_test_clocks_boundary(int fault)
{
    int saved_error = ErrorCode, saved_exception = ExceptionCount, result;
    double saved_duration = RoutingDuration;
    if (fault == 1) ErrorCode = ERR_TIMESTEP;
    if (fault == 2) ExceptionCount = 1;
    if (fault == 3) RoutingDuration = TotalDuration / 2;
    result = es_ck_engine_boundary();
    ErrorCode = saved_error;
    ExceptionCount = saved_exception;
    RoutingDuration = saved_duration;
    return result;
}
