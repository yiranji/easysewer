"""Apply qualified Modified Horton state and outfall gate reader corrections.

Apply after Horton capacity revision 1 (standard 15 / custom native I/O 13).
State layouts and checkpoint ABI stay unchanged; numerical identities advance.
"""
import hashlib
from pathlib import Path

from hotstart import once


def _record():
    return dict(revision=1, horton_state=1, outfall_gate=1,
        recipe_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())


def patch_horton_outfall(contents, *, custom=False):
    name='src/solver/infil.c';text=contents[name].decode('utf-8')
    start=text.index('\ndouble modHorton_getInfil(')
    end=text.index('\nvoid grnampt_getParams(',start)
    body=text[start:end]
    shortcut='''    if ( df == 0.0 || kd == 0.0 )
    {
        fp = f0;
        fa = irate + depth / tstep;
        if ( fp > fa ) fp = fa;
        return MAX(0.0, fp);
    }
'''
    body=once(body,shortcut,'')
    body=once(body,'// --- special cases of no or constant infiltration',
                   '// --- reject invalid infiltration parameters')
    contents[name]=(text[:start]+body+text[end:]).encode('utf-8')
    name='src/solver/node.c';text=contents[name].decode('utf-8')
    anchor='    if ( ntoks == n )\n    {\n        m = findmatch(tok[n-1], NoYesWords);'
    contents[name]=once(text,anchor,anchor.replace('ntoks == n','ntoks >= n')).encode('utf-8')
    name='src/solver/swmm5.c';text=contents[name].decode('utf-8')
    marker='swmm_getEasySewerNativeIOFixes' if custom else 'swmm_getEasySewerStandardFixes'
    old=13 if custom else 15
    text=once(text,f'int DLLEXPORT {marker}(void)\n{{\n    return {old};\n}}',
                   f'int DLLEXPORT {marker}(void)\n{{\n    return {old+1};\n}}')
    for marker in ('swmm_getEasySewerHortonState','swmm_getEasySewerOutfallGate'):
        text+=f'\nint DLLEXPORT {marker}(void)\n{{\n    return 1;\n}}\n'
    contents[name]=text.encode('utf-8')
    return _record()


def validate_horton_outfall(record):
    if record!=_record():
        raise ValueError('Prepared Horton state/outfall gate revision or recipe differs')
