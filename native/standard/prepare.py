"""Verify EPA source bytes and prepare the separately named standard patch set."""

import argparse
import hashlib
import json
from pathlib import Path

from hotstart import once, patch_hotstart
from lifecycle import patch_lifecycle
from rainfall import patch_rainfall
from runoff_io import patch_runoff
from rdii_io import patch_rdii
from routing_io import patch_routing
from output_io import patch_output
from climate_io import patch_climate
from series_io import patch_series
from report_io import patch_report
from lid_report_io import patch_lid_report
from checkpoint import patch_checkpoint
from path_io import patch_path_io
from horton import patch_horton
from horton_outfall import patch_horton_outfall


PATCH='easysewer:standard:5.2.4:16'


def prepare(source,destination):
    source=Path(source).resolve();destination=Path(destination).resolve()
    if destination==source or destination.is_relative_to(source):
        raise ValueError('Prepared tree must be outside the original source')
    if destination.exists() and any(destination.iterdir()):
        raise ValueError('Prepared destination must be empty')
    manifest=json.loads(Path(__file__).with_name('source.json').read_text(encoding='utf-8'))
    contents={}
    for name,digest in manifest['files'].items():
        raw=(source/name).read_bytes().replace(b'\r\n',b'\n')
        if hashlib.sha256(raw).hexdigest()!=digest:raise ValueError('Source differs: '+name)
        contents[name]=raw
    patch_hotstart(contents)
    patch_lifecycle(contents)
    patch_rainfall(contents)
    patch_runoff(contents)
    patch_rdii(contents)
    patch_routing(contents)
    patch_output(contents)
    patch_climate(contents)
    patch_series(contents)
    patch_report(contents)
    patch_lid_report(contents)
    name='src/solver/swmm5.c';text=contents[name].decode()
    # Keep standard equations and integration order. Fix native path buffers
    # and close the descriptor reserved by mkstemp before SWMM reopens the file.
    text=once(text,'    mkstemp(fname);','''    {
        int fd = mkstemp(fname);
        if (fd < 0) return NULL;
        if (close(fd) != 0)
        {
            remove(fname);
            return NULL;
        }
    }''')
    text=once(text,'            realpath(fname, absPath);','''            char* resolved = realpath(fname, NULL);
            if (!resolved || strlen(resolved) >= size)
            {
                free(resolved);
                reportAbsolutePathError();
                return;
            }
            strcpy(absPath, resolved);
            free(resolved);''')
    text=once(text,'            GetFullPathName((LPCSTR)fname, (DWORD)size, (LPSTR)absPath, NULL);','''            DWORD count = GetFullPathNameA(fname, (DWORD)size, absPath, NULL);
            if (count == 0 || count >= size)
            {
                reportAbsolutePathError();
                return;
            }''')
    text=once(text,'        sstrncpy(absPath, fname, strlen(fname));','''        if (strlen(fname) >= size)
        {
            reportAbsolutePathError();
            return;
        }
        sstrncpy(absPath, fname, size - 1);''')
    text=once(text,'void getAbsolutePath(const char* fname, char* absPath, size_t size)',r'''static void reportAbsolutePathError(void)
{
    report_writeErrorMsg(ERR_INP_FILE, "");
    sstrncpy(ErrorMsg, "\n  ERROR 303: input path cannot be resolved or exceeds native path buffer.", MAXMSG);
    report_writeLine(ErrorMsg);
}

void getAbsolutePath(const char* fname, char* absPath, size_t size)''')
    text+='\nint DLLEXPORT swmm_getEasySewerStandardFixes(void)\n{\n    return 13;\n}\n'
    contents[name]=text.encode()
    checkpoint=patch_checkpoint(contents)
    path_io=patch_path_io(contents)
    horton_capacity=patch_horton(contents)
    horton_outfall=patch_horton_outfall(contents)
    for name,raw in contents.items():
        path=destination/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw)
    recipe={name:hashlib.sha256((Path(__file__).parent/name).read_bytes()).hexdigest()
            for name in ('prepare.py','hotstart.py','lifecycle.py','rainfall.py','rainfall.c','runoff_io.py','runoff_io.c','runoff_rain.c','rdii_io.py','rdii_io.c','routing_io.py','routing_io.c','output_io.py','output_io.c','climate_io.py','climate_io.c','series_io.py','series_io.c','report_io.py','report_io.c','lid_report_io.py','../lid_report/prepare.py','../lid_report/report.inc')}
    recipe['checkpoint.py']=hashlib.sha256(Path(__file__).with_name('checkpoint.py').read_bytes()).hexdigest()
    recipe['path_io.py']=hashlib.sha256(Path(__file__).with_name('path_io.py').read_bytes()).hexdigest()
    recipe['horton.py']=hashlib.sha256(Path(__file__).with_name('horton.py').read_bytes()).hexdigest()
    recipe['horton_outfall.py']=hashlib.sha256(Path(__file__).with_name('horton_outfall.py').read_bytes()).hexdigest()
    evidence=dict(base=manifest,patch=PATCH,recipe=recipe,checkpoint=checkpoint,path_io=path_io,horton_capacity=horton_capacity,horton_outfall=horton_outfall,rainfall_fixes=1,runoff_fixes=1,runoff_physics=1,runoff_rain_clock=1,rdii_io=1,routing_io=1,solver_output_io=1,climate_io=1,timeseries_io=1,report_io=1,lid_report_io=1,
        files={name:hashlib.sha256(raw).hexdigest() for name,raw in contents.items()})
    (destination/'prepared-source.json').write_text(json.dumps(evidence,indent=2)+'\n',encoding='utf-8')
    return evidence


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',required=True);parser.add_argument('--destination',required=True)
    args=parser.parse_args();record=prepare(args.source,args.destination)
    print(json.dumps(dict(patch=record['patch'],files=len(record['files']))))
