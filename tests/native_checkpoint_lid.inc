/* Test-only access to LID owners, appended after the production state block. */
#include "swmm5.h"

void DLLEXPORT es_test_lid_change(int group, int poison)
{
    int i, j;
    double value = poison ? NAN : 0.0;
    TLidList *item;
    TLidUnit *u;
    TGrnAmpt *a;
    TWaterBalance *w;
    if (group == 8)
    { EvapRate = NativeInfil = MaxNativeInfil = NAN; return; }
    for (j = 0; j < GroupCount; j++) if (LidGroups[j])
    {
        if (!group || group == 6)
            LidGroups[j]->flowToPerv = LidGroups[j]->oldDrainFlow = LidGroups[j]->newDrainFlow = value;
        for (item = LidGroups[j]->lidList; item; item = item->nextLidUnit)
        {
            u = item->lidUnit; a = &u->soilInfil; w = &u->waterBalance;
            if (group == 10) u->botWidth = NAN;
            if (!group || group == 1)
            {
                u->surfaceDepth = u->paveDepth = u->soilMoisture = u->storageDepth = value;
                for (i = 0; i < MAX_LAYERS; i++) u->oldFluxRates[i] = value;
            }
            if (!group || group == 2)
                u->dryTime = u->oldDrainFlow = u->newDrainFlow = value;
            if (!group || group == 3) u->volTreated = u->nextRegenDay = value;
            if ((!group || group == 4) && a->Ks > 0.0)
            { a->IMD = a->F = a->Fu = a->T = value; a->Sat = poison ? 7 : 0; }
            if (!group || group == 5)
                w->inflow = w->evap = w->infil = w->surfFlow = w->drainFlow = w->initVol = w->finalVol = value;
            if ((!group || group == 7) && u->rptFile)
            {
                u->rptFile->wasDry = poison ? -1 : 1;
                memset(u->rptFile->results, poison ? 0xa5 : 0, sizeof(u->rptFile->results));
            }
            if (group == 9 && a->Ks == 0.0)
            {
                a->S = a->IMDmax = a->Lu = a->IMD = a->F = a->Fu = a->T = NAN;
                a->Sat = 7;
            }
        }
    }
}

int DLLEXPORT es_test_lid_observe(int index, double *v, char *row, int *wasDry)
{
    int i, j, n = 0, position = 0;
    TLidList *item;
    TLidUnit *u;
    TWaterBalance *w;
    TGrnAmpt *a;
    for (j = 0; j < GroupCount; j++) if (LidGroups[j])
        for (item = LidGroups[j]->lidList; item; item = item->nextLidUnit)
        {
            if (position++ != index) continue;
            u = item->lidUnit; w = &u->waterBalance; a = &u->soilInfil;
            v[n++] = LidGroups[j]->flowToPerv;
            v[n++] = LidGroups[j]->oldDrainFlow; v[n++] = LidGroups[j]->newDrainFlow;
            v[n++] = u->surfaceDepth; v[n++] = u->paveDepth;
            v[n++] = u->soilMoisture; v[n++] = u->storageDepth;
            for (i = 0; i < MAX_LAYERS; i++) v[n++] = u->oldFluxRates[i];
            v[n++] = u->dryTime; v[n++] = u->oldDrainFlow; v[n++] = u->newDrainFlow;
            v[n++] = u->volTreated; v[n++] = u->nextRegenDay;
            v[n++] = w->inflow; v[n++] = w->evap; v[n++] = w->infil;
            v[n++] = w->surfFlow; v[n++] = w->drainFlow; v[n++] = w->initVol; v[n++] = w->finalVol;
            v[n++] = a->Ks > 0.0 ? a->IMD : 0.0; v[n++] = a->Ks > 0.0 ? a->F : 0.0;
            v[n++] = a->Ks > 0.0 ? a->Fu : 0.0; v[n++] = a->Ks > 0.0 ? a->T : 0.0;
            v[n++] = a->Ks > 0.0 ? a->Sat : 0;
            row[0] = '\0'; *wasDry = -1;
            if (u->rptFile)
            {
                memcpy(row, u->rptFile->results, 255); row[255] = '\0';
                *wasDry = u->rptFile->wasDry;
            }
            return n;
        }
    return 0;
}

void DLLEXPORT es_test_lid_report_guard(int flag)
{
    /* The test calls this only at an otherwise clean public boundary. */
    LidReportFailed = flag == 1;
    LidReportOwnError = flag == 2;
    LidDirtyReports = flag == 3 ? (TLidRptFile *)1 : NULL;
}
