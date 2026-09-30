"""Correct the documented Modified Horton excess-capacity bound.

EPA Hydrology Reference Manual section 4.3.3, printed page 103, step 5 uses
MIN for the excess infiltration state. No flux equation or state layout changes.
Apply after the qualified path patch (standard 14 / custom 12).
"""
import hashlib
from pathlib import Path

from hotstart import once


def _record():
    return dict(revision=1, recipe_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())


def patch_horton(contents, *, custom=False):
    name='src/solver/infil.c'
    text=contents[name].decode('utf-8')
    anchor='        if ( infil->Fmax > 0.0 ) infil->Fe = MAX(infil->Fe, infil->Fmax);'
    contents[name]=once(text,anchor,anchor.replace('MAX(', 'MIN(')).encode('utf-8')
    name='src/solver/swmm5.c';text=contents[name].decode('utf-8')
    marker='swmm_getEasySewerNativeIOFixes' if custom else 'swmm_getEasySewerStandardFixes'
    old=12 if custom else 14
    text=once(text,f'int DLLEXPORT {marker}(void)\n{{\n    return {old};\n}}',
                   f'int DLLEXPORT {marker}(void)\n{{\n    return {old+1};\n}}')
    text+='\nint DLLEXPORT swmm_getEasySewerHortonCapacity(void)\n{\n    return 1;\n}\n'
    contents[name]=text.encode('utf-8')
    return _record()


def validate_horton(record):
    if record!=_record():
        raise ValueError('Prepared Horton capacity revision or source recipe differs')
