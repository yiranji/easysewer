/* Experimental diagnostics, never a production capability.
   Compile this translation unit IN PLACE OF the upstream infil.c.
   All infiltration calculations still call the included native functions. */
#include "infil.c"
#include "include/swmm5.h"

/* Local Horton object only. The caller supplies native ft/sec, sec, ft units.
   parameters = f0, fmin, decay, regen, Fmax; state = tp, Fe.
   Temporarily install the two native context factors and restore them before
   returning. Diagnostic calls must not run concurrently with a simulation. */
int DLLEXPORT easysewer_horton_trial(int method, const double* parameters,
    double* state, double dt, double rain, double depth, double factor,
    double recovery, double* flux)
{
    int i;
    double savedFactor, savedRecovery;
    THorton soil = {0};
    if (!parameters || !state || !flux || (method != HORTON && method != MOD_HORTON)) return 0;
    for (i = 0; i < 5; i++) if (!isfinite(parameters[i]) || parameters[i] < 0.0) return 0;
    for (i = 0; i < 2; i++) if (!isfinite(state[i]) || state[i] < 0.0) return 0;
    if (parameters[0] < parameters[1] || !isfinite(dt) || dt <= 0.0 ||
        !isfinite(rain) || rain < 0.0 || !isfinite(depth) || depth < 0.0 ||
        !isfinite(factor) || factor < 0.0 || !isfinite(recovery) || recovery < 0.0) return 0;
    soil.f0 = parameters[0]; soil.fmin = parameters[1];
    soil.decay = parameters[2]; soil.regen = parameters[3]; soil.Fmax = parameters[4];
    horton_setState(&soil, state);
    savedFactor = InfilFactor; savedRecovery = Evap.recoveryFactor;
    InfilFactor = factor; Evap.recoveryFactor = recovery;
    *flux = method == MOD_HORTON ? modHorton_getInfil(&soil, dt, rain, depth) :
        horton_getInfil(&soil, dt, rain, depth);
    horton_getState(&soil, state);
    InfilFactor = savedFactor; Evap.recoveryFactor = savedRecovery;
    return 1;
}

/* Read-only parameters and state from a running project: runoff clock seconds,
   f0, fmin, decay, regen, Fmax, tp, Fe (all remaining values in native units). */
int DLLEXPORT easysewer_horton_project(int index, double* values, int count)
{
    THorton* soil;
    if (!values || count < 8 || !Subcatch || !Infil || index < 0 || index >= Nobjects[SUBCATCH]) return 0;
    if (Subcatch[index].infilModel != HORTON && Subcatch[index].infilModel != MOD_HORTON) return 0;
    soil = &Infil[index].horton;
    values[0] = NewRunoffTime / 1000.0;
    values[1] = soil->f0; values[2] = soil->fmin; values[3] = soil->decay;
    values[4] = soil->regen; values[5] = soil->Fmax; values[6] = soil->tp; values[7] = soil->Fe;
    return 1;
}
