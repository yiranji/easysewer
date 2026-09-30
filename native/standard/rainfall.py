"""Checked binary RAIN lifecycle and bounded external-rain text access."""

from pathlib import Path
from hotstart import once


def replace_function(text, first, following, replacement):
    if text.count(first) != 1 or text.count(following) != 1:
        raise ValueError('Unexpected rainfall function boundary')
    start = text.index(first)
    end = text.index(following, start)
    return text[:start] + replacement + '\n\n' + text[end:]


def patch_rainfall(contents):
    name = 'src/solver/rain.c'
    text = contents[name].decode('utf-8')
    text = once(text, '#include <string.h>',
        '#include <string.h>\n#include <limits.h>\n#include <errno.h>\n#include <ctype.h>\n#include <math.h>')
    text = once(text, 'static int  findGageInFile(int i, int kount);\n', '')
    text = replace_function(text, 'void createRainFile(int count)\n',
                            'int rainFileConflict(int i)\n', '')
    text = replace_function(text, 'void initRainFile(void)\n',
                            'int findFileFormat(FILE *f, int i, int *hdrLines)\n', '')
    fragment = Path(__file__).with_suffix('.c').read_text(encoding='utf-8')
    text = once(text, 'void  rain_open(void)', fragment + '\n\nvoid  rain_open(void)')
    text = once(text, '        getTempFileName(Frain.name);', '''        if (!getTempFileName(Frain.name))
        {
            report_writeErrorMsg(ERR_RAIN_FILE_SCRATCH, "");
            return;
        }''')
    text = once(text, 'fopen(Frain.name, "r+b")', 'fopen(Frain.name, "rb")')
    text = once(text, '    rdii_openRdii();', '    if (!ErrorCode) rdii_openRdii();')
    text = once(text, '        fileFormat = findFileFormat(f, i, &hdrLines);',
        '''        GageIndex = i;
        fileFormat = findFileFormat(f, i, &hdrLines);
        if (ferror(f) && !ErrorCode)
            report_writeErrorMsg(ERR_RAIN_FILE_READ, Gage[i].fname);''')
    text = once(text, '            report_writeErrorMsg(ERR_RAIN_FILE_FORMAT, Gage[i].fname);',
        '            if (!ErrorCode) report_writeErrorMsg(ERR_RAIN_FILE_FORMAT, Gage[i].fname);')
    text = once(text, '''        fclose(Frain.file);
        if ( Frain.mode == SCRATCH_FILE ) remove(Frain.name);''', '''        if (fclose(Frain.file) != 0)
            rainFail(Frain.mode == USE_FILE ? ERR_RAIN_FILE_READ : ERR_RAIN_FILE_WRITE);
        if (Frain.mode == SCRATCH_FILE || (Frain.mode == SAVE_FILE && ErrorCode))
            remove(Frain.name);''')
    text = once(text, '''        fclose(f);
    }
    if ( ErrorCode ) return 0;''', '''        if (ferror(f) && !ErrorCode)
            report_writeErrorMsg(ERR_RAIN_FILE_READ, Gage[i].fname);
        if (fclose(f) != 0 && !ErrorCode)
            report_writeErrorMsg(ERR_RAIN_FILE_READ, Gage[i].fname);
    }
    if ( ErrorCode ) return 0;''')
    # No fixed-field parser may inspect beyond a line's terminating NUL.
    for format in ('%2d %4s %2s %4d', '%2d,%4s,%2s,%4d'):
        old = f'n = sscanf(&line[37], "{format}", &div, elemType, recdType, &year);'
        text = once(text, old, f'n = strlen(line) > 37 ? sscanf(&line[37], "{format}", &div, elemType, recdType, &year) : 0;')
    text = once(text, '    rewind(f);\n    fgets(line, MAXLINE, f);',
        '''    if (fseek(f, 0, SEEK_SET) != 0)
    {
        if (!ErrorCode) report_writeErrorMsg(ERR_RAIN_FILE_READ, Gage[GageIndex].fname);
        return UNKNOWN_FORMAT;
    }
    if (!fgets(line, MAXLINE, f)) return UNKNOWN_FORMAT;''')
    text = once(text, '        DataOffset = n - 11;',
        '        if (n < 11 || ValueOffset >= (int)strlen(line)) return UNKNOWN_FORMAT;\n        DataOffset = n - 11;')
    text = once(text, '    rewind(f);', '''    if (fseek(f, 0, SEEK_SET) != 0)
    {
        if (!ErrorCode) report_writeErrorMsg(ERR_RAIN_FILE_READ, Gage[GageIndex].fname);
        return;
    }''')
    text = once(text, '       if ( n < 0 ) break;', '       if (n < 0 || ErrorCode) break;')
    text = once(text, '        if ( lineLength <= DataOffset + 23 ) return 0;',
        '        if (DataOffset < 0 || ValueOffset < 0 || ValueOffset >= lineLength ||\n            lineLength <= DataOffset + 23) return 0;')
    text = once(text, '        if ( n < 3 || hour >= 25 ) break;',
        '        if (n < 3 || hour < 0 || hour >= 25 || minute < 0 || minute >= 60) break;')
    text = once(text, '        if ( sscanf(&line[col], "%6ld%c", &v, &flag) < 2 ) return 0;',
        '        if (strlen(line) < (size_t)col + 7 ||\n            sscanf(&line[col], "%6ld%c", &v, &flag) < 2) return 0;')
    text = once(text, '        if ( x > 0 || isMissing)\n',
        '        if (ErrorCode) return -1;\n        if ( x > 0 || isMissing)\n')
    text = replace_function(text, 'int readNwsOnlineValue(char* s, long* v, char* flag)\n',
                            'void  setCondition(char flag)\n', ONLINE_VALUE)
    text = replace_function(text, 'int parseStdLine(char *line, int *year, int *month, int *day, int *hour,\n',
                            'void saveAccumRainfall(DateTime date1, int hour, int minute, long v)\n', STANDARD_LINE)
    # Validate before datetime_encodeTime performs integer arithmetic.
    text = once(text, '    date2 = date1 + datetime_encodeTime(hour, minute, 0);\n    if ( date2 <= PreviousDate )',
        '''    if (!rainDateValid(date1) || hour < 0 || minute < 0 || minute >= 60 ||
        (double)hour*3600 + minute*60 > INT_MAX || !isfinite(x))
    {
        if (!ErrorCode) report_writeErrorMsg(ERR_RAIN_FILE_FORMAT, Gage[GageIndex].fname);
        return -1;
    }
    date2 = date1 + datetime_encodeTime(hour, minute, 0);
    if ( date2 <= PreviousDate )''')
    text = once(text, '    n = (datetime_timeDiff(date2, AccumStartDate) / Interval) + 1;', '''    {
        int h1, m1, s1, h2, m2, s2;
        double seconds, periods;
        if (!rainDateValid(date1) || !rainDateValid(date2) ||
            !rainDateValid(AccumStartDate) || Interval <= 0)
        { rainFail(ERR_RAIN_FILE_FORMAT); return; }
        datetime_decodeTime(date2, &h1, &m1, &s1);
        datetime_decodeTime(AccumStartDate, &h2, &m2, &s2);
        seconds = floor((floor(date2)-floor(AccumStartDate))*86400.0+0.5)
            + (3600*h1+60*m1+s1) - (3600*h2+60*m2+s2);
        periods = floor(seconds / Interval) + 1;
        if (seconds < 0 || periods > INT_MAX || periods < 1)
        { rainFail(ERR_RAIN_FILE_FORMAT); return; }
        n = (int)periods;
    }''')
    text = once(text, '        RainStats.periodsMissing += n;',
        '        if (n > INT_MAX - RainStats.periodsMissing)\n        { rainFail(ERR_RAIN_FILE_FORMAT); return; }\n        RainStats.periodsMissing += n;')
    text = once(text, '    RainStats.periodsRain += n;',
        '    if (n > INT_MAX - RainStats.periodsRain)\n    { rainFail(ERR_RAIN_FILE_FORMAT); return; }\n    RainStats.periodsRain += n;')
    text = once(text, '    if ( isMissing ) RainStats.periodsMissing++;',
        '''    if (ErrorCode) return;
    if (!rainDateValid(date1) || !isfinite(x) || hour < 0 || minute < 0 || minute >= 60 ||
        (double)hour*3600 + minute*60 > INT_MAX ||
        RainStats.periodsMissing == INT_MAX || RainStats.periodsRain == INT_MAX)
    { rainFail(ERR_RAIN_FILE_FORMAT); return; }
    if ( isMissing ) RainStats.periodsMissing++;''')
    # The four remaining writes emit ordinary or accumulated sample pairs.
    for old, new in (
        ('fwrite(&date2, sizeof(DateTime), 1, Frain.file);', 'if (!rainWrite(&date2, sizeof(DateTime), 1)) return;'),
        ('fwrite(&x, sizeof(float), 1, Frain.file);', 'if (!rainWrite(&x, sizeof(float), 1)) return;'),
    ):
        if text.count(old) != 2: raise ValueError('Unexpected rainfall sample writes')
        text = text.replace(old, new)
    contents[name] = text.encode('utf-8')
    name = 'src/solver/gage.c'; text = contents[name].decode('utf-8')
    text = once(text, '''            fseek(Frain.file, Gage[j].startFilePos, SEEK_SET);
            fread(&Gage[j].startDate, sizeof(DateTime), 1, Frain.file);
            fread(&vFirst, sizeof(float), 1, Frain.file);
            Gage[j].currentFilePos = ftell(Frain.file);''', '''            if (!rain_readRecord(j, Gage[j].startFilePos, NO_DATE,
                                 &Gage[j].startDate, &vFirst)) return 0;
            Gage[j].nextDate = Gage[j].startDate;''')
    text = once(text, '''                fseek(Frain.file, Gage[j].currentFilePos, SEEK_SET);
                fread(&Gage[j].nextDate, sizeof(DateTime), 1, Frain.file);
                fread(&vNext, sizeof(float), 1, Frain.file);
                Gage[j].currentFilePos = ftell(Frain.file);''', '''                if (!rain_readRecord(j, Gage[j].currentFilePos, Gage[j].nextDate,
                                     &Gage[j].nextDate, &vNext)) return 0;''')
    contents[name] = text.encode('utf-8')
    name = 'src/solver/funcs.h'; text = contents[name].decode('utf-8')
    text += '\nint rain_readRecord(int j, long position, DateTime previous, DateTime* date, float* value);\n'
    contents[name] = text.encode('utf-8')
    name = 'src/solver/error.h'; text = contents[name].decode('utf-8')
    text = once(text, '      ERR_RAIN_FILE_GAGE       = 321,',
        '      ERR_RAIN_FILE_GAGE       = 321,\n      ERR_RAIN_FILE_READ       = 322,\n      ERR_RAIN_FILE_WRITE      = 324,')
    contents[name] = text.encode('utf-8')
    name = 'src/solver/error.txt'; text = contents[name].decode('utf-8')
    text = once(text, 'ERR(323,',
        'ERR(322,"\\n  ERROR 322: error reading rainfall file %s.")\n\nERR(323,')
    text = once(text, 'ERR(325,',
        'ERR(324,"\\n  ERROR 324: error writing rainfall interface file %s.")\n\nERR(325,')
    contents[name] = text.encode('utf-8')


