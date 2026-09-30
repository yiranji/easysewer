void es_test_rdii_poison(int scratch)
{
    int i;
    if (scratch)
    {
        for (i = 0; i < NumRdiiNodes; i++) RdiiPendingFlow[i] = NAN;
        TotalRainVol = TotalRdiiVol = NAN;
        return;
    }
    RdiiEof = -1; RdiiStartDate = RdiiEndDate = RdiiPreviousDate = NAN;
    for (i = 0; i < NumRdiiNodes; i++) RdiiNodeFlow[i] = NAN;
}
