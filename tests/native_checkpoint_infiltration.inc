/* Test-only instrumentation appended to infil.c. */
#include "swmm5.h"
void DLLEXPORT es_test_infiltration_poison(int scratch)
{
    int j;
    if (scratch) { Fumax = -999; InfilFactor = -888; return; }
    for (j = 0; j < Nobjects[SUBCATCH]; j++)
        if (Subcatch[j].infil == j) infil_initState(j);
}

double DLLEXPORT es_test_infiltration_observe(int j, int k)
{
    double values[6] = {0};
    if (j < 0 || j >= Nobjects[SUBCATCH] || k < 0 || k >= 6) return -1;
    if (Subcatch[j].infil == j) infil_getState(j, values);
    return values[k];
}
