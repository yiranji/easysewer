"""Apply the ordered hydraulic acceleration patch to Standard revision 16."""

import hashlib
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parent
BASE_PATCH = 'easysewer:standard:5.2.4:16'
PATCH = 'easysewer:accelerated:5.2.4:1'
VERSION = 1
BASE_DYNWAVE_SHA256 = 'fb75ab9f8cf94a1c62d7e39b596cbb011d44828bf0b223bc156c824fff62e450'
DYNWAVE_SHA256 = '3abe56b90f5950ad50272010aae660db190cab359d951ada1871c44376c5cf9d'
PROTOTYPE_SHA256 = '0718729000122cbc9cc8010cfea9842d2c251c514fcc5c3219b1d99998b6b4e4'
RECIPE_FILES = ('patch.py', 'dynwave.patch')
MARKER = b'\nint DLLEXPORT swmm_getEasySewerAccelerationVersion(void)\n{\n    return 1;\n}\n'
FEATURES = (
    'immutable non-conduit and outfall scan lists in original link order',
    'node-owned conduit gathering in original per-node arithmetic order',
    'four local double accumulators with unchanged arithmetic expressions',
    'pure-conduit gather fused into the existing node-depth loop',
    'portable forced-inline annotation for conduit gathering',
    'optional overflow-checked topology caches with original-algorithm fallback',
    'unchanged model-requested and EPA-effective thread selection',
)


def sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def acceleration_record():
    """Return the exact source-policy identity required by the build recipe."""
    return dict(version=VERSION, base_patch=BASE_PATCH, patch=PATCH,
                base_dynwave_sha256=BASE_DYNWAVE_SHA256,
                dynwave_sha256=DYNWAVE_SHA256,
                dynwave_hash_normalization='CRLF to LF before patching; LF output',
                validated_prototype_sha256=PROTOTYPE_SHA256,
                features=list(FEATURES),
                recipes={name: sha256((ROOT/name).read_bytes()) for name in RECIPE_FILES})


def _apply_dynwave_patch(raw):
    """Apply one file's unified diff without fuzzy matching or external tools."""
    # Standard appends checkpoint fragments as bytes; their checkout EOL can
    # differ by host. Normalize this changed file just as EPA input is normalized.
    raw = raw.replace(b'\r\n', b'\n')
    if sha256(raw) != BASE_DYNWAVE_SHA256:
        raise ValueError('Expected unchanged Standard revision 16 dynwave.c')
    original = raw.decode('utf-8').splitlines(keepends=True)
    diff = (ROOT/'dynwave.patch').read_text(encoding='utf-8').splitlines(keepends=True)
    if diff[:2] != ['--- standard/src/solver/dynwave.c\n',
                    '+++ accelerated/src/solver/dynwave.c\n']:
        raise ValueError('Unexpected accelerated patch target')
    result, cursor, index = [], 0, 2
    while index < len(diff):
        match = re.fullmatch(r'@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@[^\n]*\n', diff[index])
        if match is None:
            raise ValueError('Unexpected accelerated patch hunk')
        old_start, old_count, new_start, new_count = (
            int(value) if value is not None else 1 for value in match.groups())
        position = old_start - 1 if old_count else old_start
        if position < cursor or position > len(original):
            raise ValueError('Accelerated patch hunks overlap or exceed source')
        result.extend(original[cursor:position])
        if len(result) != (new_start - 1 if new_count else new_start):
            raise ValueError('Accelerated patch new-line position differs')
        index += 1
        old_lines, new_lines = [], []
        while index < len(diff) and not diff[index].startswith('@@ '):
            line = diff[index]
            if not line or line[0] not in ' +-':
                raise ValueError('Unsupported accelerated patch line')
            if line[0] in ' -':
                old_lines.append(line[1:])
            if line[0] in ' +':
                new_lines.append(line[1:])
            index += 1
        if len(old_lines) != old_count or len(new_lines) != new_count:
            raise ValueError('Accelerated patch hunk length differs')
        if original[position:position + old_count] != old_lines:
            raise ValueError('Accelerated patch context differs')
        result.extend(new_lines)
        cursor = position + old_count
    result.extend(original[cursor:])
    transformed = ''.join(result).encode('utf-8')
    if sha256(transformed) != DYNWAVE_SHA256:
        raise ValueError('Accelerated dynwave.c does not match the pinned transformation')
    return transformed


def patch(contents):
    """Change only dynwave.c and append the backend identity export to swmm5.c."""
    dynwave = _apply_dynwave_patch(contents['src/solver/dynwave.c'])
    solver = contents['src/solver/swmm5.c']
    standard_marker = b'int DLLEXPORT swmm_getEasySewerStandardFixes(void)\n{\n    return 16;\n}'
    if solver.count(standard_marker) != 1 or b'swmm_getEasySewerAccelerationVersion' in solver:
        raise ValueError('Expected exactly one Standard revision 16 identity export')
    contents['src/solver/dynwave.c'] = dynwave
    contents['src/solver/swmm5.c'] = solver + MARKER
    return acceleration_record()


def validate_acceleration(record):
    if record != acceleration_record():
        raise ValueError('Prepared acceleration policy or source recipe differs')
