"""Build the declared pure Python profile without loading native code.

Requires setuptools and wheel in --python (default: this interpreter).
Never edits the source package or replaces an existing output directory.
"""
from pathlib import Path, PurePosixPath
import argparse, ast, fnmatch, hashlib, json, re, shutil, subprocess, sys, tempfile, zipfile


def relative(value):
    if not isinstance(value, str) or "\\" in value or ":" in value:
        raise ValueError("Profile paths must be portable relative paths")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(v in ("", ".", "..") for v in value.split("/")):
        raise ValueError("Profile path escapes the project")
    return Path(*path.parts)


def build(project, output, python=sys.executable):
    project, output = Path(project).resolve(), Path(output).resolve()
    profile_path = project / "packaging_profile.json"
    config = json.loads(profile_path.read_text(encoding="utf-8"))
    profile = config["profiles"]["pyodide"]
    selected = {}
    def add(target, origin):
        target, origin = relative(target), relative(origin)
        if target.parts[:2] != ("src", "easysewer") or target.suffix != ".py":
            raise ValueError("Pure profile targets must be package Python modules")
        source = (project / origin).resolve()
        if not source.is_relative_to(project) or not source.is_file():
            raise ValueError("Profile source is missing or outside the project: " + str(origin))
        name = target.relative_to("src").as_posix()
        if name in selected:
            raise ValueError("Duplicate pure profile target: " + name)
        raw = source.read_bytes()
        tree = ast.parse(raw.decode("utf-8-sig"), feature_version=(3, 10))
        # Inspect executable syntax: documentation may describe unavailable APIs.
        if set(profile["forbidden_symbols"]) != {"ctypes.CDLL", "from ctypes import CDLL", "CDLL("}:
            raise ValueError("Unsupported forbidden-symbol policy")
        for node in ast.walk(tree):
            imported = (isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "ctypes"
                        and any(v.name in ("CDLL", "*") for v in node.names))
            direct = isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "CDLL"
            attribute = isinstance(node, ast.Attribute) and node.attr == "CDLL"
            if imported or direct or attribute:
                raise ValueError("Forbidden native operation in " + name)
        selected[name] = (origin.as_posix(), raw)
    for name in profile["include_modules"]:
        if any(fnmatch.fnmatchcase(name, pattern) for pattern in profile["exclude_paths"]):
            raise ValueError("Included module also excluded: " + name)
        add(name, name)
    for replacement in profile["replace_with_stub"]:
        if replacement["target"] not in profile["exclude_paths"]:
            raise ValueError("A stub must explicitly replace an excluded module")
        add(replacement["target"], replacement["stub_module"])
    if output.exists():
        raise FileExistsError("Output directory already exists: " + str(output))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.mkdir()
    with tempfile.TemporaryDirectory(prefix=".easysewer-pure-", dir=output.parent) as temporary:
        stage = Path(temporary)
        for name, (_, raw) in selected.items():
            target = stage / "src" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
        metadata = (project / "pyproject.toml").read_text(encoding="utf-8")
        metadata, count = re.subn(r"(?ms)^\[tool\.setuptools\.package-data\]\n.*?(?=^\[|\Z)", "", metadata)
        if count != 1:
            raise ValueError("Expected one native package-data table")
        (stage / "pyproject.toml").write_text(metadata, encoding="utf-8")
        shutil.copyfile(project / "README.md", stage / "README.md")
        command = [str(python), "-I", "-B", "-c", "import sys; from setuptools.build_meta import build_wheel; print(build_wheel(sys.argv[1]))", str(output)]
        result = subprocess.run(command, cwd=stage, capture_output=True, text=True)
        (output / "build.log").write_text(result.stdout + result.stderr, encoding="utf-8")
        if result.returncode:
            raise RuntimeError("Pure wheel build failed; see " + str(output / "build.log"))
    wheel, = output.glob("*.whl")
    with zipfile.ZipFile(wheel) as archive:
        members = [n for n in archive.namelist() if n.startswith("easysewer/") and not n.endswith("/")]
        if set(members) != set(selected):
            raise ValueError("Built wheel differs from declared pure modules")
        for name, (_, raw) in selected.items():
            if archive.read(name) != raw:
                raise ValueError("Built module bytes changed: " + name)
        for name in archive.namelist():
            if any(fnmatch.fnmatchcase(name, pattern) for pattern in config["validation"]["forbidden_globs"]):
                raise ValueError("Native artifact in pure wheel: " + name)
            if any(name.startswith(prefix) for prefix in config["validation"]["forbidden_dirs"]):
                raise ValueError("Forbidden directory in pure wheel: " + name)
        metadata_name, = [n for n in archive.namelist() if n.endswith(".dist-info/WHEEL")]
        if "Root-Is-Purelib: true" not in archive.read(metadata_name).decode() or not wheel.name.endswith("-py3-none-any.whl"):
            raise ValueError("Wheel is not pure Python")
    digest = lambda raw: hashlib.sha256(raw).hexdigest()
    record = dict(wheel=str(wheel), sha256=digest(wheel.read_bytes()), profile_sha256=digest(profile_path.read_bytes()),
                  files={name:dict(source=origin,sha256=digest(raw)) for name,(origin,raw) in selected.items()},
                  required_exports=profile["required_exports"], native_execution=False)
    (output / "pure-build.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    return record


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--python", default=sys.executable)
    args = parser.parse_args()
    result = build(Path(__file__).resolve().parents[1], args.output, args.python)
    print(json.dumps(dict(wheel=result["wheel"], sha256=result["sha256"], modules=len(result["files"]))))
