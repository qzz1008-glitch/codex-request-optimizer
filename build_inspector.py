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
from packet_inspector.app_icon import APP_ICON
from packet_inspector.frontend_dev import PROFILE_NAME, PORTABLE_UI

ROOT=Path(__file__).resolve().parent


def output_directory(requested=None, mode="release"):
    base = ROOT / ".local" / "dev-builds" if mode == "dev" else ROOT / "release"
    release = (Path(requested) if requested is not None else base /
               (VERSION + "-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])).resolve()
    if release.exists() and (not release.is_dir() or any(release.iterdir())):
        raise ValueError("Output directory is not empty; use a new version directory. Existing releases are never overwritten.")
    return release


def build_profile(mode, portable_dev=False):
    if mode == "release":
        return {"mode": "release"}
    if mode == "dev":
        return {"mode": "dev", "ui_dir": PORTABLE_UI if portable_dev else str(ROOT / "packet_inspector" / "ui")}
    raise ValueError("invalid build mode")


def main(argv=None):
    parser=argparse.ArgumentParser()
    parser.add_argument("--output-dir",type=Path)
    parser.add_argument("--mode",choices=("dev","release"),default="release")
    parser.add_argument("--portable-dev",action="store_true",help="bundle editable frontend assets for distributed development builds")
    args=parser.parse_args(argv)
    try:
        release=output_directory(args.output_dir,args.mode)
    except ValueError as exc:
        parser.error(str(exc))
    release.mkdir(parents=True,exist_ok=True)
    work=ROOT/"build"/("inspector-"+args.mode+"-"+uuid.uuid4().hex[:8])
    work.mkdir(parents=True)
    log=work/"build.log"
    profile=work/PROFILE_NAME
    profile.write_text(json.dumps(build_profile(args.mode,args.portable_dev)),encoding="utf8")
    env=dict(os.environ)
    deps=ROOT/".local"/"builddeps"
    if deps.is_dir():
        env["PYTHONPATH"]=str(deps)+os.pathsep+env.get("PYTHONPATH","")
    licenses=work/"THIRD_PARTY_LICENSES"
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
    name="CodexPacketInspector-Dev" if args.mode=="dev" else "CodexPacketInspector"
    command=[sys.executable,"-m","PyInstaller","--noconfirm","--clean","--onefile","--windowed",
             "--name",name,"--icon",str(APP_ICON),"--distpath",str(release),
             "--workpath",str(work/"app"),"--specpath",str(work/"spec"),
             "--paths",str(ROOT),"--collect-submodules","zstandard","--hidden-import","zstandard.backend_c",
             "--add-data",str(ROOT/"packet_inspector"/"ui")+os.pathsep+"packet_inspector/ui",
             "--add-data",str(profile)+os.pathsep+"packet_inspector",
             "--add-data",str(APP_ICON)+os.pathsep+"packet_inspector/assets",
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
    if args.mode=="dev":
        executable=release/(name+".exe")
        with executable.open("rb") as stream:digest=hashlib.file_digest(stream,"sha256").hexdigest()
        (release/"SHA256SUMS.txt").write_text(digest+"  "+executable.name+"\n",encoding="ascii")
        print(json.dumps({"build":"ok","mode":"dev","output_dir":str(release),
                          "executable":str(executable),"frontend":build_profile("dev",args.portable_dev)["ui_dir"],
                          "sha256":digest},ensure_ascii=False))
        return 0
    dev_output=work/"development-package"
    dev_log=work/"development-package.log"
    with dev_log.open("w",encoding="utf8") as output:
        dev_result=subprocess.run([sys.executable,str(ROOT/"build_inspector.py"),"--mode","dev","--portable-dev",
                                   "--output-dir",str(dev_output)],cwd=ROOT,env=env,stdout=output,stderr=subprocess.STDOUT)
    if dev_result.returncode:
        print(json.dumps({"build":"failed","component":"development","log":str(dev_log)}))
        return dev_result.returncode
    shutil.copy2(dev_output/"CodexPacketInspector-Dev.exe",release/"CodexPacketInspector-Dev.exe")
    shutil.copyfile(ROOT/"install_inspector_service.ps1",release/"install_inspector_service.ps1")
    shutil.copyfile(ROOT/"update_inspector_service.ps1",release/"update_inspector_service.ps1")
    for name in ["README.md","LICENSE","THIRD_PARTY_NOTICES.txt"]:
        shutil.copyfile(ROOT/name,release/name)
    sources=[ROOT/"build_inspector.py",ROOT/"dev_inspector_ui.py",ROOT/"test_packet_inspector.py",ROOT/"test_providers.py",ROOT/"test_overflow_compaction.py",ROOT/"test_cache_maintenance.py",ROOT/"test_desktop_ui.py",ROOT/"test_token_window.py",ROOT/"test_session_summary.py",ROOT/"verify_inspector_exe.py",ROOT/"verify_overflow_exe.py",
              ROOT/"install_inspector_service.ps1",ROOT/"update_inspector_service.ps1",ROOT/"README.md",ROOT/"LICENSE",ROOT/"THIRD_PARTY_NOTICES.txt",
              ROOT/"requirements-dev.txt",ROOT/"AGENTS.md",ROOT/".gitignore",ROOT/".github"/"workflows"/"windows-build.yml"]
    sources.extend(p for p in (ROOT/"packet_inspector").rglob("*")
                   if p.is_file() and p.suffix in {".py",".txt",".html",".js",".css",".ps1",".json",".ico",".png",".svg"}
                   and "__pycache__" not in p.parts and "data" not in p.parts)
    sources.extend([ROOT/"build_app_icon.ps1", ROOT/"test_self_upgrade.py", ROOT/"verify_self_upgrade_exe.py",
                    ROOT/"test_usage_trigger.py", ROOT/"verify_usage_exe.py",
                    ROOT/"test_response_integrity.py", ROOT/"test_desktop_instance.py", ROOT/"test_window_lifecycle.py",
                    ROOT/"test_fixtures"/"updates_v0_6_9.py",
                    ROOT/".github"/"RELEASE_NOTES.md"])
    sources.extend([ROOT/"test_image_isolation.py", ROOT/"test_capacity_retry.py", ROOT/"test_runtime_settings.py", ROOT/"test_entry_port.py",
                    ROOT/"test_router.py",ROOT/"test_updates.py",ROOT/"test_upgrade.py",ROOT/"verify_upgrade_exe.py",ROOT/"test_frontend_dev.py",ROOT/"test_launch.py"])
    archive=release/"CodexPacketInspector-source.zip"
    with zipfile.ZipFile(archive,"w",zipfile.ZIP_DEFLATED) as zipped:
        for path in sorted(sources):
            zipped.write(path,path.relative_to(ROOT))
        for path in sorted(licenses.iterdir()):
            if path.is_file():zipped.write(path,"THIRD_PARTY_LICENSES/"+path.name)
    portable=release/"CodexPacketInspector-Windows-x64.zip"
    with zipfile.ZipFile(portable,"w",zipfile.ZIP_DEFLATED) as zipped:
        for name in ["CodexPacketInspector.exe","README.md","LICENSE","THIRD_PARTY_NOTICES.txt"]:
            zipped.write(release/name,name)
        for path in sorted(licenses.iterdir()):
            if path.is_file():zipped.write(path,"THIRD_PARTY_LICENSES/"+path.name)
    summary=[]
    for path in [release/"CodexPacketInspector.exe",release/"CodexPacketInspector-Dev.exe",portable,release/"install_inspector_service.ps1",archive]:
        with path.open("rb") as stream:
            digest=hashlib.file_digest(stream,"sha256").hexdigest()
        summary.append({"file":path.name,"bytes":path.stat().st_size,
                        "sha256":digest})
    (release/"SHA256SUMS.txt").write_text("\n".join(row["sha256"]+"  "+row["file"] for row in summary)+"\n",encoding="ascii")
    print(json.dumps({"build":"ok","mode":"release","output_dir":str(release),"artifacts":summary},ensure_ascii=False))
    return 0


if __name__=="__main__":
    raise SystemExit(main())
