/* Appended to flowrout.c only in tests. No pointer or geometry mutation. */
#include "swmm5.h"

void DLLEXPORT es_test_network_poison(int group)
{
    int i, p;
    for (i = 0; i < Nobjects[NODE]; i++)
    {
        TNode *n = &Node[i];
        if (group == 0 || group == 1)
        {
            n->updated = 0;
            n->inflow = n->outflow = n->losses = n->oldVolume = n->newVolume = 0;
            n->overflow = n->oldDepth = n->newDepth = n->oldLatFlow = n->newLatFlow = 0;
            n->oldFlowInflow = n->oldNetInflow = n->qualInflow = n->apiExtInflow = 0;
#if ES_CK_CUSTOM
            n->exflooding = n->pondingBaseVolume = n->pondingBaseDepth = 0;
            n->pondingBaseOverflow = n->pondingRemovedVolume = 0;
#endif
        }
        if (group == 0 || group == 2)
            for (p = 0; p < Nobjects[POLLUT]; p++) n->oldQual[p] = n->newQual[p] = 0;
        if (n->type == STORAGE)
        {
            TStorage *s = &Storage[n->subIndex];
            if (group == 0 || group == 3) s->hrt = s->evapLoss = s->exfilLoss = 0;
            if ((group == 0 || group == 4) && s->exfil)
            {
                grnampt_initState(s->exfil->btmExfil);
                grnampt_initState(s->exfil->bankExfil);
            }
        }
        if (n->type == OUTFALL && (group == 0 || group == 5))
        {
            TOutfall *o = &Outfall[n->subIndex];
            o->type = FREE_OUTFALL; o->fixedStage = 0;
            if (o->routeTo >= 0)
            {
                o->vRouted = 0;
                for (p = 0; p < Nobjects[POLLUT]; p++) o->wRouted[p] = 0;
            }
        }
    }
    for (i = 0; i < Nobjects[LINK]; i++)
    {
        TLink *l = &Link[i];
        if ((group == 0 || group == 9) && l->xsect.type == FILLED_CIRCULAR)
            l->xsect.yFull = l->xsect.aFull = .123;
        if (group == 0 || group == 6)
        {
            l->oldFlow = l->newFlow = l->oldDepth = l->newDepth = 0;
            l->oldVolume = l->newVolume = l->surfArea1 = l->surfArea2 = 0;
            l->setting = l->targetSetting = l->timeLastSet = l->froude = l->dqdh = 0;
            l->flowClass = DRY; l->bypassed = l->normalFlow = l->inletControl = 0;
        }
        if (group == 0 || group == 2)
            for (p = 0; p < Nobjects[POLLUT]; p++)
                l->oldQual[p] = l->newQual[p] = l->totalLoad[p] = 0;
        if ((group == 0 || group == 7) && l->type == CONDUIT)
        {
            TConduit *v = &Conduit[l->subIndex];
            v->a1 = v->a2 = v->q1 = v->q2 = v->q1Old = v->q2Old = 0;
            v->evapLossRate = v->seepLossRate = 0;
            v->capacityLimited = v->fullState = 0;
        }
        if ((group == 0 || group == 8) && l->type == ORIFICE)
        {
            TOrifice *v = &Orifice[l->subIndex];
            v->cOrif = v->hCrit = v->cWeir = v->surfArea = 0;
        }
        if ((group == 0 || group == 8) && l->type == WEIR)
            Weir[l->subIndex].cSurcharge = Weir[l->subIndex].surfArea = 0;
    }
#if ES_CK_CUSTOM
    if (group == 0 || group == 2)
        for (p = 0; p < Nobjects[POLLUT]; p++) Pollut[p].pondingRemovedMass = 0;
#endif
}

/* State observations to prove exfiltration and return-load fixtures advance. */
double DLLEXPORT es_test_network_observe(int group, int i)
{
    if (group == 0 && i >= 0 && i < Nnodes[STORAGE]) return Storage[i].hrt;
    if (group == 1 && i >= 0 && i < Nnodes[STORAGE] && Storage[i].exfil)
        return Storage[i].exfil->btmExfil->F;
    if (group == 2 && i >= 0 && i < Nnodes[STORAGE] && Storage[i].exfil)
        return Storage[i].exfil->bankExfil->F;
    if (group == 3 && i >= 0 && i < Nnodes[OUTFALL]) return Outfall[i].vRouted;
    if (group == 4 && i >= 0 && i < Nobjects[LINK] && Nobjects[POLLUT]) return Link[i].totalLoad[0];
    if (group == 5 && i >= 0 && i < Nnodes[STORAGE] && Storage[i].exfil)
        return Storage[i].exfil->btmArea;
    return 0.0;
}
