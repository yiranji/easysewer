"""Shared checked RPT writes and ownership, applied after time-series I/O."""

from pathlib import Path
import re

from hotstart import once
from rainfall import replace_function


SINKS = {'culvert.c': 1, 'inlet.c': 18, 'inputrpt.c': 77, 'lid.c': 18,
         'lidproc.c': 2, 'report.c': 232, 'statsrpt.c': 152, 'toposort.c': 3}


def patch_report(contents):
    # An RPT failure can close a project before input_countObjects/createObjects
    # have run. Destructors must leave no pointers or counts from the last owner.
    name = 'src/solver/project.c'
    text = contents[name].decode('utf-8')
    text = once(text, '        if ( Htable[j] != NULL ) HTfree(Htable[j]);',
                '        if ( Htable[j] != NULL ) HTfree(Htable[j]);\n'
                '        Htable[j] = NULL;')
    text = once(text, '    if ( MemPoolAllocated ) AllocFreePool();',
                '    if ( MemPoolAllocated ) AllocFreePool();\n'
                '    MemPoolAllocated = FALSE;')
    contents[name] = text.encode('utf-8')
    name = 'src/solver/controls.c'
    text = contents[name].decode('utf-8')
    start = text.index('void controls_delete(void)\n')
    end = text.index('\n//====', start)
    region = text[start:end]
    region = once(region, '   for (i = 0; i < ExpressionCount; i++)',
                  '   if (Expression) for (i = 0; i < ExpressionCount; i++)')
    region = once(region, '''   if ( RuleCount == 0 ) return;
   deleteActionList();
   deleteRules();''', '''   deleteActionList();
   if (Rules) deleteRules();
   controls_init();''')
    text = text[:start] + region + text[end:]
    contents[name] = text.encode('utf-8')

    for filename, expected in SINKS.items():
        name = 'src/solver/' + filename
        text, count = re.subn(r'fprintf\s*\(\s*Frpt\.file\s*,\s*',
                              'report_writeFormat(', contents[name].decode('utf-8'))
        if count != expected:
            raise ValueError('Unexpected RPT sinks in ' + filename)
        contents[name] = text.encode('utf-8')

    name = 'src/solver/error.h'
    text = contents[name].decode('utf-8')
    text = once(text, '      ERR_RPT_FILE             = 305,',
                '      ERR_RPT_FILE             = 305,\n      ERR_RPT_WRITE            = 306,')
    contents[name] = text.encode('utf-8')
    name = 'src/solver/error.txt'
    contents[name] += b'\nERR(306,"\\n  ERROR 306: error writing to report file.")\n'
    name = 'src/solver/funcs.h'
    contents[name] += b'''
void report_resetFile(void);
int report_writeFormat(const char *format, ...);
int report_checkFile(void);
int report_closeFile(void);
'''

    name = 'src/solver/report.c'
    text = contents[name].decode('utf-8')
    text = once(text, '#include <stdlib.h>', '#include <stdarg.h>\n#include <stdlib.h>')
    fragment = Path(__file__).with_suffix('.c').read_text(encoding='utf-8')
    text = once(text, 'int report_readOptions(char* tok[], int ntoks)',
                fragment + '\n\nint report_readOptions(char* tok[], int ntoks)')
    text = replace_function(text, 'void report_writeErrorMsg(int code, char* s)\n',
                            'void report_writeErrorCode()\n', ERROR_MESSAGE)
    contents[name] = text.encode('utf-8')

    name = 'src/solver/swmm5.c'
    text = contents[name].decode('utf-8')
    text = once(text, '    // --- initialize flags', '''    // A one-shot run cannot take ownership of an existing API session.
    // In particular, do not clear its flags and then close its resources.
    if (IsOpenFlag || IsStartedFlag || Finp.file || Frpt.file || Fout.file) {
        if (!ErrorCode) ErrorCode = ERR_API_NOT_ENDED;
        return report_checkFile();
    }

    // --- initialize flags''')
    text = once(text, '{\n// --- to be safe, reset the state of the floating point unit', '''{
    // Reopening would discard the only pointers to the current project and
    // its input/report/output streams. The caller must close that owner first.
    if (IsOpenFlag || IsStartedFlag || Finp.file || Frpt.file || Fout.file) {
        if (!ErrorCode) ErrorCode = ERR_API_NOT_ENDED;
        return ErrorCode;
    }
// --- to be safe, reset the state of the floating point unit''')
    text = once(text, "        ErrorMsg[0] = '\\0';",
                "        ErrorMsg[0] = '\\0';\n        report_resetFile();")
    text = once(text, '        report_writeLogo();',
                '        report_writeLogo();\n        if (report_checkFile()) return ErrorCode;')
    text = once(text, '        report_writeTitle();',
                '        report_writeTitle();\n        if (report_checkFile()) return ErrorCode;')
    text = once(text, '    // --- save saveResults flag to global variable',
                '    if (report_checkFile()) return ErrorCode;\n\n    // --- save saveResults flag to global variable')
    text = once(text, '''    if (IsOpenFlag)
        report_writeLine(line);''', '''    if (IsOpenFlag) {
        report_writeLine(line);
        report_checkFile();
    }''')
    text = once(text, '''    if (Frpt.file)
    {
        fclose(Frpt.file);
        Frpt.file = NULL;
    }''', '''    code = report_closeFile();
    if (!closeCode) closeCode = code;''')
    functions = ['swmm_open', 'swmm_start', 'swmm_step', 'swmm_stride',
                 'swmm_end', 'swmm_report']
    if 'int DLLEXPORT swmm_execRouting(void)' in text:
        functions += ['swmm_execRouting', 'swmm_saveResults']
    for function in functions:
        start = re.search(r'int\s+DLLEXPORT\s+' + function + r'\s*\(', text)
        if start is None:
            raise ValueError('Missing report boundary: ' + function)
        end = text.index('\n//====', start.end())
        region = text[start.start():end]
        if 'return ErrorCode;' not in region:
            raise ValueError('Unexpected report return: ' + function)
        region = region.replace('return ErrorCode;', 'return report_checkFile();')
        text = text[:start.start()] + region + text[end:]
    text += '\nint DLLEXPORT swmm_getEasySewerReportIO(void)\n{\n    return 1;\n}\n'
    contents[name] = text.encode('utf-8')


ERROR_MESSAGE = '''void report_writeErrorMsg(int code, char* s)
{
    /* Set the native error before attempting to describe it. If the RPT stream
     * then fails, its I/O error cannot replace this original failure. */
    if (RptFailed && ErrorCode) return;
    ErrorCode = code;
    if (ErrorCode <= ERR_INPUT || ErrorCode >= ERR_FILE_NAME)
        snprintf(ErrorMsg, MAXMSG, error_getMsg(ErrorCode, Msg), s);
    if (Frpt.file) {
        WRITE("");
        report_writeFormat(error_getMsg(code, Msg), s);
    }
}

'''
