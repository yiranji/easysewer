"""Checked complete RUNOFF frames and their native file lifecycle."""

from pathlib import Path
from hotstart import once
from rainfall import replace_function


def patch_runoff(contents):
    name = 'src/solver/runoff.c'
    text = contents[name].decode('utf-8')
    text = once(text, '#include <stdlib.h>',
        '#include <stdlib.h>\n#include <stdint.h>\n#include <limits.h>\n#include <math.h>\n#include <sys/types.h>')
    text = replace_function(text, 'void runoff_initFile(void)\n',
                            'void runoff_getOutfallRunon(double tStep)\n', '')
    fragment = Path(__file__).with_suffix('.c').read_text(encoding='utf-8')
    fragment = Path(__file__).with_name('runoff_rain.c').read_text(encoding='utf-8') + '\n' + fragment
    text = once(text, 'int runoff_open()', fragment + '\n\nint runoff_open()')
    text = once(text, '    // --- see if a runoff interface file should be opened',
        '    if (ErrorCode) return ErrorCode;\n\n'
        '    // --- see if a runoff interface file should be opened')
    text = once(text, 'fopen(Frunoff.name, "r+b")', 'fopen(Frunoff.name, "rb")')
    text = once(text, '    return ErrorCode;\n}',
        '    runoffRainInit();\n    if (ErrorCode) runoffCloseFile();\n    return ErrorCode;\n}')
    text = once(text, '''    if ( Frunoff.file )
    {
        // --- write to file number of time steps simulated
        if ( Frunoff.mode == SAVE_FILE )
        {
            fseek(Frunoff.file, MaxStepsPos, SEEK_SET);
            fwrite(&Nsteps, sizeof(int), 1, Frunoff.file);
        }
        fclose(Frunoff.file);
    }''', '    runoffCloseFile();')
    text = once(text, '    Nsteps++;\n    if ( Frunoff.mode == SAVE_FILE )', '''    if (Nsteps == INT_MAX)
    {
        runoffFail(Frunoff.mode == SAVE_FILE ? ERR_RUNOFF_FILE_WRITE : ERR_TIMESTEP);
        return;
    }
    Nsteps++;
    if ( Frunoff.mode == SAVE_FILE )''')
    contents[name] = text.encode('utf-8')
    # USE has its own consumer rain clock. Do not advance gages to cache frame
    # boundaries before control/report times have reached them.
    branch = '''    // --- read runoff results from interface file if applicable
    if ( Frunoff.mode == USE_FILE )
    {
        runoff_readFromFile();
        return;
    }

'''
    text = once(text, branch, '')
    text = once(text, '    // --- update current rainfall at each raingage',
                branch + '    // --- update current rainfall at each raingage')
    contents[name] = text.encode('utf-8')
    name = 'src/solver/swmm5.c'; text = contents[name].decode('utf-8')
    text = once(text, '        if ( DoRunoff ) runoff_open();',
        '        if ( DoRunoff ) runoff_open();\n        if (ErrorCode) return ErrorCode;')
    text += '\nint DLLEXPORT swmm_getEasySewerRunoffPhysics(void)\n{\n    return 1;\n}\n'
    text = once(text, '        // --- route flows & pollutants through drainage system',
        '        runoff_updateReplayRain(NewRoutingTime);\n        if (ErrorCode) return;\n\n'
        '        // --- route flows & pollutants through drainage system')
    text = once(text, '        // --- update elapsed time (days)',
        '        runoff_updateReplayRain(NewRoutingTime);\n\n        // --- update elapsed time (days)')
    text += '\nint DLLEXPORT swmm_getEasySewerRunoffRainClock(void)\n{\n    return 1;\n}\n'
    contents[name] = text.encode('utf-8')
    name = 'src/solver/funcs.h'; text = contents[name].decode('utf-8')
    text = once(text, 'void    runoff_execute(void);',
        'void    runoff_execute(void);\nvoid    runoff_updateReplayRain(double time);')
    text = once(text, 'void     gage_setState(int gage, DateTime aDate);',
        'void     gage_setState(int gage, DateTime aDate);\nvoid     gage_setReplayState(int gage, DateTime aDate);')
    contents[name] = text.encode('utf-8')
    name = 'src/solver/gage.c'; text = contents[name].decode('utf-8')
    text = once(text, 'void gage_setState(int j, DateTime t)', '''static void gage_setStateAt(int j, DateTime t, int advanceTolerance);

void gage_setState(int j, DateTime t)
{
    gage_setStateAt(j, t, TRUE);
}

void gage_setReplayState(int j, DateTime t)
{
    gage_setStateAt(j, t, FALSE);
}

static void gage_setStateAt(int j, DateTime t, int advanceTolerance)''')
    text = once(text, '    t += OneSecond;', '    if (advanceTolerance) t += OneSecond;')
    contents[name] = text.encode('utf-8')
    name = 'src/solver/output.c'; text = contents[name].decode('utf-8')
    text = once(text, '''    // --- update reported rainfall at each rain gage
    for ( j=0; j<Nobjects[GAGE]; j++ )
    {
        gage_setReportRainfall(j, reportDate);
    }''', '''    // --- update reported rainfall at each rain gage
    if (Frunoff.mode == USE_FILE) runoff_updateReplayRain(reportTime);
    if (ErrorCode) return;
    for ( j=0; j<Nobjects[GAGE]; j++ )
    {
        if (Frunoff.mode == USE_FILE) Gage[j].reportRainfall = Gage[j].rainfall;
        else gage_setReportRainfall(j, reportDate);
    }''')
    contents[name] = text.encode('utf-8')
    name = 'src/solver/error.h'; text = contents[name].decode('utf-8')
    text = once(text, '      ERR_RUNOFF_FILE_FORMAT   = 325,',
        '      ERR_RUNOFF_FILE_FORMAT   = 325,\n      ERR_RUNOFF_FILE_WRITE    = 326,')
    contents[name] = text.encode('utf-8')
    name = 'src/solver/error.txt'; text = contents[name].decode('utf-8')
    text = once(text, 'ERR(327,',
        'ERR(326,"\\n  ERROR 326: error writing runoff interface file %s.")\nERR(327,')
    contents[name] = text.encode('utf-8')
