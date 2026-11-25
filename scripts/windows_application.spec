# Build the self-contained Windows package and its installed bootloader from
# one PyInstaller analysis. The package contains just one copy of the DLLs.
import json
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

from packet_inspector.managed_runtime import APP_EXE, EMBEDDED_RUNTIME, LAYOUT_FILE, LIBRARIES, sha256_file

configuration = json.loads((Path(SPECPATH) / "windows-build.json").read_text(encoding="utf8"))
settings = dict(console=False, icon=configuration["icon"], upx=False)
analysis = Analysis(
    [configuration["script"]],
    pathex=[configuration["source"]],
    datas=configuration["datas"],
    hiddenimports=[*collect_submodules("zstandard"), "zstandard.backend_c"],
    excludes=configuration["excludes"],
)
modules = PYZ(analysis.pure)
runtime = EXE(modules, analysis.scripts, [], exclude_binaries=True, name="installed-app",
              contents_directory=LIBRARIES, **settings)
files = {}
for name, source, kind in [*analysis.binaries, *analysis.datas]:
    if kind not in {"BINARY", "EXTENSION", "DATA"}:
        raise ValueError("Unsupported Windows runtime file type: " + kind)
    files[name.replace("\\", "/")] = sha256_file(Path(source))
layout = Path(runtime.name).with_name(LAYOUT_FILE)
layout.write_text(json.dumps({"version": 1, "exe": sha256_file(Path(runtime.name)), "files": files},
                             sort_keys=True), encoding="utf8")
package = EXE(
    modules, analysis.scripts, analysis.binaries, analysis.datas,
    [(f"{EMBEDDED_RUNTIME}/{APP_EXE}", runtime.name, "DATA"),
     (f"{EMBEDDED_RUNTIME}/{LAYOUT_FILE}", str(layout), "DATA")],
    name=configuration["name"], **settings,
)
