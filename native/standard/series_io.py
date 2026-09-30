"""Shared table-stream and lookup-cursor patch, applied after climate I/O.

The public Model schema and solver API do not expose these private C fields.
This patch remains a candidate until both library families are qualified.
"""

from pathlib import Path
from hotstart import once
from rainfall import replace_function


def patch_series(contents):
    name = 'src/solver/objects.h'
    text = contents[name].decode('utf-8')
    text = once(text, 'typedef struct\n{\n   char*         ID;              // Table/time series ID',
                'typedef struct Table\n{\n   char*         ID;              // Table/time series ID')
    text = once(text, '   TFile         file;            // external data file\n}  TTable;',
                '''   TFile         file;            // external data file
   struct Table* lookup;          // owned cursor, borrowed immutable entries
   int           lookupReady;
   int           lookupEnded;
   int           fileHasPrevious;
   int           fileValidating;
   double        filePrevious;
}  TTable;''')
    contents[name] = text.encode('utf-8')

    name = 'src/solver/funcs.h'
    contents[name] += b'\nint table_closeFile(TTable *table);\nint table_closeFiles(void);\n'
    name = 'src/solver/table.c'
    text = contents[name].decode('utf-8')
    text = once(text, '#include <stdlib.h>', '#include <limits.h>\n#include <stdlib.h>')
    fragment = Path(__file__).with_suffix('.c').read_text(encoding='utf-8')
    start = text.index('void   table_tseriesInit(TTable *table)\n')
    text = text[:start]  # Replaced through the original end-of-file parsers.
    text = once(text, 'int table_readCurve(char* tok[], int ntoks)',
                fragment + '\n\nint table_readCurve(char* tok[], int ntoks)')
    text = once(text, '    if ( !entry ) return FALSE;',
                '    if ( !entry ) return tsFail(table, ERR_MEMORY);')
    text = once(text, '''    if (table->file.file)
    { 
        fclose(table->file.file);
        table->file.file = NULL;
    }''', '    table_closeFile(table);')
    text = once(text, '    table->curveType = -1;', '''    table->curveType = -1;
    table->lookup = NULL;
    table->lookupReady = table->lookupEnded = 0;
    table->fileHasPrevious = table->fileValidating = 0;
    table->filePrevious = 0.0;
    table->file.name[0] = 0;''')
    text = replace_function(text, 'int   table_validate(TTable *table)\n',
                            'int table_getFirstEntry(TTable *table, double *x, double *y)\n', VALIDATE)
    text = once(text, '''        if ( table->file.file == NULL ) return FALSE;
        rewind(table->file.file);
        return table_getNextFileEntry(table, x, y);''', '''        if (!table->file.file || fseek(table->file.file, 0, SEEK_SET))
            return tsFail(table, ERR_TABLE_FILE_READ);
        clearerr(table->file.file);
        table->lastDate = StartDateTime;
        table->fileHasPrevious = FALSE;
        return table_getNextFileEntry(table, x, y);''')
    text = once(text, '    entry = table->thisEntry->next;',
                '    entry = table->thisEntry ? table->thisEntry->next : NULL;')
    contents[name] = text.encode('utf-8')

    # Climate owns independent table clones. Their new lookup cursor is never
    # inherited from the project table, and closing releases only owned state.
    name = 'src/solver/climate.c'
    text = contents[name].decode('utf-8')
    text = replace_function(text, 'static int clCloseSeries(TTable *series)\n',
                            'static int clSeriesError(TTable *series)\n',
                            'static int clCloseSeries(TTable *series)\n{\n    return table_closeFile(series);\n}\n\n')
    text = once(text, '    *series = Tseries[index];',
                '    *series = Tseries[index];\n    series->lookup = NULL;\n    series->fileValidating = FALSE;')
    text = replace_function(text, 'static int clFirstEntry(TTable *series, double *date, double *value)\n',
                            '/* Keep an unadjusted current rate', '''static int clFirstEntry(TTable *series, double *date, double *value)
{
    int found = table_getFirstEntry(series, date, value);
    if (!found && series->file.mode == USE_FILE && !ErrorCode)
        report_writeErrorMsg(ERR_TABLE_FILE_READ, series->file.name);
    return found && !ErrorCode;
}

''')
    contents[name] = text.encode('utf-8')

    name = 'src/solver/swmm5.c'
    text = contents[name].decode('utf-8')
    text = once(text, '    if (IsOpenFlag) project_close();', '''    code = table_closeFiles();
    if (!closeCode) closeCode = code;
    if (IsOpenFlag) project_close();''')
    text += '\nint DLLEXPORT swmm_getEasySewerTimeSeriesIO(void)\n{\n    return 1;\n}\n'
    contents[name] = text.encode('utf-8')

    name = 'src/solver/project.c'
    text = contents[name].decode('utf-8')
    text = once(text, '         if ( err ) report_writeErrorMsg(ERR_CURVE_SEQUENCE, Curve[i].ID);',
                '         if (err) { report_writeErrorMsg(err, Curve[i].ID); return; }')
    text = once(text, '        if ( err ) report_writeTseriesErrorMsg(err, &Tseries[i]);',
                '        if (err) { report_writeTseriesErrorMsg(err, &Tseries[i]); return; }')
    text = once(text, '    climate_initState();', '    climate_initState();\n    if (ErrorCode) return ErrorCode;')
    text = once(text, '    for (j=0; j<Nobjects[TSERIES]; j++)  table_tseriesInit(&Tseries[j]);',
                '''    for (j=0; j<Nobjects[TSERIES]; j++) {
        table_tseriesInit(&Tseries[j]);
        if (ErrorCode) return ErrorCode;
    }''')
    contents[name] = text.encode('utf-8')

    name = 'src/solver/gage.c'
    text = contents[name].decode('utf-8')
    text = '#include <limits.h>\n' + text
    text = once(text, '        gageInterval = (int)(floor(Tseries[k].dxMin*SECperDAY + 0.5));',
                '''        if (Tseries[k].dxMin >= (double)INT_MAX/SECperDAY) gageInterval = INT_MAX;
        else gageInterval = (int)(floor(Tseries[k].dxMin*SECperDAY + 0.5));''')
    contents[name] = text.encode('utf-8')


