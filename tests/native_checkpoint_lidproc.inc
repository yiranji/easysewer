/* Test-only scratch poisoning between completed outflow/save-results pairs. */
#include "swmm5.h"

void DLLEXPORT es_test_lidproc_poison(void)
{
    int i;
    theLidUnit = NULL; theLidProc = NULL;
    Tstep = EvapRate = MaxNativeInfil = NAN;
    SurfaceInflow = SurfaceInfil = SurfaceEvap = SurfaceOutflow = SurfaceVolume = NAN;
    PaveEvap = PavePerc = PaveVolume = NAN;
    SoilEvap = SoilPerc = SoilVolume = NAN;
    StorageInflow = StorageExfil = StorageEvap = StorageDrain = StorageVolume = NAN;
    for (i = 0; i < MAX_LAYERS; i++) Xold[i] = NAN;
}
