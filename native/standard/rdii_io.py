"""Checked sparse RDII interfaces, production arithmetic and file ownership."""

from pathlib import Path
from hotstart import once
from rainfall import replace_function


def patch_rdii(contents):
    name='src/solver/rdii.c';text=contents[name].decode('utf-8')
    text=once(text,'#include <stdlib.h>',
        '#include <stdlib.h>\n#include <stdint.h>\n#include <limits.h>\n#include <float.h>\n#include <errno.h>\n#include <ctype.h>\n#include <math.h>')
    text=once(text,'static void  openRdiiTextFile(void);\n','')
    text=replace_function(text,'void rdii_openRdii()\n','void createRdiiFile()\n','')
    text=once(text,'int rdii_readRdiiInflow(char* tok[], int ntoks)',
        Path(__file__).with_suffix('.c').read_text(encoding='utf-8')+'\n\nint rdii_readRdiiInflow(char* tok[], int ntoks)')
    text=once(text,'if ( !getDouble(tok[2], &a) || a < 0.0 )',
        'if (!getDouble(tok[2], &a) || !isfinite(a) || a < 0.0 || !isfinite(a / UCF(LANDAREA)))')
    text=once(text,'    setUnitHydParams(j, k, m, x);\n    return 0;',
        '    if (!rdiiParamsValid(x)) return error_setInpError(ERR_NUMBER, tok[0]);\n'
        '    setUnitHydParams(j, k, m, x);\n    return 0;')
    text=once(text,'    for ( m=0; m<12; m++)',
        '    UnitHyd[j].rainGage = -1;\n    for ( m=0; m<12; m++)')
    # The original legacy loop invoked the setter before all three R/T/K
    # fields had been assigned, causing reads/casts of uninitialized doubles.
    text=once(text,'''            x[i] = p[3*k + i];
            setUnitHydParams(j, k, m, x);
        }
    }''','''            x[i] = p[3*k + i];
        }
        if (!rdiiParamsValid(x)) return error_setInpError(ERR_NUMBER, tok[0]);
        setUnitHydParams(j, k, m, x);
    }''')
    text=once(text,'        if ( Node[i].rdiiInflow )\n        {\n            // --- check that sewer area',
        '''        if ( Node[i].rdiiInflow )
        {
            j = Node[i].rdiiInflow->unitHyd;
            if (j < 0 || j >= Nobjects[UNITHYD] || UnitHyd[j].rainGage < 0 ||
                UnitHyd[j].rainGage >= Nobjects[GAGE])
            {
                report_writeErrorMsg(ERR_RDII_GAGE, Node[i].ID);
                continue;
            }
            // --- check that sewer area''')
    text=once(text,'    initGageData();\n    if ( ErrorCode ) return;',
        '    if (ErrorCode) return;\n    initGageData();\n    if ( ErrorCode ) return;')
    text=once(text,'            getRainfall(currentDate);',
        '            getRainfall(currentDate);\n            if (ErrorCode) break;')
    text=once(text,'        report_writeErrorMsg(ERR_RDII_FILE_SCRATCH, "");',
        '        if (!ErrorCode) report_writeErrorMsg(ERR_RDII_FILE_SCRATCH, "");')
    text=once(text,'        n = (UnitHyd[i].tBase[m][k] / rainInterval) + 1;',
        '''        if (rainInterval <= 0 || UnitHyd[i].tBase[m][k] / rainInterval > INT_MAX - 2)
        {
            rdiiFail(ERR_MEMORY);
            return 0;
        }
        n = (int)(UnitHyd[i].tBase[m][k] / rainInterval) + 1;''')
    text=once(text,'            n = UHGroup[i].uh[k].maxPeriods;\n            if ( n > 0 )',
        '''            n = UHGroup[i].uh[k].maxPeriods;
            if (ErrorCode || n < 0 || (size_t)n > SIZE_MAX / sizeof(double)) return FALSE;
            if ( n > 0 )''')
    # Windows long is 32-bit. Only comparisons use this duration; saturating
    # at the event-memory horizon preserves all ordinary dry-period decisions.
    text=once(text,'   long      drySeconds;', '   int64_t   drySeconds;')
    text=once(text,'(UHGroup[i].uh[k].maxPeriods * UHGroup[i].rainInterval) + 1;',
        '((int64_t)UHGroup[i].uh[k].maxPeriods * UHGroup[i].rainInterval) + 1;')
    text=once(text,'rainInterval *\n            UHGroup[j].uh[k].maxPeriods',
        '(int64_t)rainInterval *\n            UHGroup[j].uh[k].maxPeriods')
    text=once(text,'        UHGroup[j].uh[k].drySeconds += rainInterval;',
        '''        int64_t limit = (int64_t)rainInterval * UHGroup[j].uh[k].maxPeriods;
        if (UHGroup[j].uh[k].drySeconds < limit)
            UHGroup[j].uh[k].drySeconds += rainInterval;''')
    text=once(text,'            rainInterval * UHGroup[j].uh[k].maxPeriods',
        '            (int64_t)rainInterval * UHGroup[j].uh[k].maxPeriods')
    text=once(text,'        g = UnitHyd[j].rainGage;\n        rainInterval = UHGroup[j].rainInterval;',
        '''        g = UnitHyd[j].rainGage;
        if (g < 0 || g >= Nobjects[GAGE]) continue; /* unbound, unused group */
        rainInterval = UHGroup[j].rainInterval;''')
    text=once(text,'        if ( rdii < ZERO_RDII ) rdii = 0.0;',
        '''        if (!isfinite(rdii) || fabs(rdii) > FLT_MAX)
        {
            rdiiFail(ERR_RDII_FILE_WRITE);
            return FALSE;
        }
        if ( rdii < ZERO_RDII ) rdii = 0.0;''')
    text=once(text,'    else if ( Frdii.mode == NO_FILE ) Frdii.mode = SCRATCH_FILE;',
        '    else if ( Frdii.mode == NO_FILE ) Frdii.mode = SCRATCH_FILE;\n'
        '    if (RdiiStep <= 0) { rdiiFail(ERR_RDII_FILE_FORMAT); return; }')
    text=once(text,'    if ( Frdii.mode == SCRATCH_FILE ) getTempFileName(Frdii.name);',
        '    if (Frdii.mode == SCRATCH_FILE && !getTempFileName(Frdii.name))\n'
        '        return rdiiFail(ERR_RDII_FILE_SCRATCH);')
    text=once(text,'    // --- write file stamp to RDII file\n',
        '    RdiiWriting = TRUE;\n\n    // --- write file stamp to RDII file\n')
    text=once(text,'    fwrite(FileStamp, sizeof(char), strlen(FileStamp), Frdii.file);',
        '    if (!rdiiWrite(FileStamp, sizeof(char), strlen(FileStamp))) return FALSE;')
    for value in ('RdiiStep','NumRdiiNodes'):
        text=once(text,f'    fwrite(&{value}, sizeof(INT4), 1, Frdii.file);',
            f'    if (!rdiiWrite(&{value}, sizeof(INT4), 1)) return FALSE;')
    text=once(text,'        if ( Node[j].rdiiInflow ) fwrite(&j, sizeof(INT4), 1, Frdii.file);',
        '        if (Node[j].rdiiInflow && !rdiiWrite(&j, sizeof(INT4), 1)) return FALSE;')
    text=once(text,'    fwrite(&currentDate, sizeof(DateTime), 1, Frdii.file);\n    fwrite(RdiiNodeFlow, sizeof(REAL4), NumRdiiNodes, Frdii.file);',
        '    if (!rdiiDateValid(currentDate)) { rdiiFail(ERR_RDII_FILE_WRITE); return; }\n'
        '    if (rdiiWrite(&currentDate, sizeof(DateTime), 1))\n'
        '        rdiiWrite(RdiiNodeFlow, sizeof(REAL4), NumRdiiNodes);')
    text=once(text,'    if ( Frdii.file ) fclose(Frdii.file);', '    rdiiCloseFile();')
    text=once(text,'    FREE(RdiiNodeFlow);', '    FREE(RdiiNodeFlow);\n    FREE(RdiiPendingFlow);')
    contents[name]=text.encode('utf-8')
    name='src/solver/error.h';text=contents[name].decode('utf-8')
    text=once(text,'      ERR_RDII_AREA            = 155,',
        '      ERR_RDII_GAGE            = 154,\n      ERR_RDII_AREA            = 155,')
    text=once(text,'      ERR_RDII_FILE_FORMAT     = 345,',
        '      ERR_RDII_FILE_FORMAT     = 345,\n      ERR_RDII_FILE_WRITE      = 346,\n      ERR_RDII_FILE_READ       = 347,')
    contents[name]=text.encode('utf-8')
    name='src/solver/error.txt';text=contents[name].decode('utf-8')
    text=once(text,'ERR(155,"\\n  ERROR 155: invalid sewer area for RDII at node %s.")',
        'ERR(154,"\\n  ERROR 154: missing rain gage for RDII hydrograph at node %s.")\n'
        'ERR(155,"\\n  ERROR 155: invalid sewer area for RDII at node %s.")')
    text=once(text,'ERR(345,"\\n  ERROR 345: invalid format for RDII interface file.")',
        'ERR(345,"\\n  ERROR 345: invalid format for RDII interface file.")\n'
        'ERR(346,"\\n  ERROR 346: error writing RDII interface file %s.")\n'
        'ERR(347,"\\n  ERROR 347: error reading RDII interface file %s.")')
    contents[name]=text.encode('utf-8')
    name='src/solver/swmm5.c';text=contents[name].decode('utf-8')
    text+='\nint DLLEXPORT swmm_getEasySewerRdiiIO(void)\n{\n    return 1;\n}\n'
    contents[name]=text.encode('utf-8')
