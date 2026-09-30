"""Checked native paths and physical input records, shared by both engines.

Applied after checkpoint ABI 2 to verified standard 13 / custom 11 sources.
The staged recipe does not register these candidate binaries for public use.
"""
from pathlib import Path

RECIPE_FILES = ('patch.py', 'paths.c', 'input.c', 'mempool.c', 'scratch.c')


def once(text, old, new):
    if text.count(old) != 1:
        raise ValueError('Unexpected path patch anchor: ' + old[:100])
    return text.replace(old, new)


helper=r'''int es_makeFileName(char *dest, const char *source, int absolute)
{
    char result[MAXFNAME+1];
    size_t prefix=0, length;
    if (!source) { dest[0]=0; return 0; }
    length=strlen(source);
    if (absolute && length && isRelativePath(source)) prefix=strlen(InpDir);
    if (length>MAXFNAME || prefix>MAXFNAME-length)
    { dest[0]=0; return 0; }
    if (prefix) memcpy(result,InpDir,prefix);
    memcpy(result+prefix,source,length+1);
    memcpy(dest,result,prefix+length+1);
    return 1;
}

char* addAbsolutePath(char* fname)
{
    if (!es_makeFileName(fname,fname,1))
        report_writeErrorMsg(ERR_FILE_PATH, "");
    return fname;
}
'''
linehelper=r'''/* Refuse an overlong physical INP record before parsing any prefix as a
 * complete record. External data formats retain their independent budgets. */
static int es_inputReadLine(char *line)
{
    size_t length, content;
    int ch;
    if (!fgets(line,MAXLINE+1,Finp.file)) return 0;
    length=strlen(line); content=length;
    if (content && line[content-1]=='\n') content--;
    if (content && line[content-1]=='\r') content--;
    if (content<MAXLINE) return 1;
    if (!length || line[length-1]!='\n')
        while ((ch=fgetc(Finp.file))!=EOF && ch!='\n') {}
    return -1;
}

'''

INPUT_READER=r'''static int es_inputReadLine(char *line)
{
    size_t length=0, content;
    int ch, any=0, overflow=0, invalid=0;
    while ((ch=fgetc(Finp.file))!=EOF)
    {
        any=1;
        if (ch=='\n') break;
        if (!ch) invalid=1;
        if (length<MAXLINE) line[length++]=(char)ch;
        else overflow=1;
    }
    line[length]=0;
    if (ferror(Finp.file)) return -3;
    if (!any) return 0;
    content=length;
    if (content && line[content-1]=='\r') content--;
    if (invalid) return -2;
    if (overflow || content>=MAXLINE) return -1;
    /* Normalize only the record terminator, preserving data and whitespace. */
    length=content;
    if (ch=='\n') line[length++]='\n';
    line[length]=0;
    return 1;
}

'''


