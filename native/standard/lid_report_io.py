"""Share the qualified detailed-report patch between both native families."""
from pathlib import Path
import runpy


def patch_lid_report(contents):
    # Keep one implementation: the separate development recipe produced the
    # qualified candidate. Its CLI does not run through run_path.
    recipe = Path(__file__).resolve().parent.parent / 'lid_report' / 'prepare.py'
    runpy.run_path(str(recipe))['patch'](contents)
    contents['src/solver/swmm5.c'] += (
        b'\nint DLLEXPORT swmm_getEasySewerLidReportIO(void)\n{\n    return 1;\n}\n')
