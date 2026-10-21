"""Build a distributable EXE and a clean source archive, without local records."""
from pathlib import Path
import argparse
import hashlib
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import zipfile
import uuid
from datetime import datetime, timezone

from packet_inspector import VERSION

ROOT=Path(__file__).resolve().parent


def output_directory(requested=None):
    release = (Path(requested) if requested is not None else ROOT / "release" /
               (VERSION + "-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])).resolve()
    if release.exists() and (not release.is_dir() or any(release.iterdir())):
        raise ValueError("Output directory is not empty; use a new version directory. Existing releases are never overwritten.")
    return release


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--output-dir",type=Path)
    args=parser.parse_args()
    try:
        release=output_directory(args.output_dir)
    except ValueError as exc:
        parser.error(str(exc))
    release.mkdir(parents=True,exist_ok=True)
    log=ROOT/"build"/"inspector-build.log"
    log.parent.mkdir(exist_ok=True)
    env=dict(os.environ)
    deps=ROOT/".local"/"builddeps"
    if deps.is_dir():
        env["PYTHONPATH"]=str(deps)+os.pathsep+env.get("PYTHONPATH","")
    licenses=ROOT/"build"/"THIRD_PARTY_LICENSES"
    licenses.mkdir(parents=True,exist_ok=True)
    shutil.copyfile(ROOT/"packet_inspector"/"Codex-LICENSE.txt",licenses/"Codex-LICENSE.txt")
    python_license=Path(sys.base_prefix)/"LICENSE.txt"
    if python_license.is_file():shutil.copyfile(python_license,licenses/"Python-LICENSE.txt")
    distributions=list(importlib.metadata.distributions())
    if deps.is_dir():distributions.extend(importlib.metadata.distributions(path=[str(deps)]))
    for distribution in distributions:
        name=(distribution.metadata.get("Name") or "").lower()
        if name not in {"pyinstaller","zstandard","tomlkit"}:continue
        for entry in distribution.files or []:
            if "licenses/" in str(entry).replace("\\","/"):
                source=Path(distribution.locate_file(entry))
                if source.is_file():shutil.copyfile(source,licenses/(name+"-"+source.name))
    command=[sys.executable,"-m","PyInstaller","--noconfirm","--clean","--onefile","--windowed",
             "--name","CodexPacketInspector","--distpath",str(release),
             "--workpath",str(ROOT/"build"/"inspector"),"--specpath",str(ROOT/"build"/"inspector-spec"),
             "--paths",str(ROOT),"--collect-submodules","zstandard","--hidden-import","zstandard.backend_c",
             "--add-data",str(ROOT/"packet_inspector"/"ui")+os.pathsep+"packet_inspector/ui",
             "--add-data",str(ROOT/"packet_inspector"/"update_channel.json")+os.pathsep+"packet_inspector",
             "--add-data",str(ROOT/"packet_inspector"/"compress_image.ps1")+os.pathsep+"packet_inspector",
              "--add-data",str(ROOT/"packet_inspector"/"windows_install.ps1")+os.pathsep+"packet_inspector",
              "--add-data",str(licenses)+os.pathsep+"THIRD_PARTY_LICENSES",
             str(ROOT/"packet_inspector"/"launch.py")]
    with log.open("w",encoding="utf8") as output:
        result=subprocess.run(command,cwd=ROOT,env=env,stdout=output,stderr=subprocess.STDOUT)
    if result.returncode:
        print("Build failed; log:",log)
        print("\n".join(log.read_text(encoding="utf8",errors="replace").splitlines()[-18:]))
        return result.returncode
    for name, entry, resources in [
        ("CodexInspectorRouter", ROOT/"packet_inspector"/"router.py", []),
        ("CodexRequestOptimizer-Upgrade", ROOT/"packet_inspector"/"upgrade.py", [ROOT/"build"/"upgrade-payload"]),
    ]:
        if resources:
            payload=resources[0]
            payload.mkdir(parents=True,exist_ok=True)
            hashes={}
            for binary in ["CodexPacketInspector.exe","CodexInspectorRouter.exe"]:
                shutil.copy2(release/binary,payload/binary)
                with (payload/binary).open("rb") as stream:hashes[binary]=hashlib.file_digest(stream,"sha256").hexdigest()
            (payload/"manifest.json").write_text(json.dumps({"version":VERSION,"router_protocol":1,"sha256":hashes}),encoding="utf8")
        helper_log=ROOT/"build"/(name+".log")
        helper_command=[sys.executable,"-m","PyInstaller","--noconfirm","--clean","--onefile","--windowed",
                        "--name",name,"--distpath",str(release),"--workpath",str(ROOT/"build"/name),
                        "--specpath",str(ROOT/"build"/"inspector-spec"),"--paths",str(ROOT)]
        for resource in resources:helper_command.extend(["--add-data",str(resource)+os.pathsep+"upgrade-payload"])
        helper_command.append(str(entry))
        with helper_log.open("w",encoding="utf8") as output:
            helper_result=subprocess.run(helper_command,cwd=ROOT,env=env,stdout=output,stderr=subprocess.STDOUT)
        if helper_result.returncode:
            print(json.dumps({"build":"failed","component":name,"log":str(helper_log)}))
            return helper_result.returncode
    shutil.copyfile(ROOT/"install_inspector_service.ps1",release/"install_inspector_service.ps1")
    shutil.copyfile(ROOT/"update_inspector_service.ps1",release/"update_inspector_service.ps1")
    for name in ["README.md","LICENSE","THIRD_PARTY_NOTICES.txt"]:
        shutil.copyfile(ROOT/name,release/name)
    sources=[ROOT/"build_inspector.py",ROOT/"dev_inspector_ui.py",ROOT/"test_packet_inspector.py",ROOT/"test_providers.py",ROOT/"test_overflow_compaction.py",ROOT/"test_cache_maintenance.py",ROOT/"test_desktop_ui.py",ROOT/"test_token_window.py",ROOT/"test_session_summary.py",ROOT/"verify_inspector_exe.py",ROOT/"verify_overflow_exe.py",
              ROOT/"install_inspector_service.ps1",ROOT/"update_inspector_service.ps1",ROOT/"README.md",ROOT/"LICENSE",ROOT/"THIRD_PARTY_NOTICES.txt",
              ROOT/"requirements-dev.txt",ROOT/"AGENTS.md",ROOT/".gitignore",ROOT/".github"/"workflows"/"windows-build.yml"]
    sources.extend(p for p in (ROOT/"packet_inspector").rglob("*")
                   if p.is_file() and p.suffix in {".py",".txt",".html",".js",".css",".ps1",".json"}
                   and "__pycache__" not in p.parts and "data" not in p.parts)
    sources.extend([ROOT/"test_capacity_retry.py", ROOT/"test_runtime_settings.py", ROOT/"test_entry_port.py",
                    ROOT/"test_router.py",ROOT/"test_updates.py",ROOT/"test_upgrade.py",ROOT/"verify_upgrade_exe.py",ROOT/"test_frontend_dev.py"])
    archive=release/"CodexPacketInspector-source.zip"
    with zipfile.ZipFile(archive,"w",zipfile.ZIP_DEFLATED) as zipped:
        for path in sorted(sources):
            zipped.write(path,path.relative_to(ROOT))
        for path in sorted(licenses.iterdir()):
            if path.is_file():zipped.write(path,"THIRD_PARTY_LICENSES/"+path.name)
    portable=release/"CodexPacketInspector-Windows-x64.zip"
    with zipfile.ZipFile(portable,"w",zipfile.ZIP_DEFLATED) as zipped:
        for name in ["CodexRequestOptimizer-Upgrade.exe","CodexPacketInspector.exe","update_inspector_service.ps1","README.md","LICENSE","THIRD_PARTY_NOTICES.txt"]:
            zipped.write(release/name,name)
        for path in sorted(licenses.iterdir()):
            if path.is_file():zipped.write(path,"THIRD_PARTY_LICENSES/"+path.name)
    summary=[]
    for path in [release/"CodexPacketInspector.exe",release/"CodexRequestOptimizer-Upgrade.exe",release/"CodexInspectorRouter.exe",portable,release/"install_inspector_service.ps1",archive]:
        with path.open("rb") as stream:
            digest=hashlib.file_digest(stream,"sha256").hexdigest()
        summary.append({"file":path.name,"bytes":path.stat().st_size,
                        "sha256":digest})
    (release/"SHA256SUMS.txt").write_text("\n".join(row["sha256"]+"  "+row["file"] for row in summary)+"\n",encoding="ascii")
    print(json.dumps({"build":"ok","output_dir":str(release),"artifacts":summary},ensure_ascii=False))
    return 0


if __name__=="__main__":
    raise SystemExit(main())
