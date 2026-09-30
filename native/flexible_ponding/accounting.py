"""Apply the reviewed ABI 201 accounting changes to verified base source."""

from pathlib import Path


def patch_accounting(contents):
    def edit(name, old, new, count=1):
        key='src/solver/'+name
        text=contents[key].decode('utf-8')
        if text.count(old)!=count:
            raise ValueError('Unexpected accounting patch anchor: '+name+' '+old[:60])
        contents[key]=text.replace(old,new).encode('utf-8')

    edit('objects.h','   double        exflooding;      // external flooding rate (cfs)',
        '''   double        exflooding;      // external flooding rate (cfs)
   double        pondingBaseVolume, pondingBaseDepth, pondingBaseOverflow;
   double        pondingRemovedVolume; // cumulative discrete removal (ft3)''')
    edit('objects.h','}  TPollut;','   double        pondingRemovedMass; // concentration units times ft3\n}  TPollut;')
    for name in ('FlowTotals','QualTotals[j]'):
        edit('massbal.c',name+'.flooding += Step'+name+'.flooding * tStep * 2;',
            name+'.flooding += Step'+name+'.flooding * tStep;')
    edit('massbal.c','(Node[j].degree == 0 && Node[j].type != STORAGE)',
        '(RouteModel != DW && Node[j].degree == 0 && Node[j].type != STORAGE)')
    edit('massbal.c','sysFlows[SYS_FLOODING] = (f1 * OldStepFlowTotals.flooding +\n                               f * StepFlowTotals.flooding) * UCF(FLOW);',
        'sysFlows[SYS_FLOODING] = (f1 * (OldStepFlowTotals.flooding + OldPondingFlow) +\n                               f * (StepFlowTotals.flooding + PondingFlow)) * UCF(FLOW);')
    edit('massbal.c','totalOutflow = StepFlowTotals.flooding +','totalOutflow = StepFlowTotals.flooding + PondingFlow +')
    edit('massbal.c','void massbal_report()', '''static void massbal_computeErrors(void)
{
    if (Nobjects[SUBCATCH] > 0)
    {
        massbal_getRunoffError();
        if (Nobjects[POLLUT] > 0 && !IgnoreQuality) massbal_getLoadingError();
    }
    if (Nobjects[AQUIFER] > 0 && !IgnoreGwater) massbal_getGwaterError();
    if (Nobjects[NODE] > 0 && !IgnoreRouting)
    {
        massbal_getFlowError();
        if (Nobjects[POLLUT] > 0 && !IgnoreQuality) massbal_getQualError();
    }
}

void massbal_report()''')
    edit('massbal.c','''    double gwArea = 0.0;

    if ( Nobjects[SUBCATCH] > 0 )''','''    double gwArea = 0.0;

    if (RptFlags.disabled) { massbal_computeErrors(); return; }
    if ( Nobjects[SUBCATCH] > 0 )''')
    edit('output.c','(StepFlowTotals.flooding * UCF(FLOW))','((StepFlowTotals.flooding + PondingFlow) * UCF(FLOW))')
    edit('routing.c','''        if (Nobjects[LINK] > 0)
        {
            stats_updateFlowStats(routingStep, getDateTime(NewRoutingTime));''',
        '''        if (PondingMode) ponding_stepStats(trialsCount, inSteadyState);
        else if (Nobjects[LINK] > 0)
        {
            stats_updateFlowStats(routingStep, getDateTime(NewRoutingTime));''')
    edit('routing.c','''    // --- route flow through the drainage network
    if ( Nobjects[LINK] > 0 )''','''    // A disconnected ponded node still stores its lateral inflow.
    if (Nobjects[LINK] > 0 || (PondingMode && routingModel == DW && Nobjects[NODE] > 0))''')
    edit('stats.c','''    // --- allocate memory for node & link stats
    if ( Nobjects[LINK] > 0 )''','''    // --- allocate node statistics even for a network without links
    if ( Nobjects[NODE] > 0 )''')
    edit('stats.c','if ( !NodeStats || !LinkStats )','if ( !NodeStats || (Nobjects[LINK] > 0 && !LinkStats) )')
    edit('stats.c','int    canPond = (AllowPonding && Node[j].pondedArea > 0.0);',
        '''int    canPond = (AllowPonding && Node[j].pondedArea > 0.0);
    double floodFlow = PondingMode && canPond ? Node[j].exflooding : Node[j].overflow;''')
    edit('stats.c','newVolume > Node[j].fullVolume || Node[j].overflow > 0.0',
        'newVolume > Node[j].fullVolume || floodFlow > 0.0')
    edit('stats.c','NodeStats[j].volFlooded += Node[j].overflow * tStep;',
        'NodeStats[j].volFlooded += floodFlow * tStep;')
    edit('stats.c','if ( Node[j].overflow > NodeStats[j].maxOverflow )','if ( floodFlow > NodeStats[j].maxOverflow )')
    edit('stats.c','NodeStats[j].maxOverflow = Node[j].overflow;','NodeStats[j].maxOverflow = floodFlow;')
    edit('node.c','z = Node[j].exflooding * UCF(FLOW);  // Replace NODE_OVERFLOW with exflooding',
        'z = (PondingMode && AllowPonding && Node[j].pondedArea > 0.0 ? Node[j].exflooding : Node[j].overflow) * UCF(FLOW);')

    text=contents['src/solver/swmm5.c'].decode('utf-8')
    start=text.index('void updateExfloodingTotals()')
    end=text.index('void saveResults()',start)
    text=text[:start]+text[end:]
    contents['src/solver/swmm5.c']=text.encode('utf-8')
    edit('swmm5.c','    // --- update external flooding totals before saving results\n'
        '    updateExfloodingTotals();\n    \n','')
    edit('swmm5.c','        stats_open();','        stats_open();\n        ponding_reset();')
    edit('swmm5.c','''        if ( !ErrorCode && RptFlags.disabled == 0 )
        {
            massbal_report();
            stats_report();
        }''','''        if ( !ErrorCode )
        {
            massbal_report();
            if (!RptFlags.disabled) stats_report();
        }''')
    edit('swmm5.c','''    // --- call the internal execRouting function
    execRouting();
    return ErrorCode;''','''    if (NewRoutingTime >= RoutingDuration)
        return (ErrorCode = ERR_API_TIME_PERIOD);
    if (ponding_begin()) return ErrorCode;
    execRouting();
    if (!ErrorCode) ponding_capture();
    return ErrorCode;''')
    edit('swmm5.c','''    // --- call the internal saveResults function if results are to be saved
    if (SaveResultsFlag)''','''    // Finalize even when binary output is disabled. Exactly one commit per route.
    if (ponding_finalize()) return ErrorCode;
    if (SaveResultsFlag)''')
    edit('swmm5.c','''    if ( IsStartedFlag )
    {
        // --- write ending records''','''    if ( IsStartedFlag )
    {
        if (PondingPending && !ErrorCode) ErrorCode = ERR_API_PROPERTY_VALUE;
        // --- write ending records''')
    # A session may use the standard step API or the split ponding API, not mix them.
    edit('swmm5.c','''    *elapsedTime = 0.0;
    if ( ErrorCode )''','''    *elapsedTime = 0.0;
    if (PondingMode) return (ErrorCode = ERR_API_PROPERTY_VALUE);
    if ( ErrorCode )''')
    edit('swmm5.c','objType > swmm_LINK','objType > swmm_POLLUTANT',count=3)
    edit('swmm5.c','case LINK:     idName = Link[index].ID;     break;',
        'case LINK:     idName = Link[index].ID;     break;\n        case POLLUT:   idName = Pollut[index].ID;   break;')
    edit('swmm5.c','''    if (property < 500)
        return getLinkValue(property, index);
    return 0;''','''    if (property < 500)
        return getLinkValue(property, index);
    if (index >= 0 && index < Nobjects[POLLUT])
    {
        if (property == 500) return Pollut[index].units;
        if (property == 501) return Pollut[index].pondingRemovedMass * LperFT3;
    }
    return 0;''')
    api=r'''
double DLLEXPORT swmm_getPondingStep(void)
{
    if (!IsStartedFlag || !PondingPending) return 0.0;
    return (NewRoutingTime - OldRoutingTime) / 1000.0;
}

int DLLEXPORT swmm_getPondingNodeStat(int index, int field, double* value)
{
    if (!IsStartedFlag) return ERR_API_NOT_STARTED;
    return ponding_nodeStat(index, field, value);
}

int DLLEXPORT swmm_getNodePollutant(int index, int pollutant, double* value)
{
    if (!IsStartedFlag) return ERR_API_NOT_STARTED;
    if (!value) return ERR_API_PROPERTY_VALUE;
    if (index < 0 || index >= Nobjects[NODE] || pollutant < 0 || pollutant >= Nobjects[POLLUT])
        return ERR_API_OBJECT_INDEX;
    *value = Node[index].newQual[pollutant];
    return 0;
}
'''
    contents['src/solver/swmm5.c']+=api.encode('utf-8')
    edit('include/swmm5.h','swmm_LINK     = 3,','swmm_LINK     = 3,\n    swmm_POLLUTANT = 4,')
    edit('include/swmm5.h','int    DLLEXPORT swmm_getFlexiblePondingAbi(void);',
        '''int    DLLEXPORT swmm_getFlexiblePondingAbi(void);
double DLLEXPORT swmm_getPondingStep(void);
int    DLLEXPORT swmm_getPondingNodeStat(int index, int field, double* value);
int    DLLEXPORT swmm_getNodePollutant(int index, int pollutant, double* value);''')
    declarations='''
/* easysewer split-routing accounting contract, custom ABI 201 */
extern int PondingMode, PondingPending;
extern double PondingFlow, OldPondingFlow;
void ponding_reset(void);
int ponding_begin(void);
int ponding_capture(void);
void ponding_stepStats(int trials, int steady);
int ponding_finalize(void);
int ponding_nodeStat(int node, int field, double* value);
'''
    contents['src/solver/funcs.h']+=declarations.encode('utf-8')
    contents['src/solver/easysewer_ponding.c']=Path(__file__).with_name('accounting.c').read_bytes().replace(b'\r\n',b'\n')
