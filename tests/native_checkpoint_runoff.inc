#include "swmm5.h"

void DLLEXPORT es_test_runoff_poison(int group)
{
    int j;
    size_t k;
    if (group == 0 || group == 1) HasRunoff = FALSE;
    if (group == 0 || group == 2) HasSnow = FALSE;
    if (group == 0 || group == 3) HasWetLids = FALSE;
    if (group == 0 || group == 4) Nsteps = 0;
    if (group == 0 || group == 5) ReplayRainTime = 0.0;
    if (group == 0 || group == 6)
        if (ReplayRawRain) for (j = 0; j < Nobjects[GAGE]; j++) ReplayRawRain[j] = 0.0;
    if (group == 0 || group == 7)
    {
        IsRaining = 123;
        if (OutflowLoad) for (j = 0; j < Nobjects[POLLUT]; j++) OutflowLoad[j] = NAN;
        if (RunoffFrame) for (k = 0; k < RunoffItems; k++) RunoffFrame[k] = NAN;
    }
}

double DLLEXPORT es_test_runoff_observe(int field)
{
    switch (field)
    {
    case 0: return HasRunoff;
    case 1: return HasSnow;
    case 2: return HasWetLids;
    case 3: return Nsteps;
    case 4: return ReplayRainTime;
    default:
        field -= 5;
        return ReplayRawRain && field >= 0 && field < Nobjects[GAGE] ? ReplayRawRain[field] : 0.0;
    }
}
