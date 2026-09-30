#include "swmm5.h"
void DLLEXPORT es_test_ponding_change(int group, int poison)
{
    if (!group || group == 1) PondingMode = poison ? -1 : FALSE;
    if (!group || group == 2) PondingFlow = poison ? NAN : 0.0;
    if (!group || group == 3) OldPondingFlow = poison ? NAN : 0.0;
    if (group == 4)
    {
        PondingStep = NAN; CollectStats = -1; StepTrials = -1; StepSteady = -1;
    }
}
