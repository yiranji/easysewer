/* Test-only access in dynwave.c; never included in installed libraries. */
#include "swmm5.h"
void DLLEXPORT es_test_dynwave_poison(int field)
{
    int i;
    if (field == 0 || field == 1) VariableStep = 0.0;
    if (Xnode) for (i = 0; i < Nobjects[NODE]; i++)
    {
        if (field == 0 || field == 2) Xnode[i].oldSurfArea = 0.0;
        if (field == 0 || field == 3) Xnode[i].dYdT = 0.0;
        if (field == 4)
        {
            Xnode[i].converged = 99;
            Xnode[i].newSurfArea = -12345.0;
            Xnode[i].sumdqdh = -12345.0;
        }
    }
    if (field == 4) { Steps = -12345; Omega = -12345.0; }
}

/* Read-only branch evidence; never call getVariableStep (updates statistics).
 * 1: near-crown surcharge uses remembered area; 2: node rate limits step. */
int DLLEXPORT es_test_dynwave_branches(void)
{
    int i, flags = 0, unused = -1;
    if (RouteModel != DW || !Xnode) return 0;
    for (i = 0; i < Nobjects[NODE]; i++)
    {
        double crown = Node[i].crownElev - Node[i].invertElev;
        if (Node[i].type != OUTFALL && SurchargeMethod == EXTRAN && crown > 0 &&
            Node[i].newDepth > crown && Node[i].newDepth < 1.25 * crown &&
            !(AllowPonding && Node[i].pondedArea > 0 && Node[i].newDepth > Node[i].fullDepth))
            flags |= 1;
    }
    if (getNodeStep(RouteStep, &unused) < RouteStep) flags |= 2;
    return flags;
}
