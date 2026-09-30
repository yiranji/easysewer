"""Checked solver OUT writes, report rereads and completion ownership."""

from pathlib import Path
from hotstart import once
from rainfall import replace_function


def patch_output(contents):
    name = 'src/solver/output.c'
    text = contents[name].decode('utf-8')
    begin = text.index('// Large File Support')
    end = text.index('#include <stdlib.h>', begin)
    text = text[:begin] + '''#define _FILE_OFFSET_BITS 64
#ifdef _WIN32
  #define F_OFF int64_t
  #define F_SEEK _fseeki64
  #define F_TELL _ftelli64
#else
  #define F_OFF int64_t
  #define F_SEEK fseeko
  #define F_TELL ftello
#endif
#include <stdint.h>
#include <limits.h>
#include <errno.h>
''' + text[end:]
    # Retain calculation order, but distinguish floats/dates from integer
    # records so no float payload can silently serialize NaN/Inf.
    text = text.replace('fwrite(', 'outWrite(')
    for value in ('&SubcatchResults[0]', 'NodeResults', 'LinkResults', 'SysResults', 'SubcatchResults'):
        text = text.replace('outWrite(' + value + ',', 'outFloats(' + value + ',')
    for value in ('&z', '&date'):
        text = text.replace('outWrite(' + value + ',', 'outDate(' + value + ',')
    text = text.replace('ftell(Fout.file)', 'outTell()')
    text = once(text, '    // --- open binary output file\n', '''    // Release prior start/end buffers before another start on this project.
    output_close();
    OutReady = OutFailed = OutEnded = 0;
    // --- open binary output file
''')
    text = once(text, '    // --- subcatchment results consist of Rainfall, Snowdepth, Evap,', '''    if (NumPolluts < 0 || NumPolluts > INT_MAX - MAX_SUBCATCH_RESULTS)
    {
        outFail(ERR_OUT_SIZE);
        return ErrorCode;
    }
    // --- subcatchment results consist of Rainfall, Snowdepth, Evap,''')
    first = '    numResults = ((F_OFF)NumSubcatch * (F_OFF)NumSubcatchVars)'
    begin = text.index(first)
    end = text.index('    Nperiods = 0;', begin)
    text = text[:begin] + '''    numResults = MAX_SYS_RESULTS;
    if ((F_OFF)NumSubcatch > ((INT64_MAX-8)/4-numResults)/NumSubcatchVars)
        { outFail(ERR_OUT_SIZE); return ErrorCode; }
    numResults += (F_OFF)NumSubcatch * NumSubcatchVars;
    if ((F_OFF)NumNodes > ((INT64_MAX-8)/4-numResults)/NumNodeVars)
        { outFail(ERR_OUT_SIZE); return ErrorCode; }
    numResults += (F_OFF)NumNodes * NumNodeVars;
    if ((F_OFF)NumLinks > ((INT64_MAX-8)/4-numResults)/NumLinkVars)
        { outFail(ERR_OUT_SIZE); return ErrorCode; }
    numResults += (F_OFF)NumLinks * NumLinkVars;
    BytesPerPeriod = sizeof(REAL8) + numResults * sizeof(REAL4);
''' + text[end:]
    text = once(text, '    F_SEEK(Fout.file, 0, SEEK_SET);',
                '    if (!outSeek(0, ERR_OUT_WRITE)) return ErrorCode;')
    text = once(text, '    OutputStartPos = outTell();\n    return ErrorCode;',
                '    OutputStartPos = outTell();\n    if (!ErrorCode) OutReady = 1;\n    return ErrorCode;')
    text = once(text, '    if (Fout.file != NULL) fclose(Fout.file); ',
                '    if (Fout.file != NULL) { if (output_closeFile()) return; }')
    text = once(text, '        getTempFileName(Fout.name);',
                '        if (!getTempFileName(Fout.name)) { outFail(ERR_OUT_FILE); return; }')
    text = once(text, '    REAL8 date;\n', '    REAL8 date;\n    F_OFF position;\n')
    text = once(text, '    if ( reportDate < ReportStart ) return;', '''    if (ErrorCode || !OutReady || OutEnded || OutFailed) return;
    if ( reportDate < ReportStart ) return;
    if (Nperiods >= INT32_MAX) { outFail(ERR_OUT_SIZE); return; }
    if (!outPosition(Nperiods, &position) || !outSeek(position, ERR_OUT_WRITE)) return;''')
    text = once(text, '    // --- save outfall flows to interface file if called for',
                '    if (ErrorCode) { OutFailed = 1; return; }\n\n    // --- save outfall flows to interface file if called for')
    text = replace_function(text, 'void output_end()\n', 'void output_close()\n', '''void output_end()
{
    INT4 footer[6];
    F_OFF position;
    if (!OutReady || OutEnded || OutFailed || !Fout.file) return;
    if (!outPosition(Nperiods, &position) || !outSeek(position, ERR_OUT_WRITE)) return;
    footer[0] = (INT4)IDStartPos;
    footer[1] = (INT4)InputStartPos;
    footer[2] = (INT4)OutputStartPos;
    footer[3] = (INT4)Nperiods;
    footer[4] = (INT4)ErrorCode;
    footer[5] = MAGICNUMBER;
    if (outWrite(footer, sizeof(INT4), 6, Fout.file) != 6) return;
    if (fflush(Fout.file) != 0 || ferror(Fout.file)) { outFail(ERR_OUT_WRITE); return; }
    OutEnded = 1;
}

''')
    text = once(text, '        report_writeErrorMsg(ERR_OUT_WRITE, "");',
                '        outFail(ERR_OUT_WRITE);')
    text = once(text, '    output_closeAvgResults();\n}',
                '    output_closeAvgResults();\n    OutReady = 0;\n    Nsteps = 0;\n}')
    text = replace_function(text, 'void output_readDateTime(long period, DateTime* days)\n',
                            'int output_openAvgResults()\n', '')
    text = once(text, '    if ( AvgNodeResults == NULL ) return FALSE;',
                '    if (NumNodes && AvgNodeResults == NULL) return FALSE;')
    text = once(text, '    if (AvgLinkResults == NULL)\n',
                '    if (NumLinks && AvgLinkResults == NULL)\n')
    text = once(text, '    int i, j, k, sign;\n',
                '    int i, j, k, sign;\n    if (ErrorCode || !OutReady || OutEnded) return;\n'
                '    if (Nsteps == INT_MAX) { outFail(ERR_OUT_SIZE); return; }\n')
    text = once(text, '    // --- examine each reportable node',
                '    if ((NumNodes || NumLinks) && Nsteps < 1) { outFail(ERR_OUT_WRITE); return; }\n\n'
                '    // --- examine each reportable node')
    fragment = Path(__file__).with_suffix('.c').read_text(encoding='utf-8')
    text = once(text, 'int output_open()\n', fragment + '\n\nint output_open()\n')
    contents[name] = text.encode('utf-8')
    name = 'src/solver/funcs.h'
    text = contents[name].decode('utf-8')
    text += '\nint output_closeFile(void);\n'
    contents[name] = text.encode('utf-8')
    name = 'src/solver/swmm5.c'
    text = contents[name].decode('utf-8')
    text = once(text, '        output_open();', '        output_open();\n        if (ErrorCode) return ErrorCode;')
    text = once(text, '    if ( !ErrorCode )\n        report_writeReport();', '''    if (ErrorCode) return ErrorCode;
    if (!IsOpenFlag) return (ErrorCode = ERR_API_NOT_OPEN);
    if (IsStartedFlag) return (ErrorCode = ERR_API_NOT_ENDED);
    if (!Fout.file) return (ErrorCode = ERR_API_NOT_STARTED);
    report_writeReport();''')
    start = text.index('int DLLEXPORT swmm_close()')
    end = text.index('int  DLLEXPORT swmm_getMassBalErr', start)
    close = text[start:end]
    close = once(close, 'int DLLEXPORT swmm_close()\n{\n',
                 'int DLLEXPORT swmm_close()\n{\n    int previous = ErrorCode, closeCode = 0, code;\n')
    close = once(close, '    if (IsStartedFlag) swmm_end();',
                 '    if (IsStartedFlag) { swmm_end(); if (!previous) closeCode = ErrorCode; }')
    close = once(close, '    if (Fout.file) output_close();',
                 '    output_close();\n    code = output_closeFile();\n    if (!closeCode) closeCode = code;')
    first = close.index('    if (Fout.file)\n')
    last = close.index('    IsOpenFlag = FALSE;', first)
    close = close[:first] + close[last:]
    close = once(close, '    return 0;', '    return closeCode;')
    text = text[:start] + close + text[end:]
    text = once(text, '    if (!IsOpenFlag)\n        return 0;\n    if (IsStartedFlag)\n',
                '    if (!IsOpenFlag || !Fout.file || ErrorCode)\n        return 0;\n    if (IsStartedFlag)\n')
    for kind, obj in (('Subcatch', 'SUBCATCH'), ('Node', 'NODE'), ('Link', 'LINK')):
        # Bound indexes before dereferencing model arrays, including empty groups.
        anchor = ('    int outIndex = ' if kind != 'Link' else '    int    outIndex = ') + kind + '[index].rptFlag - 1;'
        replacement = '    int outIndex;\n    if (index < 0 || index >= Nobjects[' + obj + ']) return 0;\n    outIndex = ' + kind + '[index].rptFlag - 1;'
        text = once(text, anchor, replacement)
        text = once(text, '    output_read' + kind + 'Results(period, outIndex);',
                    '    output_read' + kind + 'Results(period, outIndex);\n    if (ErrorCode) return 0;')
    text += '\nint DLLEXPORT swmm_getEasySewerSolverOutputIO(void)\n{\n    return 1;\n}\n'
    contents[name] = text.encode('utf-8')
    name = 'src/solver/report.c'
    text = contents[name].decode('utf-8')
    for call in ('output_readDateTime(period, &days);', 'output_readSubcatchResults(period, k);',
                 'output_readNodeResults(period, k);', 'output_readLinkResults(period, k);'):
        expected = 3 if 'DateTime' in call else 1
        if text.count(call) != expected:
            raise ValueError('Unexpected report OUT call: ' + call)
        text = text.replace(call, call + '\n                if (ErrorCode) return;')
    text = once(text, '    if ( IgnoreRouting == TRUE && IgnoreQuality == TRUE ) return;',
                '    if (ErrorCode) return;\n    if ( IgnoreRouting == TRUE && IgnoreQuality == TRUE ) return;')
    text = once(text, '    if ( RptFlags.links != NONE ) report_Links();',
                '    if (!ErrorCode && RptFlags.links != NONE) report_Links();')
    contents[name] = text.encode('utf-8')
