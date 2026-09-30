"""Private checkpoint ABI adapter; no test exports or cross-runtime FILEs.

The caller serializes solver calls and binds a verified complete execution
identity, immutable inputs, state and output prefixes in an outer container.
Providers create/resolve private verified resources and own path cleanup. This
module is not a public Session/Runner resume API or an integrity container.
"""
import ctypes as c
from dataclasses import dataclass
import os

from .backend import NativeFailure

INPUT = c.CFUNCTYPE(c.c_int, c.c_void_p, c.c_char_p, c.c_void_p, c.c_size_t)
OUTPUT = c.CFUNCTYPE(c.c_int, c.c_void_p, c.c_int, c.c_uint32, c.c_int,
                    c.c_uint64, c.c_void_p, c.c_size_t)


class CheckpointError(RuntimeError):
    def __init__(self, operation, code):
        self.operation, self.code = operation, code
        super().__init__(f'Native checkpoint {operation} failed ({code})')


@dataclass(frozen=True)
class RestoreResult:
    error: int
    committed: bool
    cleanup_error: int
    provider_errors: tuple[NativeFailure, ...]


@dataclass(frozen=True)
class InputResource:
    index: int
    identity: str
    initial_path: str


@dataclass(frozen=True)
class OutputResource:
    index: int
    role: int
    text: bool
    size: int
    path: str


class NativeCheckpoint:
    def __init__(self, solver, binding, *, max_state_bytes=512 * 1024 * 1024):
        if type(binding) is not bytes or len(binding) != 32:
            raise ValueError('Checkpoint requires a 32-byte execution identity digest')
        if type(max_state_bytes) is not int or max_state_bytes <= 0:
            raise ValueError('Checkpoint size limit must be a positive integer')
        self.solver, self.lib = solver, solver.lib
        self.binding, self.max_state_bytes = binding, max_state_bytes
        for name, args in (
            ('Version', []),
            ('Capture', [c.c_void_p, c.c_void_p, c.c_size_t, c.POINTER(c.c_size_t)]),
            ('Validate', [c.c_void_p, c.c_void_p, c.c_size_t]),
            ('Restore', [c.c_void_p,c.c_void_p,c.c_size_t,INPUT,OUTPUT,c.c_void_p,
                         c.POINTER(c.c_int),c.POINTER(c.c_int)]),
            ('InputCount', [c.POINTER(c.c_uint32)]),
            ('InputInfo', [c.c_uint32,c.c_void_p,c.c_size_t,c.c_void_p,c.c_size_t]),
            ('OutputCount', [c.POINTER(c.c_uint32)]),
            ('OutputInfo', [c.c_uint32,c.POINTER(c.c_int),c.POINTER(c.c_int),
                            c.POINTER(c.c_uint64),c.c_void_p,c.c_size_t]),
        ):
            function = getattr(self.lib, 'swmm_checkpoint' + name)
            function.argtypes, function.restype = args, c.c_int
        if self.lib.swmm_checkpointVersion() != 2:
            raise ValueError('Unsupported native checkpoint ABI')

    @staticmethod
    def _check(operation, code):
        if code:
            raise CheckpointError(operation, code)

    def _payload(self, payload):
        if type(payload) is not bytes or not 0 < len(payload) <= self.max_state_bytes:
            raise ValueError('Invalid checkpoint state bytes or size limit exceeded')
        return c.create_string_buffer(payload)

    def capture(self):
        size = c.c_size_t()
        self._check('measure', self.lib.swmm_checkpointCapture(self.binding, None, 0, c.byref(size)))
        if not 0 < size.value <= self.max_state_bytes:
            raise ValueError('Native checkpoint exceeds the configured state size limit')
        data = c.create_string_buffer(size.value)
        self._check('capture', self.lib.swmm_checkpointCapture(self.binding, data, len(data), c.byref(size)))
        if size.value != len(data):
            raise ValueError('Native checkpoint layout changed during capture')
        return data.raw

    def validate(self, payload):
        raw = self._payload(payload)
        self._check('validate', self.lib.swmm_checkpointValidate(self.binding, raw, len(payload)))

    def inputs(self):
        count = c.c_uint32()
        self._check('input inventory', self.lib.swmm_checkpointInputCount(c.byref(count)))
        result = []
        for index in range(count.value):
            identity, path = c.create_string_buffer(4096), c.create_string_buffer(4096)
            self._check('input info', self.lib.swmm_checkpointInputInfo(index, identity, len(identity), path, len(path)))
            result.append(InputResource(index, os.fsdecode(identity.value), os.fsdecode(path.value)))
        return tuple(result)

    def outputs(self):
        count = c.c_uint32()
        self._check('output inventory', self.lib.swmm_checkpointOutputCount(c.byref(count)))
        result = []
        for index in range(count.value):
            role, text, size = c.c_int(), c.c_int(), c.c_uint64()
            path = c.create_string_buffer(4096)
            self._check('output info', self.lib.swmm_checkpointOutputInfo(
                index, c.byref(role), c.byref(text), c.byref(size), path, len(path)))
            if text.value not in (0, 1) or not 0 <= role.value <= 5:
                raise ValueError('Invalid native output resource descriptor')
            result.append(OutputResource(index, role.value, bool(text.value), size.value, os.fsdecode(path.value)))
        return tuple(result)

    def restore(self, payload, *, input_provider, output_provider):
        if not callable(input_provider) or not callable(output_provider):
            raise TypeError('Checkpoint requires input and output resource providers')
        raw = self._payload(payload)
        failures, interrupts = [], []

        def resolve(operation, provider, args, destination, capacity):
            try:
                path = os.fsencode(provider(*args))
                if not path or b'\0' in path or len(path) >= capacity:
                    raise ValueError('Invalid native checkpoint resource path')
                c.memmove(destination, path + b'\0', len(path) + 1)
                return 0
            except BaseException as error:
                failures.append(NativeFailure(stage=operation, code=None,
                                              message=f'{type(error).__name__}: {error}'))
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    interrupts.append(error)
                return 7

        read = INPUT(lambda context, identity, dest, capacity:
                     resolve('checkpoint_input', input_provider, (os.fsdecode(identity),), dest, capacity))
        write = OUTPUT(lambda context, role, index, text, size, dest, capacity:
                       resolve('checkpoint_output', output_provider,
                               (role, index, bool(text), size), dest, capacity))
        committed, cleanup = c.c_int(), c.c_int()
        error = self.lib.swmm_checkpointRestore(self.binding, raw, len(payload), read, write,
                                                None, c.byref(committed), c.byref(cleanup))
        result = RestoreResult(error, bool(committed.value), cleanup.value, tuple(failures))
        if interrupts:
            interrupts[0].checkpoint_result = result
            raise interrupts[0]
        return result
