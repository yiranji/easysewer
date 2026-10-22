"""Mark ctypes payloads as platform-specific and package only the selected ABI.

The explicit pure profile is built in a separate staging directory without
this file. Native release wheels are built and repaired by the tools under
tools/; an ordinary source build never advertises a manylinux baseline.
"""
import json
import os
from pathlib import Path
import platform
import shutil
import struct
import sysconfig

from setuptools import setup
from setuptools.dist import Distribution
from setuptools.command.build_py import build_py
from setuptools.command.bdist_wheel import bdist_wheel


TARGETS = {
    'windows-x86_64': ('win', 'win_amd64'),
    'linux-x86_64': ('linux', 'linux_x86_64'),
    'linux-aarch64': ('linux-aarch64', 'linux_aarch64'),
    'darwin-arm64': ('macos-arm64', 'macosx_14_0_arm64'),
}


def selected_target():
    system = platform.system()
    machine = platform.machine().lower()
    if struct.calcsize('P') != 8:
        raise RuntimeError('Native wheels require a supported 64-bit Python ABI')
    if system == 'Windows':
        target = 'windows-x86_64' if sysconfig.get_platform() == 'win-amd64' else None
    elif system == 'Linux':
        architecture = {'amd64': 'x86_64', 'x86_64': 'x86_64',
                        'aarch64': 'aarch64', 'arm64': 'aarch64'}.get(machine)
        target = 'linux-' + architecture if architecture else None
    elif system == 'Darwin' and machine in ('arm64', 'aarch64'):
        target = 'darwin-arm64'
    else:
        target = None
    if target not in TARGETS:
        raise RuntimeError('No native wheel target for this Python ABI; use the explicit pure profile')
    if os.environ.get('EASYSEWER_NATIVE_TARGET', target) != target:
        raise RuntimeError('The selected native wheel target differs from the build interpreter ABI')
    return target


class NativeBuild(build_py):
    def run(self):
        super().run()
        target = selected_target()
        libraries = Path(self.build_lib) / 'easysewer' / 'libs'
        manifest_path = libraries / 'runtime-manifest.json'
        if not manifest_path.is_file():
            raise RuntimeError('Native source build requires a verified runtime manifest; '
                               'use tools/build_native_wheel.py to build the platform payloads')
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        if manifest.get('schema') != 'easysewer:native-runtime:1' or target not in manifest.get('targets', {}):
            raise RuntimeError('This source tree has no native artifacts for ' + target +
                               '; use tools/build_native_wheel.py with the pinned upstream source')
        selected = manifest['targets'][target]
        for name, record in selected['libraries'].items():
            path = Path(self.build_lib) / 'easysewer' / record['path']
            if not path.is_file():
                raise RuntimeError('Selected native artifact is absent: ' + name)
        keep = {TARGETS[target][0], 'licenses'}
        for path in libraries.iterdir():
            if path.is_dir() and path.name not in keep:
                shutil.rmtree(path)
        manifest['targets'] = {target: selected}
        manifest_path.write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')


class NativeWheel(bdist_wheel):
    def finalize_options(self):
        super().finalize_options()
        self.root_is_pure = False
        self.plat_name = TARGETS[selected_target()][1]

    def get_tag(self):
        return 'py3', 'none', self.plat_name


class NativeDistribution(Distribution):
    def has_ext_modules(self):
        # ctypes libraries do not use CPython's extension ABI, but they belong
        # in platlib rather than a universal purelib wheel.
        return True


setup(distclass=NativeDistribution,
      cmdclass={'build_py': NativeBuild, 'bdist_wheel': NativeWheel})
