"""Stage a pure wheel browser check; optionally serve it on loopback.

Requires a pure-build.json produced by tools/build_pure.py. Browser execution
downloads the pinned Pyodide runtime from its official CDN. Results are stored
locally and never sent to the CDN. Existing output directories are rejected.
"""
from pathlib import Path
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import argparse
import hashlib
import json
import shutil
import time
import zipfile


def stage(build_manifest, output, wheel=None):
    manifest = json.loads(build_manifest.read_text(encoding="utf-8"))
    wheel = wheel or build_manifest.parent / Path(manifest["wheel"]).name
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    if digest != manifest["sha256"]:
        raise ValueError("Wheel digest differs from build manifest")
    with zipfile.ZipFile(wheel) as archive:
        members = {name for name in archive.namelist()
                   if name.startswith("easysewer/") and not name.endswith("/")}
        if members != set(manifest["files"]):
            raise ValueError("Wheel members differ from build manifest")
        for name, row in manifest["files"].items():
            if hashlib.sha256(archive.read(name)).hexdigest() != row["sha256"]:
                raise ValueError("Module digest differs: " + name)
    if not wheel.name.endswith("-py3-none-any.whl"):
        raise ValueError("Expected a pure Python wheel")
    output.mkdir(parents=True, exist_ok=False)
    site = output / "site"
    site.mkdir()
    shutil.copyfile(wheel, site / wheel.name)
    for name in ("index.html", "runner.js", "worker.js", "probe.py"):
        shutil.copyfile(Path(__file__).parent / "browser" / name, site / name)
    data = dict(wheel=wheel.name, sha256=digest, files=manifest["files"],
                module_count=len(members), pyodide="314.0.7")
    (site / "manifest.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
    return site


def serve(site):
    results = site.parent / "results"
    results.mkdir(exist_ok=True)

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(site), **kwargs)

        def do_POST(self):
            origin = "http://127.0.0.1:" + str(self.server.server_port)
            if self.path != "/result" or self.headers.get("Origin") != origin:
                self.send_error(403)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length < 2 * 1024**2:
                    self.send_error(413)
                    return
                raw = self.rfile.read(length)
                data = json.loads(raw)
                if not isinstance(data, dict):
                    raise ValueError("Expected JSON object")
            except (ValueError, UnicodeError):
                self.send_error(400)
                return
            target = results / (str(time.time_ns()) + ".json")
            with target.open("xb") as stream:
                stream.write(raw)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
            print(json.dumps(dict(result=str(target), success=data.get("success"))), flush=True)

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        print("http://127.0.0.1:" + str(server.server_port), flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-manifest", required=True, type=Path)
    parser.add_argument("--wheel", type=Path, help="Override manifest wheel location")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--serve", action="store_true")
    args = parser.parse_args()
    site = stage(args.build_manifest.resolve(), args.output.resolve(),
                 args.wheel.resolve() if args.wheel else None)
    print(json.dumps(dict(site=str(site))), flush=True)
    if args.serve:
        serve(site)
