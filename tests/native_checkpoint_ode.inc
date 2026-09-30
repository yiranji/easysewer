/* Test-only; arrays are poisoned without changing allocation or capacity. */
#include "swmm5.h"

void DLLEXPORT es_test_ode_poison(void)
{
    int i;
    for (i = 0; i < nmax; i++) y[i] = yscal[i] = yerr[i] = ytemp[i] = dydx[i] = NAN;
    for (i = 0; i < 5*nmax; i++) ak[i] = NAN;
}

int DLLEXPORT es_test_ode_calls(int dimension)
{
    return dimension >= 0 && dimension < 3 ? es_test_ode_counts[dimension] : -1;
}

void DLLEXPORT es_test_ode_reset_counts(void)
{
    int i;
    for (i = 0; i < 3; i++) es_test_ode_counts[i] = 0;
}
