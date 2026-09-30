/* Test-only destructive perturbation of each persistent rain owner group. */
#include "swmm5.h"
void DLLEXPORT es_test_gage_poison(int group)
{
    int j, k;
    for (j = 0; j < Nobjects[GAGE]; j++)
    {
        TGage *p = &Gage[j];
        if (group == 0 || group == 1)
        {
            p->startDate = p->endDate = p->nextDate = NO_DATE;
            p->rainfall = p->nextRainfall = p->reportRainfall = 0.0;
        }
        if (group == 0 || group == 2) p->rainAccum = 0.0;
        if (group == 0 || group == 3)
        {
            p->pastInterval = 0;
            for (k = 0; k <= MAXPASTRAIN; k++) p->pastRain[k] = 0.0;
        }
        if (group == 0 || group == 4) p->apiRainfall = MISSING;
        if ((group == 0 || group == 5) && p->dataSource == RAIN_FILE)
            p->currentFilePos = p->endFilePos;
        if (group == 6) p->isCurrent = 12345;
    }
}

double DLLEXPORT es_test_gage_observe(int j, int field)
{
    TGage *p;
    if (j < 0 || j >= Nobjects[GAGE]) return -999.0;
    p = &Gage[j];
    if (field >= 0 && field <= MAXPASTRAIN) return p->pastRain[field];
    switch (field)
    {
    case 49: return p->pastInterval;
    case 50: return p->rainAccum;
    case 51: return p->rainfall;
    case 52: return p->nextRainfall;
    case 53: return p->apiRainfall;
    case 54: return p->reportRainfall;
    case 55: return p->currentFilePos;
    default: return -999.0;
    }
}
