/* Test-only: treatment globals are overwritten before each evaluation. */
#include "swmm5.h"
void DLLEXPORT es_test_treatment_poison(void)
{
    int p;
    ErrCode = 123; J = -123;
    Dt = Q = V = -9876.0;
    for (p = 0; p < Nobjects[POLLUT]; p++)
    {
        if (R) R[p] = 77.0;
        if (Cin) Cin[p] = -88.0;
    }
}
