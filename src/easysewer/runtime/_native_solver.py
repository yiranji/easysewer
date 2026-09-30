"""Trusted SWMM C ABI adapter, imported only inside the isolated worker."""

import ctypes as c
import hashlib
import math
from pathlib import Path
import platform
import struct

from .backend import NativeFailure
from ..model.identity import canonical_key

OBJECTS = {'swmm:raingages': 0, 'swmm:subcatchments': 1, 'swmm:nodes': 2, 'swmm:links': 3}


class NativeCallFailure(Exception):
    def __init__(self, stage, code, message):
        self.failure = NativeFailure(stage=stage, code=code, message=message)
        super().__init__(message)


class NativeSolver:
    def __init__(self, library, *, expected_sha256=None):
        self.open_attempted = False
        self.start_attempted = False
        self.ended = False
        self.closed = False
        path = Path(library).resolve(strict=True)
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        if expected_sha256 and before != expected_sha256:
            raise ValueError('Native library SHA-256 does not match the selected artifact')
        self.lib = c.CDLL(str(path))
        if type(self) is NativeSolver and hasattr(self.lib,'swmm_execRouting'):
            raise ValueError('A custom routing library requires its explicit backend and output semantics')
        for name, args, result in (
            ('open', [c.c_char_p]*3, c.c_int), ('start', [c.c_int], c.c_int),
            ('step', [c.POINTER(c.c_double)], c.c_int), ('end', [], c.c_int),
            ('report', [], c.c_int), ('close', [], c.c_int), ('getVersion', [], c.c_int),
            ('getError', [c.c_char_p,c.c_int], c.c_int), ('getWarnings', [], c.c_int),
            ('getCount', [c.c_int], c.c_int), ('getName', [c.c_int,c.c_int,c.c_char_p,c.c_int], None),
            ('getIndex', [c.c_int,c.c_char_p], c.c_int), ('getValue', [c.c_int,c.c_int], c.c_double),
            ('getMassBalErr', [c.POINTER(c.c_float)]*3, c.c_int),
        ):
            function = getattr(self.lib, 'swmm_'+name)
            function.argtypes = args; function.restype = result
        self.version = self.lib.swmm_getVersion()
        if hashlib.sha256(path.read_bytes()).hexdigest() != before:
            raise ValueError('Native library changed while it was being loaded')
        self.metadata = dict(library=str(path), sha256=before, engine_version=self.version,
            platform=platform.system(), architecture=platform.machine(), abi=f'cdecl:{struct.calcsize("P")*8}')

    def check(self, stage, code):
        if code:
            message = c.create_string_buffer(4096)
            retained_code = self.lib.swmm_getError(message, len(message))
            # Engine messages may contain paths/IDs from the UTF-8 input.
            # A byte escape preserves an undecodable native diagnostic.
            detail = message.value.decode('utf-8',errors='backslashreplace')
            # Closing can fail after a primary error. The C engine retains the
            # primary diagnostic, while close returns its own failure code.
            if retained_code and retained_code != code:
                detail = f'Native returned {code}; retained engine error {retained_code}: {detail}'
            raise NativeCallFailure(stage, int(code), detail or f'Native returned {code}')

    def open(self, paths):
        self.open_attempted = True
        self.check('open', self.lib.swmm_open(*(str(path).encode('utf-8') for path in paths)))
        groups = []
        for collection, kind in OBJECTS.items():
            count = self.lib.swmm_getCount(kind)
            if not 0 <= count <= 10_000_000:
                raise ValueError('Invalid native object count')
            names = []
            for index in range(count):
                name = c.create_string_buffer(256)
                self.lib.swmm_getName(kind,index,name,len(name))
                decoded = name.value.decode('utf-8',errors='strict')
                if not decoded or self.lib.swmm_getIndex(kind,name.value) != index:
                    raise ValueError('Native name/index verification failed')
                names.append(decoded)
            if len({canonical_key(name) for name in names}) != len(names):
                raise ValueError('Native object names are not unique')
            groups.append((collection,names))
        return {'groups':groups, 'warnings':self.lib.swmm_getWarnings(), 'flow_units':int(self.lib.swmm_getValue(8,0))}

    def start(self, save_results):
        # Native sets IsStartedFlag before operations which can fail. Always
        # attempt end() after a partial start, even if start() returned an error.
        self.start_attempted = True
        self.check('start',self.lib.swmm_start(int(save_results)))

    def step(self, max_steps=1):
        elapsed = c.c_double()
        for index in range(max_steps):
            self.check('step',self.lib.swmm_step(c.byref(elapsed)))
            if not math.isfinite(elapsed.value) or elapsed.value < 0:
                raise ValueError('Native returned invalid elapsed time')
            if elapsed.value == 0:
                return {'elapsed_days':0.,'finished':True,'steps':index+1}
        return {'elapsed_days':elapsed.value,'finished':False,'steps':max_steps}

    def end(self):
        self.ended = True
        self.check('end',self.lib.swmm_end())
        values = [c.c_float() for _ in range(3)]
        self.check('mass_balance',self.lib.swmm_getMassBalErr(*(c.byref(value) for value in values)))
        if not all(math.isfinite(value.value) for value in values):
            raise ValueError('Native returned nonfinite continuity errors')
        return dict(zip(('runoff_percent','flow_percent','quality_percent'),(value.value for value in values)))

    def report(self):
        self.check('report',self.lib.swmm_report())

    def cleanup(self, on_error=None):
        issues = []
        if self.closed:
            return issues
        self.closed = True
        actions = []
        if self.start_attempted and not self.ended:
            actions.append(('end',self.lib.swmm_end))
        if self.open_attempted:
            actions.append(('close',self.lib.swmm_close))
        for stage, action in actions:
            before = len(issues)
            try:
                self.check(stage,action())
            except NativeCallFailure as error:
                issues.append(error.failure)
            except BaseException as error:
                issues.append(NativeFailure(stage=stage,code=None,message=f'{type(error).__name__}: {error}'))
            if on_error and len(issues)>before:
                on_error(issues[-1])
        return issues
