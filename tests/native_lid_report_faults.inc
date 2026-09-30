/* Test-only failure injection; never included in formal build recipes. */
#include <errno.h>
#include <stdarg.h>
#include "include/swmm5.h"
static int EsLidFaultKind, EsLidFaultHit, EsLidFaultFired, EsLidCalls[9];
static int EsLidConfigured, EsLidEnvironment;
static int es_lid_fail(int kind)
{
    if (!EsLidConfigured) {
        const char *which = getenv("ES_LID_IO_FAULT");
        const char *hit = getenv("ES_LID_IO_HIT");
        EsLidConfigured = 1;
        EsLidEnvironment = (which != NULL);
        EsLidFaultKind = which ? atoi(which) : 0;
        EsLidFaultHit = hit ? atoi(hit) : 1;
    }
    EsLidCalls[kind]++;
    if (kind != EsLidFaultKind || EsLidCalls[kind] != EsLidFaultHit) return 0;
    EsLidFaultFired++;
    if (EsLidEnvironment) {
        fprintf(stderr, "lid-fault kind=%d hit=%d fired=1\n", kind, EsLidFaultHit);
        if (getenv("ES_LID_IO_PRIMARY")) {
            ErrorCode = ERR_MEMORY;
            snprintf(ErrorMsg, MAXMSG, "prior error 101");
        }
    }
    errno = kind == 6 || kind == 7 ? ENOMEM : ENOSPC;
    return 1;
}
int es_test_lid_vfprintf(FILE *f, const char *fmt, va_list args)
{ return es_lid_fail(1) ? -1 : vfprintf(f, fmt, args); }
int es_test_lid_fflush(FILE *f)
{ return es_lid_fail(2) ? EOF : fflush(f); }
int es_test_lid_fclose(FILE *f)
{ int result = fclose(f); return es_lid_fail(3) ? EOF : result; }
int es_test_lid_vsnprintf(char *buf, size_t n, const char *fmt, va_list args)
{
    if (es_lid_fail(4)) return -1;
    if (es_lid_fail(5)) return (int)n;
    return vsnprintf(buf, n, fmt, args);
}
void *es_test_lid_calloc(size_t n, size_t size)
{ return es_lid_fail(6) ? NULL : calloc(n, size); }
void *es_test_lid_malloc(size_t n)
{ return es_lid_fail(6) ? NULL : malloc(n); }
void *es_test_lid_model_calloc(size_t n, size_t size)
{ return es_lid_fail(7) ? NULL : calloc(n, size); }
void *es_test_lid_model_malloc(size_t n)
{ return es_lid_fail(7) ? NULL : malloc(n); }
FILE *es_test_lid_fopen(const char *path, const char *mode)
{ return es_lid_fail(8) ? NULL : fopen(path, mode); }
void DLLEXPORT es_test_lid_fault(int kind, int hit)
{
    int i;
    EsLidFaultKind = kind; EsLidFaultHit = hit; EsLidFaultFired = 0;
    EsLidConfigured = 1; EsLidEnvironment = 0;
    for (i = 0; i < 9; i++) EsLidCalls[i] = 0;
}
int DLLEXPORT es_test_lid_fault_fired(void) { return EsLidFaultFired; }
int DLLEXPORT es_test_lid_calls(int kind)
{ return kind >= 1 && kind <= 8 ? EsLidCalls[kind] : -1; }
void DLLEXPORT es_test_lid_prior_error(int code)
{ ErrorCode = code; snprintf(ErrorMsg, MAXMSG, "prior error %d", code); }
void DLLEXPORT es_test_lid_secondary_error(int code)
{ report_writeErrorMsg(code, "secondary"); }
