void es_test_iface_poison(int scratch)
{
    int i, j;
    for (i = 0; i < NumIfaceNodes; i++)
        for (j = 0; j <= NumIfacePolluts; j++)
            if (scratch) IfacePendingValues[i][j] = NAN;
            else OldIfaceValues[i][j] = NewIfaceValues[i][j] = NAN;
    if (!scratch)
    {
        IfaceEof = -1;
        IfaceFrac = OldIfaceDate = NewIfaceDate = IfacePreviousDate = NAN;
    }
}