STANDARD_LINE = r'''int parseStdLine(char *line, int *year, int *month, int *day, int *hour,
                 int *minute, float *value)
{
    int i, length = 0;
    int* fields[5] = {year, month, day, hour, minute};
    char token[MAXLINE], *end, *cursor = line;
    long number;
    while (isspace((unsigned char)*cursor)) cursor++;
    while (*cursor && !isspace((unsigned char)*cursor) && length < MAXLINE-1)
        token[length++] = *cursor++;
    token[length] = '\0';
    if (!length || (StationID && !strcomp(token, StationID))) return 0;
    for (i = 0; i < 5; i++)
    {
        errno = 0;
        number = strtol(cursor, &end, 10);
        if (end == cursor || errno == ERANGE || number < INT_MIN || number > INT_MAX ||
            (*end && !isspace((unsigned char)*end))) return 0;
        *fields[i] = (int)number;
        cursor = end;
    }
    *value = strtof(cursor, &end);
    return end != cursor;
}'''

ONLINE_VALUE = r'''int readNwsOnlineValue(char* s, long* v, char* flag)
{
    char* end;
    if (strchr(s, '.'))
    {
        float x = strtof(s, &end);
        double rounded;
        if (end == s || !isfinite(x)) return 0;
        rounded = (double)(100.0f * x + 0.5f);
        if (!isfinite(rounded) || rounded >= (double)LONG_MAX || rounded < (double)LONG_MIN)
            return 0;
        *v = (long)rounded;
    }
    else
    {
        errno = 0;
        *v = strtol(s, &end, 10);
        if (end == s || errno == ERANGE) return 0;
    }
    while (isspace((unsigned char)*end)) end++;
    if (*end) { *flag = *end; return 2; }
    return 1;
}'''
