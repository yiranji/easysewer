#include "swmm5.h"

void DLLEXPORT es_test_climate_poison(int group)
{
    int i, j;
    if (group == 0)
    {
        Temp.ta = Temp.tmax = Temp.ea = Wind.ws = 0.0;
        Evap.rate = Evap.recoveryFactor = Snow.season = Snow.removed = 0.0;
        Adjust.rainFactor = Adjust.hydconFactor = 0.0;
        LastDay = ClLastState = NO_DATE;
    }
    if ((group == 0 || group == 1) && Temp.dataSource == FILE_TEMP)
    {
        Tmin = Tmax = Trng = Trng1 = Tave = 0.0;
        if (group == 0) Hrsr = Hrss = Hrday = Dhrdy = Dydif = 0.0;
    }
    if (group == 0 || group == 2)
    { NextEvapDate = NO_DATE; NextEvapRate = ClEvapRate = 0.0; }
    if ((group == 0 || group == 3) && Evap.type == TEMPERATURE_EVAP)
    {
        Tma.count = Tma.front = 0; Tma.tAve = Tma.tRng = 0.0;
        for (i = 0; i < 7; i++) Tma.ta[i] = Tma.tr[i] = NAN;
    }
    if (Fclimate.mode == USE_FILE)
    {
        if (group == 0)
        {
            FileYear = ClRowYear = 2000; FileMonth = FileDay = ClRowMonth = 1;
            FileLastDay = 31; FileElapsedDays = ClEof = ClHaveRow = 0;
            ClLastMonth = 24001;
        }
        if (group == 0 || group == 4)
            for (i = 0; i < 4; i++)
            {
                FileValue[i] = 0.0;
                for (j = 0; j < 32; j++) FileData[i][j] = MISSING;
            }
        if (group == 0 || group == 5)
            for (i = 0; i < 4; i++) for (j = 0; j < 32; j++) ClValues[i][j] = MISSING;
    }
    if (group == 6)
    {
        strcpy(FileLine, "invalid scratch line");
        if (Evap.type == TEMPERATURE_EVAP && Tma.count < 7)
            for (i = Tma.count; i < 7; i++) Tma.ta[i] = Tma.tr[i] = NAN;
    }
}

double DLLEXPORT es_test_climate_observe(int field)
{
    switch (field)
    {
    case 0: return Temp.ta; case 1: return Temp.tmax; case 2: return Temp.ea;
    case 3: return Wind.ws; case 4: return Evap.rate; case 5: return Evap.recoveryFactor;
    case 6: return Snow.season; case 7: return Snow.removed;
    case 8: return Adjust.rainFactor; case 9: return Adjust.hydconFactor;
    case 10: return LastDay; case 11: return NextEvapDate;
    case 12: return NextEvapRate; case 13: return ClEvapRate; case 14: return ClLastState;
    case 15: return Tma.tAve; case 16: return Tma.tRng; case 17: return Tma.count;
    case 18: return Tma.front; case 19: return FileYear; case 20: return FileMonth;
    case 21: return FileDay; case 22: return FileElapsedDays;
    case 23: return FileValue[TMIN]; case 24: return FileValue[TMAX];
    case 25: return FileValue[EVAP]; case 26: return FileValue[WIND];
    default: return 0.0;
    }
}
