/* Test-only destructive probes, never shipped as solver API. */
#include "swmm5.h"
void DLLEXPORT es_test_output_change(int group, int poison)
{
    int i,j;
    REAL4 value = poison ? NAN : 0.0f;
    if (!group || group == 1) Nperiods = poison ? -1 : 0;
    if (!group || group == 2) Nsteps = poison ? -1 : 0;
    if (RptFlags.averages && (!group || group == 3))
    {
        for (i=0; i<NumNodes; i++) for (j=0; j<NumNodeVars; j++) AvgNodeResults[i].xAvg[j] = value;
        for (i=0; i<NumLinks; i++) for (j=0; j<NumLinkVars; j++) AvgLinkResults[i].xAvg[j] = value;
    }
    if (group == 4)
    {
        for (i=0; i<NumSubcatchVars; i++) SubcatchResults[i] = value;
        for (i=0; i<NumNodeVars; i++) NodeResults[i] = value;
        for (i=0; i<NumLinkVars; i++) LinkResults[i] = value;
        for (i=0; i<MAX_SYS_RESULTS; i++) SysResults[i] = value;
    }
}
int DLLEXPORT es_test_output_periods(void) { return (int)Nperiods; }
void DLLEXPORT es_test_output_guard(int group, int value)
{
    if (group == 0) OutReady = value;
    if (group == 1) OutFailed = value;
    if (group == 2) OutEnded = value;
}
