"""Prepare an uninstalled LID report I/O candidate on a qualified engine tree."""
import argparse
import hashlib
import json
from pathlib import Path


def patch(contents):
    def replace(name, old, new, count=1):
        name = 'src/solver/' + name
        old, new = old.encode(), new.encode()
        if contents[name].count(old) != count:
            raise ValueError('Unexpected LID report source: ' + name)
        contents[name] = contents[name].replace(old, new)

    replace('lid.h', '// LID Report File\ntypedef struct\n{',
            '// LID Report File\ntypedef struct TLidRptFile\n{')
    replace('lid.h', '    FILE*     file;               // file pointer',
            '    FILE*     file;               // file pointer\n'
            '    char*     name;               // owned diagnostic path\n'
            '    int       dirty, failed;\n'
            '    struct TLidRptFile* dirtyNext;')
    contents['src/solver/lid.h'] += b'''
int lid_writeReport(TLidRptFile *r, const char *format, ...);
int lid_formatReport(TLidRptFile *r, const char *format, ...);
'''
    contents['src/solver/funcs.h'] += b'''
void lid_resetReportFiles(void);
int lid_reportHasFailed(void);
int lid_checkReportFiles(void);
int lid_closeReportFiles(void);
'''
    replace('lid.c', 'static int    createLidRptFile(TLidUnit* lidUnit, char* fname);',
            'static int    createLidRptFile(TLidUnit* lidUnit, char* fname);\n'
            'static int    lid_closeReportFile(TLidRptFile *r);')
    replace('lid.c', '    for (j = 0; j < GroupCount; j++) freeLidGroup(j);',
            '    lid_closeReportFiles();\n'
            '    if (LidGroups) for (j = 0; j < GroupCount; j++) freeLidGroup(j);')
    replace('lid.c', '    for (j = 0; j < LidCount; j++) FREE(LidProcs[j].drainRmvl);',
            '    if (LidProcs) for (j = 0; j < LidCount; j++) FREE(LidProcs[j].drainRmvl);')
    # lid_create reports allocation failure through ErrorCode. controls_create
    # used to overwrite that error; input_readData then dereferenced NULL owners.
    replace('project.c', '    lid_create(Nobjects[LID], Nobjects[SUBCATCH]);',
            '    lid_create(Nobjects[LID], Nobjects[SUBCATCH]);\n'
            '    if ( ErrorCode ) return;')
    replace('project.c', '    createObjects();\n\n    // --- read project data',
            '    createObjects();\n    if ( ErrorCode ) return;\n\n'
            '    // --- read project data')
    replace('project.c', '        for (k = 0; k < Nobjects[LANDUSE]; k++)\n'
            '        {\n            FREE(Subcatch[j].landFactor[k].buildup);',
            '        if (Subcatch[j].landFactor)\n'
            '        for (k = 0; k < Nobjects[LANDUSE]; k++)\n'
            '        {\n            FREE(Subcatch[j].landFactor[k].buildup);')
    replace('lid.c', '            if ( lidUnit->rptFile->file ) fclose(lidUnit->rptFile->file);',
            '            lid_closeReportFile(lidUnit->rptFile);\n'
            '            free(lidUnit->rptFile->name);')
    replace('lid.c', '    rptFile = (TLidRptFile *) malloc(sizeof(TLidRptFile));',
            '    rptFile = (TLidRptFile *) calloc(1, sizeof(TLidRptFile));')
    replace('lid.c', '    lidUnit->rptFile = rptFile;\n    rptFile->file = fopen(fname, "wt");',
            '    lidUnit->rptFile = rptFile;\n'
            '    rptFile->name = malloc(strlen(fname) + 1);\n'
            '    if (!rptFile->name) return 0;\n'
            '    strcpy(rptFile->name, fname);\n'
            '    rptFile->file = fopen(fname, "wt");')
    replace('lid.c', 'fprintf(f,', 'lid_writeReport(lidUnit->rptFile,', 9)
    replace('lidproc.c', 'fprintf(theLidUnit->rptFile->file,',
            'lid_writeReport(theLidUnit->rptFile,', 3)
    replace('lidproc.c', '        snprintf(theLidUnit->rptFile->results, sizeof(theLidUnit->rptFile->results),',
            '        if (lid_formatReport(theLidUnit->rptFile,')
    replace('lidproc.c', '             rptVars[8], rptVars[9], rptVars[10], rptVars[11]);',
            '             rptVars[8], rptVars[9], rptVars[10], rptVars[11]) < 0) return;')
    replace('lidproc.c', '            theLidUnit->rptFile->wasDry++;',
            '            if (theLidUnit->rptFile->wasDry < 2) theLidUnit->rptFile->wasDry++;')
    replace('report.c', '    RptFailed = RptDirty = RptOwnError = 0;',
            '    RptFailed = RptDirty = RptOwnError = 0;\n    lid_resetReportFiles();')
    replace('report.c', '    rptRestoreError();\n    return ErrorCode;',
            '    rptRestoreError();\n    lid_checkReportFiles();\n    return ErrorCode;')
    replace('report.c', '    if (RptFailed && ErrorCode) return;',
            '    if ((RptFailed || lid_reportHasFailed()) && ErrorCode) return;')
    replace('swmm5.c', '    if (IsOpenFlag) project_close();',
            '    code = lid_closeReportFiles();\n'
            '    if (!closeCode) closeCode = code;\n'
            '    if (IsOpenFlag) project_close();')
    contents['src/solver/lid.c'] += b'\n' + Path(__file__).with_name('report.inc').read_bytes()


def prepare(source, destination):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if destination == source or destination.is_relative_to(source):
        raise ValueError('Destination must be outside source')
    if destination.exists() and any(destination.iterdir()):
        raise ValueError('Destination must be empty')
    manifest = source / 'prepared-source.json'
    record = json.loads(manifest.read_bytes())
    if record.get('report_io') != 1 or not (
        record.get('patch') == 'easysewer:standard:5.2.4:11' or
        (record.get('patch') == 'easysewer:flexible-ponding:abi:201' and
         record.get('native_io_fixes') == 9)
    ):
        raise ValueError('Requires qualified standard11 or custom9 source')
    contents = {}
    for name, digest in record['files'].items():
        p = source / name
        if not p.resolve().is_relative_to(source):
            raise ValueError('Source manifest path escapes tree')
        raw = p.read_bytes()
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError('Prepared source changed: ' + name)
        contents[name] = raw
    patch(contents)
    for name, raw in contents.items():
        p = destination / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(raw)
    result = dict(kind='easysewer:lid-report-development', version=3,
                  status='uninstalled candidate; qualification incomplete',
                  base_manifest_sha256=hashlib.sha256(manifest.read_bytes()).hexdigest(),
                  base=record,
                  recipes={name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                           for name in ('prepare.py', 'report.inc')},
                  files={name: hashlib.sha256(raw).hexdigest() for name, raw in contents.items()})
    (destination / 'lid-report-source.json').write_text(json.dumps(result, indent=2) + '\n')
    return result


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source', required=True)
    p.add_argument('--destination', required=True)
    a = p.parse_args()
    print(json.dumps(prepare(a.source, a.destination)['status']))
