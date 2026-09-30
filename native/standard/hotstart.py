"""Reviewed HOTSTART I/O corrections against the pinned EPA 5.2.4 source."""

import re


def once(text, old, new):
    if text.count(old) != 1:
        raise ValueError('Unexpected native patch anchor: '+old[:80])
    return text.replace(old, new)


def patch_hotstart(contents, *, buildup_count_fixed=False):
    name='src/solver/hotstart.c'
    text=contents[name].decode('utf-8')
    if buildup_count_fixed:
        # The pinned custom source already corrected the per-pollutant count.
        # Require that exact state before sharing the remaining I/O corrections.
        if ('fwrite(x, sizeof(double), Nobjects[POLLUT], f);' in text or
                text.count('                    fwrite(x, sizeof(double), 1, f);') != 1):
            raise ValueError('Unexpected corrected HOTSTART buildup write')
    else:
        text=once(text,'fwrite(x, sizeof(double), Nobjects[POLLUT], f);','fwrite(x, sizeof(double), 1, f);')
    text=once(text,'static int  readDouble(double* x, FILE* f);',
        'static int  readDouble(double* x, FILE* f);\n'
        'static int writeData(const void* data, size_t size, size_t count, FILE* f);')
    begin=text.index('int openHotstartFile1()')
    end=text.index('int openHotstartFile2()',begin)
    text=text[:begin]+INPUT+text[end:]
    begin=text.index('void hotstart_close()')
    end=text.index('int openHotstartFile1()',begin)
    text=text[:begin]+CLOSE+text[end:]
    begin=text.index('int  readFloat(float *x, FILE* f)\n')
    text=text[:begin]+READERS
    # Every writer checks item counts. Header failures close immediately;
    # payload failures return to hotstart_close, which always closes the file.
    begin=text.index('int openHotstartFile2()')
    end=text.index('void  saveRouting()',begin)
    part=text[begin:end]
    part,n=re.subn(r'(?m)^(\s*)fwrite\(([^;]+)\);',r'\1if (!writeData(\2)) goto write_error;',part)
    if n!=7:raise ValueError('Unexpected HOTSTART header writes')
    part=once(part,'    return TRUE;','''    return TRUE;
write_error:
    fclose(Fhotstart2.file);
    Fhotstart2.file = NULL;
    return FALSE;''')
    text=text[:begin]+part+text[end:]
    text,n=re.subn(r'(?m)^(\s*)fwrite\(([^;]+)\);',r'\1if (!writeData(\2)) return;',text)
    if n!=13:raise ValueError('Unexpected HOTSTART state writes: '+str(n))
    contents[name]=text.encode()
    name='src/solver/error.h';text=contents[name].decode()
    text=once(text,'      ERR_HOTSTART_FILE_READ   = 335,',
        '      ERR_HOTSTART_FILE_WRITE  = 334,\n      ERR_HOTSTART_FILE_READ   = 335,')
    contents[name]=text.encode()
    name='src/solver/error.txt';text=contents[name].decode()
    text=once(text,'ERR(335,', 'ERR(334,"\\n  ERROR 334: error writing hot start file %s.")\n\nERR(335,')
    contents[name]=text.encode()


INPUT=r'''int openHotstartFile1()
{
    int nSubcatch = Nobjects[SUBCATCH];
    int nLandUses = Nobjects[LANDUSE];
    int nNodes, nLinks, nPollut, flowUnits;
    char stamp[16] = {0};
    FILE* f;
    if (Fhotstart1.mode != USE_FILE) return TRUE;
    f = fopen(Fhotstart1.name, "rb");
    Fhotstart1.file = f;
    if (!f)
    {
        report_writeErrorMsg(ERR_HOTSTART_FILE_OPEN, Fhotstart1.name);
        return FALSE;
    }
    if (fread(stamp, 1, 15, f) != 15) goto format_error;
    if (strcmp(stamp, "SWMM5-HOTSTART4") == 0) fileVersion = 4;
    else if (strcmp(stamp, "SWMM5-HOTSTART3") == 0) fileVersion = 3;
    else if (strcmp(stamp, "SWMM5-HOTSTART2") == 0) fileVersion = 2;
    else
    {
        if (memcmp(stamp, "SWMM5-HOTSTART", 14) != 0) goto format_error;
        if (fseek(f, 14, SEEK_SET) != 0) goto format_error;
        fileVersion = 1;
    }
    if (fileVersion >= 2 && fread(&nSubcatch, sizeof(int), 1, f) != 1) goto format_error;
    if (fileVersion >= 3 && fread(&nLandUses, sizeof(int), 1, f) != 1) goto format_error;
    if (fread(&nNodes, sizeof(int), 1, f) != 1 ||
        fread(&nLinks, sizeof(int), 1, f) != 1 ||
        fread(&nPollut, sizeof(int), 1, f) != 1 ||
        fread(&flowUnits, sizeof(int), 1, f) != 1) goto format_error;
    if (nSubcatch != Nobjects[SUBCATCH] || nLandUses != Nobjects[LANDUSE] ||
        nNodes != Nobjects[NODE] || nLinks != Nobjects[LINK] ||
        nPollut != Nobjects[POLLUT] || flowUnits != FlowUnits) goto format_error;
    if (fileVersion >= 3) readRunoff();
    if (!ErrorCode) readRouting();
    if (!ErrorCode && (fgetc(f) != EOF || ferror(f))) goto format_error;
    fclose(f);
    Fhotstart1.file = NULL;
    return ErrorCode ? FALSE : TRUE;
format_error:
    if (!ErrorCode) report_writeErrorMsg(ERR_HOTSTART_FILE_FORMAT, "");
    fclose(f);
    Fhotstart1.file = NULL;
    return FALSE;
}

'''

CLOSE=r'''void hotstart_close()
{
    if (Fhotstart1.file)
    {
        fclose(Fhotstart1.file);
        Fhotstart1.file = NULL;
    }
    if (Fhotstart2.file)
    {
        if (!ErrorCode) saveRunoff();
        if (!ErrorCode) saveRouting();
        if (fclose(Fhotstart2.file) != 0 && !ErrorCode)
            report_writeErrorMsg(ERR_HOTSTART_FILE_WRITE, Fhotstart2.name);
        Fhotstart2.file = NULL;
    }
}

'''

READERS=r'''int readFloat(float* x, FILE* f)
{
    if (fread(x, sizeof(float), 1, f) != 1 || !isfinite(*x))
    {
        if (!ErrorCode) report_writeErrorMsg(ERR_HOTSTART_FILE_READ, "");
        *x = 0.0f;
        return FALSE;
    }
    return TRUE;
}

int readDouble(double* x, FILE* f)
{
    if (fread(x, sizeof(double), 1, f) != 1 || !isfinite(*x))
    {
        if (!ErrorCode) report_writeErrorMsg(ERR_HOTSTART_FILE_READ, "");
        *x = 0.0;
        return FALSE;
    }
    return TRUE;
}

static int writeData(const void* data, size_t size, size_t count, FILE* f)
{
    if (fwrite(data, size, count, f) != count)
    {
        if (!ErrorCode) report_writeErrorMsg(ERR_HOTSTART_FILE_WRITE, Fhotstart2.name);
        return FALSE;
    }
    return TRUE;
}
'''
