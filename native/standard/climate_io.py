"""Shared climate parser, stream lifecycle and calendar advancement patch.

Apply to both pinned solver trees after the solver OUT patch. This recipe is
qualified independently before either distributed binary is replaced.
"""

from pathlib import Path
from hotstart import once
from rainfall import replace_function


def patch_climate(contents):
    name = 'src/solver/climate.c'
    text = contents[name].decode('utf-8')
    text = once(text, '#include <stdlib.h>', '#include <limits.h>\n#include <stdlib.h>')
    for declaration in (
        'static void readFileLine(int *year, int *month);',
        'static void readUserFileLine(int *year, int *month);',
        'static void readTD3200FileLine(int *year, int *month);',
        'static void readDLY0204FileLine(int *year, int *month);',
        'static void parseUserFileLine(void);',
        'static void parseTD3200FileLine(void);',
        'static void parseDLY0204FileLine(void);',
        'static void setTD3200FileValues(int param);',
        'static int  isGhcndFormat(char* line);',
        'static void readGhcndFileLine(int *year, int *month);',
        'static void parseGhcndFileLine(void);',
    ):
        text = once(text, declaration, '')
    begin = text.index('static int      FileFieldPos[4];')
    end = text.index('static int      FileWindType;', begin)
    text = text[:begin] + text[end:]
    text = replace_function(text, 'void climate_openFile()\n', 'void climate_initState()\n', '')
    text = replace_function(text, 'int  getFileFormat()\n', 'double convertGhcndValue(int var, double v)\n', '')
    fragment = Path(__file__).with_suffix('.c').read_text(encoding='utf-8')
    text = once(text, 'int  climate_readParams(char* tok[], int ntoks)',
                fragment + '\n\nint  climate_readParams(char* tok[], int ntoks)')
    text = once(text, '    LastDay = NO_DATE;', '''    ClLastState = NO_DATE;
    clCloseSeries(&ClTempSeries);
    clCloseSeries(&ClEvapSeries);
    if (ErrorCode) return;
    if (Fclimate.mode == USE_FILE)
    {
        if (Fclimate.file) clStart();
        else climate_openFile();
        if (ErrorCode) return;
    }
    LastDay = NO_DATE;''')
    text = once(text, '    Temp.tmax = MISSING;', '''    if (Temp.dataSource == TSERIES_TEMP && Temp.tSeries >= 0)
    {
        if (!clInitSeries(&ClTempSeries, Temp.tSeries)) return;
        if (!clFirstEntry(&ClTempSeries, &ClTempSeries.x1, &ClTempSeries.y1)) return;
        ClTempSeries.x2 = ClTempSeries.x1;
        ClTempSeries.y2 = ClTempSeries.y1;
        table_getNextEntry(&ClTempSeries, &ClTempSeries.x2, &ClTempSeries.y2);
        if (clSeriesError(&ClTempSeries)) return;
    }
    Temp.tmax = MISSING;''')
    begin = text.index('    // --- initialize variables for time series evaporation')
    end = text.index('    // --- initialize variables for temperature evaporation', begin)
    text = text[:begin] + '''    ClEvapRate = 0.0;
    if (Evap.type == TIMESERIES_EVAP && Evap.tSeries >= 0)
    {
        if (!clInitSeries(&ClEvapSeries, Evap.tSeries)) return;
        NextEvapDate = NO_DATE;
        if (clFirstEntry(&ClEvapSeries, &NextEvapDate, &NextEvapRate))
        {
            // Retain the first entry as the default before the series begins.
            ClEvapRate = NextEvapRate;
            clAdvanceEvap(StartDateTime);
        }
        else NextEvapDate = NO_DATE;
        if (clSeriesError(&ClEvapSeries)) return;
        Evap.rate = ClEvapRate / UCF(EVAPRATE);
    }

''' + text[end:]
    begin = text.index('void climate_setState(DateTime theDate)\n')
    end = text.index('DateTime climate_getNextEvapDate()', begin)
    old = text[begin:end]
    old = once(old, 'void climate_setState(DateTime theDate)', 'static void clApplyState(DateTime theDate)')
    old = once(old, '    if ( Fclimate.mode == USE_FILE ) updateFileValues(theDate);',
               '    if ( Fclimate.mode == USE_FILE ) updateFileValues(theDate);\n    if (ErrorCode) return;')
    old = once(old, '    setEvap(theDate);',
               '    if (Evap.type == TIMESERIES_EVAP) clAdvanceEvap(theDate);\n    if (ErrorCode) return;\n    setEvap(theDate);')
    old = once(old, '    if ( Temp.dataSource != NO_TEMP ) setTemp(theDate);',
               '    if ( Temp.dataSource != NO_TEMP ) setTemp(theDate);\n    if (ErrorCode) return;')
    text = text[:begin] + old + '''void climate_setState(DateTime theDate)
{
    DateTime day, previous;
    if (ErrorCode) return;
    if (!isfinite(theDate) || theDate < StartDateTime ||
        theDate < datetime_encodeDate(1, 1, 1) ||
        theDate >= datetime_encodeDate(9999, 12, 31) + 1.0 ||
        (ClLastState != NO_DATE && theDate < ClLastState))
    {
        clFail(ERR_TIMESTEP);
        return;
    }
    previous = ClLastState == NO_DATE ? StartDateTime : ClLastState;
    if (ClLastState == NO_DATE && floor(theDate) > floor(previous))
        clApplyState(previous);
    /* Advance every missing day, including missing months and leap days. The
     * temperature moving average and previous maximum need each daily state. */
    for (day = floor(previous) + 1.0; day < floor(theDate) && !ErrorCode; day += 1.0)
        clApplyState(day);
    if (!ErrorCode) clApplyState(theDate);
    if (!ErrorCode) ClLastState = theDate;
}

''' + text[end:]
    # Exhausted series use NO_DATE. Do not invent a future event and retain an
    # outdated rate, or apply monthly adjustments repeatedly to adjusted state.
    begin = text.index('void setNextEvapDate(DateTime theDate)\n')
    end = text.index('void updateFileValues(DateTime theDate)\n', begin)
    region = text[begin:end]
    region = once(region, '    int    yr, mon, day, k;\n    double d, e;',
                  '    int    yr, mon, day;\n    if (Evap.type == TIMESERIES_EVAP) { clAdvanceEvap(theDate); return; }')
    first = region.index('      // --- for time series evaporation,')
    last = region.index('      // --- for climate file daily evaporation,', first)
    region = region[:first] + region[last:]
    text = text[:begin] + region + text[end:]
    text = once(text, '''        if ( theDate >= NextEvapDate )
            Evap.rate = NextEvapRate / UCF(EVAPRATE);''',
                '        Evap.rate = ClEvapRate / UCF(EVAPRATE);')
    text = once(text, '            Temp.ta = table_tseriesLookup(&Tseries[k], theDate, TRUE);',
                '            Temp.ta = table_tseriesLookup(&ClTempSeries, theDate, TRUE);\n'
                '            if (clSeriesError(&ClTempSeries)) return;')
    text = once(text, '    if ( deltaDays > FileElapsedDays )', '    while ( deltaDays > FileElapsedDays )')
    text = once(text, '            readFileValues();\n            FileDay = 1;', '''            if (!clDate(FileYear, FileMonth, 1)) { clFail(ERR_CLIMATE_FILE_READ); return; }
            readFileValues();
            if (ErrorCode) return;
            FileDay = 1;''')
    contents[name] = text.encode('utf-8')

    name = 'src/solver/funcs.h'
    contents[name] += b'\nint climate_closeFile(void);\n'
    name = 'src/solver/swmm5.c'
    text = contents[name].decode('utf-8')
    text = once(text, '        project_init();', '        project_init();\n        if (ErrorCode) return ErrorCode;')
    text = once(text, '''    if (Fclimate.file)
    {
        fclose(Fclimate.file);
        Fclimate.file = NULL;
    }''', '''    code = climate_closeFile();
    if (!closeCode) closeCode = code;''')
    anchor = 'else climate_setState(getDateTime(NewRoutingTime));'
    count = text.count(anchor)
    if count not in (1, 2):
        raise ValueError('Unexpected climate routing call count')
    text = text.replace(anchor, anchor + '\n        if (ErrorCode) return;')
    text += '\nint DLLEXPORT swmm_getEasySewerClimateIO(void)\n{\n    return 1;\n}\n'
    contents[name] = text.encode('utf-8')

    name = 'src/solver/runoff.c'
    text = contents[name].decode('utf-8')
    text = once(text, '    if ( Fclimate.file ) fclose(Fclimate.file);\n    Fclimate.file = NULL;',
                '    climate_closeFile();')
    text = once(text, '    climate_setState(currentDate);', '    climate_setState(currentDate);\n    if (ErrorCode) return;')
    text = once(text, '    long maxStep = DryStep;', '    long maxStep = DryStep;\n    DateTime next;')
    text = once(text, '    timeStep = datetime_timeDiff(climate_getNextEvapDate(), currentDate);',
                '    next = climate_getNextEvapDate();\n    timeStep = next == NO_DATE ? 0 : datetime_timeDiff(next, currentDate);')
    text = once(text, '''        timeStep = datetime_timeDiff(gage_getNextRainDate(j, currentDate),
                   currentDate);''', '''        next = gage_getNextRainDate(j, currentDate);
        timeStep = next == NO_DATE ? 0 : datetime_timeDiff(next, currentDate);''')
    contents[name] = text.encode('utf-8')

    name = 'src/solver/datetime.c'
    text = contents[name].decode('utf-8')
    text = '#include <limits.h>\n' + text
    text = once(text, '    long   s1, s2, secs;', '''    long   s1, s2;
    double secs;
    /* No event has no interval. Keep the historic long ABI, with defined
     * saturation for valid intervals wider than the platform's long. */
    if (!isfinite(date1) || !isfinite(date2) || date1 == NO_DATE || date2 == NO_DATE) return 0;''')
    text = once(text, '    secs = (int)(floor((d1 - d2)*SecsPerDay + 0.5));',
                '    secs = floor((d1 - d2)*SecsPerDay + 0.5);')
    text = once(text, '    return secs;', '''    if (secs >= (double)LONG_MAX) return LONG_MAX;
    if (secs <= (double)LONG_MIN) return LONG_MIN;
    return (long)secs;''')
    contents[name] = text.encode('utf-8')