VALIDATE = '''int table_validate(TTable *table)
{
    double x1, y1, x2, y2, dx, minimum = BIG;
    int result, code = 0;
    if (table->file.mode == USE_FILE) {
        if (table_closeFile(table)) return ERR_TABLE_FILE_READ;
        table->file.file = fopen(table->file.name, "rb");
        if (!table->file.file) return ERR_TABLE_FILE_OPEN;
    }
    table->fileValidating = TRUE;
    result = table_getFirstEntry(table, &x1, &y1);
    if (!result) {
        if (ErrorCode || table->file.mode == USE_FILE) code = ERR_TABLE_FILE_READ;
        goto done;
    }
    for (;;) {
        if (!isfinite(x1) || !isfinite(y1) || (table->curveType < 0 &&
            (x1 < datetime_encodeDate(1,1,1) || x1 >= datetime_encodeDate(9999,12,31)+1.0))) {
            code = ERR_TABLE_FILE_READ; break;
        }
        if (!table_getNextEntry(table, &x2, &y2)) {
            if (ErrorCode) code = ERR_TABLE_FILE_READ;
            break;
        }
        dx = x2-x1;
        if (dx <= 0.0) { table->x2 = x2; code = ERR_CURVE_SEQUENCE; break; }
        minimum = MIN(minimum, dx);
        x1 = x2; y1 = y2;
    }
done:
    table->fileValidating = FALSE;
    if (code) table_closeFile(table);
    else table->dxMin = minimum;
    return code;
}

'''