def patch(contents, *, custom=False):
 def edit(name,fn):contents['src/solver/'+name]=fn(contents['src/solver/'+name].decode()).encode()
 edit('consts.h',lambda t:once(t,'#define   MAXFNAME           259','#define   MAXFNAME           4095'))
 edit('funcs.h',lambda t:t+'\nint es_makeFileName(char *dest, const char *source, int absolute);\n')
 edit('error.h',lambda t:once(t,'      ERR_TABLE_FILE_READ      = 363,','      ERR_TABLE_FILE_READ      = 363,\n      ERR_FILE_PATH            = 364,'))
 edit('error.txt',lambda t:t+'\nERR(364,"\\n  ERROR 364: file path exceeds native capacity or is invalid.")\n')
 edit('error.c',lambda t:once(t,'    strcpy(ErrString, s);', '''    size_t length = s ? strlen(s) : 0;
    if (length >= sizeof(ErrString)) length = sizeof(ErrString)-1;
    if (length) memcpy(ErrString, s, length);
    ErrString[length] = 0;'''))
 def swmm(t):
  begin=t.index('char* addAbsolutePath(char* fname)');end=t.index('//=============================================================================',begin)
  t=t[:begin]+helper+'\n'+t[end:]
  t=once(t,'        project_open(f1, f2, f3);\n        getAbsolutePath', '        project_open(f1, f2, f3);\n        if ( ErrorCode ) return report_checkFile();\n        getAbsolutePath')
  # The primary error cannot be replaced by a failed path-resolution follow-up.
  return t
 edit('swmm5.c',swmm)
 def project(t):
  t=once(t,'        sstrncpy(TempDir, s2, MAXFNAME);','        if (!es_makeFileName(TempDir,s2,0))\n            return error_setInpError(ERR_FILE_PATH, "");')
  return once(t,'''    sstrncpy(Finp.name, f1, MAXFNAME);
    sstrncpy(Frpt.name, f2, MAXFNAME);
    sstrncpy(Fout.name, f3, MAXFNAME);''','''    if (!es_makeFileName(Finp.name,f1,0) ||
        !es_makeFileName(Frpt.name,f2,0) ||
        !es_makeFileName(Fout.name,f3,0))
    {
        report_writeErrorMsg(ERR_FILE_PATH, "");
        return;
    }''')
 edit('project.c',project)
 edit('table.c',lambda t:once(t,'''        sstrncpy(fname, tok[2], MAXFNAME);
        sstrncpy(Tseries[j].file.name, addAbsolutePath(fname), MAXFNAME);''','''        if (!es_makeFileName(Tseries[j].file.name,tok[2],1))
            return error_setInpError(ERR_FILE_PATH, "");''').replace('    char fname[MAXFNAME + 1];\n',''))
 edit('climate.c',lambda t:once(t,'''        sstrncpy(fname, tok[1], MAXFNAME);
        sstrncpy(Fclimate.name, addAbsolutePath(fname), MAXFNAME);''','''        if (!es_makeFileName(Fclimate.name,tok[1],1))
            return error_setInpError(ERR_FILE_PATH, "");''').replace('    char  fname[MAXFNAME + 1];\n',''))
 def gage(t):
  t=once(t,'        sstrncpy(fname, tok[5], MAXFNAME);','        if (!es_makeFileName(fname,tok[5],0))\n            return error_setInpError(ERR_FILE_PATH, "");')
  return once(t,'        sstrncpy(Gage[j].fname, addAbsolutePath(fname), MAXFNAME);','        if (!es_makeFileName(Gage[j].fname,fname,1))\n            return error_setInpError(ERR_FILE_PATH, "");')
 edit('gage.c',gage)
 def iface(t):
  # RDII has always used the process working directory; the other interface
  # files use the input document directory. Preserve that public distinction.
  t=once(t,'    sstrncpy(fname, tok[2], MAXFNAME);','    if (!es_makeFileName(fname,tok[2],j != RDII_FILE))\n        return error_setInpError(ERR_FILE_PATH, "");')
  assert t.count('addAbsolutePath(fname)')==6
  return t.replace('addAbsolutePath(fname)','fname')
 edit('iface.c',iface)
 edit('lid.c',lambda t:once(t,'    rptFile = (TLidRptFile *) calloc(1, sizeof(TLidRptFile));','    if (!fname || strlen(fname)>MAXFNAME) return 0;\n    rptFile = (TLidRptFile *) calloc(1, sizeof(TLidRptFile));'))
 def inp(t):
  at=t.index('int input_countObjects()');t=t[:at]+linehelper+t[at:]
  assert t.count('    while ( fgets(line, MAXLINE, Finp.file) != NULL )')==2
  t=t.replace('    while ( fgets(line, MAXLINE, Finp.file) != NULL )','    while ( (es_line_status = es_inputReadLine(line)) != 0 )')
  t=once(t,'    int   sect = -1, newsect;','    int es_line_status;\n    int   sect = -1, newsect;')
  t=once(t,'    int   sect, newsect;','    int es_line_status;\n    int   sect, newsect;')
  assert t.count('        lineCount++;')==2
  t=t.replace('        lineCount++;', '''        lineCount++;
        if (es_line_status<0)
        {
            error_setInpError(ERR_LINE_LENGTH, "");
            report_writeInputErrorMsg(ERR_LINE_LENGTH,sect,line,lineCount);
            ErrorCode=ERR_INPUT;
            return ErrorCode;
        }''')
  return t
 edit('input.c',inp)
 def edit(n,fun):name='src/solver/'+n;contents[name]=fun(contents[name].decode()).encode()
 def objects(t):
  t=once(t,'}  TFile;', '''}  TFile;

/* Immutable input names borrow project-pool storage. Lookup/climate/checkpoint
 * cursors may share these names; only the project pool frees them, after all
 * objects and stream handles have been closed. Global output names keep their
 * fixed transaction-owned buffers. */
typedef struct
{
   char* name;
   char mode;
   char state;
   FILE* file;
} TInputFile;''')
  t=once(t,'   TFile         file;            // external data file','   TInputFile    file;            // external data file, borrowed immutable name')
  return once(t,'   char          fname[MAXFNAME+1]; // name of rainfall data file','   char*         fname;           // immutable name owned by project pool')
 edit('objects.h',objects)
 edit('funcs.h',lambda t:t+'\nint es_storeInputPath(char **dest, const char *source);\n')
 def swmm(t):
  return once(t,'char* addAbsolutePath(char* fname)', '''#include "mempool.h"

/* Called only after the project ID pool is initialized. Failed allocation
 * leaves the previous immutable input name intact and closes normally. */
int es_storeInputPath(char **dest, const char *source)
{
    char name[MAXFNAME+1];
    char *copy;
    size_t length;
    if (!es_makeFileName(name,source,1)) return ERR_FILE_PATH;
    length=strlen(name);
    copy=Alloc((long)length+1);
    if (!copy) return ERR_MEMORY;
    memcpy(copy,name,length+1);
    *dest=copy;
    return 0;
}

char* addAbsolutePath(char* fname)''')
 edit('swmm5.c',swmm)
 def table(t):
  t=once(t,'''        if (!es_makeFileName(Tseries[j].file.name,tok[2],1))
            return error_setInpError(ERR_FILE_PATH, "");''','''        int path_error=es_storeInputPath(&Tseries[j].file.name,tok[2]);
        if (path_error) return error_setInpError(path_error, "");''')
  return once(t,'    table->file.name[0] = 0;','    table->file.name = "";')
 edit('table.c',table)
 edit('gage.c',lambda t:once(t,'''        if (!es_makeFileName(Gage[j].fname,fname,1))
            return error_setInpError(ERR_FILE_PATH, "");''','''        err=es_storeInputPath(&Gage[j].fname,fname);
        if (err) return error_setInpError(err, "");'''))
 edit('project.c',lambda t:once(t,'        sstrncpy(Gage[j].fname, "", 0);','        Gage[j].fname = "";'))
 n='src/solver/input.c';t=contents[n].decode();a=t.index('static int es_inputReadLine(');b=t.index('int input_countObjects()',a);t=t[:a]+INPUT_READER+t[b:]
 oldblock='''            error_setInpError(ERR_LINE_LENGTH, "");
            report_writeInputErrorMsg(ERR_LINE_LENGTH,sect,line,lineCount);
            ErrorCode=ERR_INPUT;'''
 newblock='''            int code=es_line_status==-1 ? ERR_LINE_LENGTH :
                     es_line_status==-2 ? ERR_INPUT_CHAR : ERR_INPUT_READ;
            error_setInpError(code, "");
            report_writeInputErrorMsg(code,sect,line,lineCount);
            ErrorCode=es_line_status==-3 ? ERR_INPUT_READ : ERR_INPUT;'''
 assert t.count(oldblock)==2;t=t.replace(oldblock,newblock)
 a=t.index('        // --- check if max. line length exceeded');b=t.index('        // --- check if at start of a new input section',a);t=t[:a]+t[b:]
 t=t.replace('    char* comment;                // ptr. to start of comment in input line\n','').replace('    int   lineLength;             // number of characters in input line\n','');contents[n]=t.encode()
 n='src/solver/error.h';t=contents[n].decode();assert t.count('      ERR_FILE_PATH            = 364,')==1;contents[n]=t.replace('      ERR_FILE_PATH            = 364,','      ERR_FILE_PATH            = 364,\n      ERR_INPUT_CHAR           = 365,\n      ERR_INPUT_READ           = 366,').encode()
 n='src/solver/error.txt';contents[n]+=b'\nERR(365,"\\n  ERROR 365: embedded zero byte in input record.")\nERR(366,"\\n  ERROR 366: error reading input file.")\n'
 # Platform resolution and the binary physical-record reader supersede the
 # original prototype helpers. Pool changes make failed allocations atomic.
 root = Path(__file__).resolve().parent
 n = 'src/solver/swmm5.c'; t = contents[n].decode()
 a = t.index('int  isRelativePath(const char* fname)')
 b = t.index('char* addAbsolutePath(char* fname)', a)
 t = t[:a] + (root/'paths.c').read_text(encoding='utf-8') + '\n' + t[b:]
 symbol = 'swmm_getEasySewerNativeIOFixes' if custom else 'swmm_getEasySewerStandardFixes'
 revision = 11 if custom else 13
 t = once(t, f'int DLLEXPORT {symbol}(void)\n{{\n    return {revision};\n}}',
             f'int DLLEXPORT {symbol}(void)\n{{\n    return {revision+1};\n}}')
 t += '\nint DLLEXPORT swmm_getEasySewerPathIO(void)\n{\n    return 1;\n}\n'
 # Climate cursors borrow table names from the project pool. A failed fclose
 # reports that name, so every climate borrower must close before pool release.
 t = once(t, '''    if (IsOpenFlag) project_close();
    report_writeSysTime();
    code = climate_closeFile();
    if (!closeCode) closeCode = code;''', '''    code = climate_closeFile();
    if (!closeCode) closeCode = code;
    if (IsOpenFlag) project_close();
    report_writeSysTime();''')
 contents[n] = t.encode()
 n = 'src/solver/input.c'; t = contents[n].decode()
 a = t.index('static int es_inputReadLine('); b = t.index('int input_countObjects()', a)
 contents[n] = (t[:a] + (root/'input.c').read_text(encoding='utf-8') + '\n' + t[b:]).encode()
 n = 'src/solver/project.c'
 contents[n] = once(contents[n].decode(), 'fopen(f1,"rt")', 'fopen(f1,"rb")').encode()
 n = 'src/solver/error.txt'
 contents[n] = once(contents[n].decode(), 'embedded zero byte in input record.',
                    'zero byte or Ctrl+Z in input record.').encode()
 n = 'src/solver/mempool.c'
 contents[n] = (root/'mempool.c').read_bytes()
 # Scratch streams have a single descriptor owner from creation onward.
 n = 'src/solver/swmm5.c'; t = contents[n].decode()
 a = t.index('char* getTempFileName(char* fname)')
 b = t.index('void getElapsedTime(', a)
 t = t[:a] + (root/'scratch.c').read_text(encoding='utf-8') + '\n\n' + t[b:]
 contents[n] = t.encode()
 n = 'src/solver/funcs.h'
 contents[n] = once(contents[n].decode(),
     'char*    getTempFileName(char *s);            // get temporary file name',
     'FILE*    es_openScratchFile(char *s);        // create owned scratch stream').encode()
 n = 'src/solver/project.c'
 contents[n] = once(contents[n].decode(), 'es_makeFileName(TempDir,s2,0)',
                    'es_makeFileName(TempDir,s2,1)').encode()
 n = 'src/solver/output.c'; t = contents[n].decode()
 t = once(t, '        if (!getTempFileName(Fout.name)) { outFail(ERR_OUT_FILE); return; }',
              '        Fout.file = es_openScratchFile(Fout.name);\n'
              '        if (!Fout.file) { outFail(ERR_OUT_FILE); return; }')
 t = once(t, '    if ( (Fout.file = fopen(Fout.name, "w+b")) == NULL)',
              '    if (!Fout.file && (Fout.file = fopen(Fout.name, "w+b")) == NULL)')
 contents[n] = t.encode()
 n = 'src/solver/rain.c'; t = contents[n].decode()
 a = t.index('        if (!getTempFileName(Frain.name))')
 b = t.index('        break;', a)
 t = t[:a] + '''        Frain.file = es_openScratchFile(Frain.name);
        if (!Frain.file)
        {
            report_writeErrorMsg(ERR_RAIN_FILE_SCRATCH, "");
            return;
        }
''' + t[b:]
 contents[n] = t.encode()
 n = 'src/solver/rdii.c'; t = contents[n].decode()
 t = once(t, '''    if (Frdii.mode == SCRATCH_FILE && !getTempFileName(Frdii.name))
        return rdiiFail(ERR_RDII_FILE_SCRATCH);

    // --- open the RDII file as a formatted text file
    Frdii.file = fopen(Frdii.name, "w+b");''', '''    if (Frdii.mode == SCRATCH_FILE)
    {
        Frdii.file = es_openScratchFile(Frdii.name);
        if (!Frdii.file) return rdiiFail(ERR_RDII_FILE_SCRATCH);
    }
    else Frdii.file = fopen(Frdii.name, "w+b");''')
 contents[n] = t.encode()
