/* Test hooks only; appended to snow.c in separately instrumented candidates. */
#include "swmm5.h"

void DLLEXPORT es_test_snow_immediate_poison(void)
{
    int i, j;
    for (j = 0; j < Nobjects[SUBCATCH]; j++)
        if (Subcatch[j].snowpack)
            for (i = 0; i < 3; i++) Subcatch[j].snowpack->imelt[i] = NAN;
}

int DLLEXPORT es_test_snow_reinitialize(int j, double *values)
{
    int i;
    double depth;
    TSnowpack saved, *p;
    if (j < 0 || j >= Nobjects[SUBCATCH] || !Subcatch[j].snowpack) return -1;
    p = Subcatch[j].snowpack; saved = *p; depth = Subcatch[j].newSnowDepth;
    /* Simulate arbitrary freshly allocated memory in precisely the slots
       omitted by the original initializer; then invoke the real initializer. */
    for (i = 0; i < 3; i++) p->imelt[i] = p->sba[i] = p->sbws[i] = NAN;
    snow_initSnowpack(j);
    for (i = 0; i < 3; i++)
    {
        values[i] = p->imelt[i];
        values[3+i] = p->sba[i];
        values[6+i] = p->sbws[i];
    }
    *p = saved; Subcatch[j].newSnowDepth = depth;
    return 9;
}

void DLLEXPORT es_test_snow_change(int group, int poison)
{
    int i, j;
    double value = poison ? NAN : 0.0;
    TSnowpack *p;
    for (j = 0; j < Nobjects[SUBCATCH]; j++)
    {
        p = Subcatch[j].snowpack;
        if (!p) continue;
        for (i = 0; i < 3; i++)
        {
            if (group == 0 || group == 1) p->wsnow[i] = p->fw[i] = value;
            if (group == 0 || group == 2)
            { p->coldc[i] = value; p->ati[i] = poison ? NAN : 32.0; }
            if (group == 0 || group == 3)
            { p->sba[i] = p->sbws[i] = value; p->awe[i] = poison ? NAN : 1.0; }
        }
    }
    if (group == 0 || group == 4)
        for (j = 0; j < Nobjects[SNOWMELT]; j++)
            for (i = 0; i < 3; i++) Snowmelt[j].dhm[i] = value;
}

int DLLEXPORT es_test_snow_observe(int j, double *values)
{
    int i, k;
    TSnowpack *p;
    if (j < 0 || j >= Nobjects[SUBCATCH] || !Subcatch[j].snowpack) return 0;
    p = Subcatch[j].snowpack; k = p->snowmeltIndex;
    for (i = 0; i < 3; i++)
    {
        values[8*i] = p->wsnow[i]; values[8*i+1] = p->fw[i];
        values[8*i+2] = p->coldc[i]; values[8*i+3] = p->ati[i];
        values[8*i+4] = p->sba[i]; values[8*i+5] = p->awe[i];
        values[8*i+6] = p->sbws[i]; values[8*i+7] = Snowmelt[k].dhm[i];
    }
    return 24;
}
