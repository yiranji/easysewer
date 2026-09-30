"""Verify the pinned custom source tree and prepare a separate patched build.

The source checkout is read-only. The destination must be empty. This script
does not download code or change the original EasySewerSWMM checkout.
"""

import argparse
import hashlib
import json
from pathlib import Path
import sys
from accounting import patch_accounting

# These exact-source patches are shared by the independently pinned families.
# The source distribution retains native/standard beside this build recipe.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'standard'))
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


def prepare(source, destination):
    source=Path(source).resolve();destination=Path(destination).resolve()
    if destination==source or destination.is_relative_to(source):
        raise ValueError('The build source must be outside the original checkout')
    if destination.exists() and any(destination.iterdir()):
        raise ValueError('The prepared source destination must be empty')
    manifest=json.loads(Path(__file__).with_name('source.json').read_text(encoding='utf-8'))
    contents={}
    for relative,digest in manifest['files'].items():
        raw=(source/relative).read_bytes().replace(b'\r\n',b'\n')
        if hashlib.sha256(raw).hexdigest()!=digest:
            raise ValueError('Source does not match the reviewed tree: '+relative)
        contents[relative]=raw
    name='src/solver/swmm5.c'
    text=contents[name].decode('utf-8').replace('\r\n','\n')
    begin=text.index('double DLLEXPORT swmm_getCurrentTime(void)')
    end=text.index('double DLLEXPORT swmm_getRoutingDuration(void)',begin)
    region=text[begin:end]
    if region.count('return ElapsedTime;')!=1:raise ValueError('Unexpected clock implementation')
    region=region.replace('return ElapsedTime;',
        '// Routing time is independent of control-rule evaluation intervals.\n'
        '    return NewRoutingTime / MSECperDAY;')
    text=text[:begin]+region+text[end:]
    # glibc's checked realpath requires a PATH_MAX-sized caller buffer, while
    # SWMM stores paths in a smaller fixed array. Resolve into allocated memory
    # first and explicitly reject names that the engine cannot represent.
    replacements={
        '            realpath(fname, absPath);': '''            char* resolved = realpath(fname, NULL);
            if (!resolved || strlen(resolved) >= size)
            {
                if (resolved) free(resolved);
                reportAbsolutePathError();
                return;
            }
            strcpy(absPath, resolved);
            free(resolved);''',
        '            GetFullPathName((LPCSTR)fname, (DWORD)size, (LPSTR)absPath, NULL);': '''            DWORD count = GetFullPathNameA(fname, (DWORD)size, absPath, NULL);
            if (count == 0 || count >= size)
            {
                reportAbsolutePathError();
                return;
            }''',
        '        sstrncpy(absPath, fname, strlen(fname));': '''        if (strlen(fname) >= size)
        {
            reportAbsolutePathError();
            return;
        }
        sstrncpy(absPath, fname, size - 1);'''}
    for old,new in replacements.items():
        if text.count(old)!=1:raise ValueError('Unexpected native path implementation')
        text=text.replace(old,new)
    anchor='void getAbsolutePath(const char* fname, char* absPath, size_t size)'
    if text.count(anchor)!=1:raise ValueError('Unexpected absolute path function')
    helper=r'''static void reportAbsolutePathError(void)
{
    report_writeErrorMsg(ERR_INP_FILE, "");
    sstrncpy(ErrorMsg, "\n  ERROR 303: absolute input path cannot be resolved or exceeds native path buffer.", MAXMSG);
    report_writeLine(ErrorMsg);
}

'''
    text=text.replace(anchor,helper+anchor)
    text+='\n// easysewer 2.0 custom ABI: routing clock and discrete removal accounting.\nint DLLEXPORT swmm_getFlexiblePondingAbi(void)\n{\n    return 201;\n}\n'
    contents[name]=text.encode('utf-8')
    header='src/solver/include/swmm5.h'
    text=contents[header].decode('utf-8').replace('\r\n','\n')
    anchor='double DLLEXPORT swmm_getRoutingDuration(void);'
    if text.count(anchor)!=1:raise ValueError('Unexpected API header')
    contents[header]=text.replace(anchor,anchor+'\nint    DLLEXPORT swmm_getFlexiblePondingAbi(void);').encode('utf-8')
    patch_accounting(contents)
    patch_hotstart(contents, buildup_count_fixed=True)
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
    name='src/solver/swmm5.c'
    text=once(contents[name].decode('utf-8'),'    mkstemp(fname);','''    {
        int fd = mkstemp(fname);
        if (fd < 0) return NULL;
        if (close(fd) != 0)
        {
            remove(fname);
            return NULL;
        }
    }''')
    text+='\nint DLLEXPORT swmm_getEasySewerNativeIOFixes(void)\n{\n    return 11;\n}\n'
    contents[name]=text.encode('utf-8')
    checkpoint=patch_checkpoint(contents,custom=True)
    path_io=patch_path_io(contents,custom=True)
    horton_capacity=patch_horton(contents,custom=True)
    horton_outfall=patch_horton_outfall(contents,custom=True)
    recipe={name:hashlib.sha256((Path(__file__).parent/name).read_bytes()).hexdigest()
        for name in ('prepare.py','accounting.py','accounting.c',
                     '../standard/hotstart.py','../standard/lifecycle.py',
                     '../standard/rainfall.py','../standard/rainfall.c',
                     '../standard/runoff_io.py','../standard/runoff_io.c','../standard/runoff_rain.c',
                     '../standard/rdii_io.py','../standard/rdii_io.c',
                     '../standard/routing_io.py','../standard/routing_io.c',
                     '../standard/output_io.py','../standard/output_io.c',
                     '../standard/climate_io.py','../standard/climate_io.c',
                     '../standard/series_io.py','../standard/series_io.c',
                     '../standard/report_io.py','../standard/report_io.c',
                     '../standard/lid_report_io.py','../lid_report/prepare.py','../lid_report/report.inc')}
    for relative,raw in contents.items():
        target=destination/relative;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(raw)
    recipe['../standard/checkpoint.py']=hashlib.sha256((Path(__file__).parent.parent/'standard/checkpoint.py').read_bytes()).hexdigest()
    recipe['../standard/path_io.py']=hashlib.sha256((Path(__file__).parent.parent/'standard/path_io.py').read_bytes()).hexdigest()
    recipe['../standard/horton.py']=hashlib.sha256((Path(__file__).parent.parent/'standard/horton.py').read_bytes()).hexdigest()
    recipe['../standard/horton_outfall.py']=hashlib.sha256((Path(__file__).parent.parent/'standard/horton_outfall.py').read_bytes()).hexdigest()
    evidence=dict(base=manifest,patch='easysewer:flexible-ponding:abi:201',checkpoint=checkpoint,
        native_io_fixes=14,path_io=path_io,horton_capacity=horton_capacity,horton_outfall=horton_outfall,rainfall_fixes=1,runoff_fixes=1,runoff_physics=1,runoff_rain_clock=1,rdii_io=1,routing_io=1,solver_output_io=1,climate_io=1,timeseries_io=1,report_io=1,lid_report_io=1,recipe=recipe,
        files={name:hashlib.sha256(raw).hexdigest() for name,raw in contents.items()})
    (destination/'prepared-source.json').write_text(json.dumps(evidence,indent=2)+'\n',encoding='utf-8')
    return evidence


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',required=True);parser.add_argument('--destination',required=True)
    args=parser.parse_args();evidence=prepare(args.source,args.destination)
    print(json.dumps({'source_files':len(evidence['files']),'patch':evidence['patch']},indent=2))
