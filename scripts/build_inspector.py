"""Build verified executables and the minimal public release files."""
from pathlib import Path
import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import uuid
from datetime import datetime, timezone
from scripts.build_source import Workspace, Snapshot, SourceError, local_development_path

from packet_inspector import VERSION
from packet_inspector.app_icon import APP_ICON
from packet_inspector.frontend_dev import PROFILE_NAME, PORTABLE_UI

ROOT=Path(__file__).resolve().parents[1]
PUBLIC_FILES = ("CodexPacketInspector.exe", "README.md", "LICENSE", "THIRD_PARTY_NOTICES.txt")
LINUX_FILES = ("CodexPacketInspector", "README.md", "README.en.md", "LICENSE", "THIRD_PARTY_NOTICES.txt",
               "codex-packet-inspector.service")


def source_files(root=None):
    """Collect redistributable sources while excluding local runtime data."""
    root = Path(root or ROOT).resolve()
    files = [root / name for name in ("README.md", "LICENSE", ".gitignore", ".gitattributes", "requirements-dev.txt",
             ".github/RELEASE_NOTES.md", ".github/workflows/windows-build.yml", ".githooks/pre-push")
             if (root / name).is_file()]
    suffixes = {".py", ".txt", ".md", ".html", ".js", ".cjs", ".css", ".ps1", ".json", ".ico", ".png", ".svg",
                ".toml", ".yaml", ".yml", ".sh", ".bat", ".cmd", ".woff", ".woff2", ".ttf", ".otf",
                ".webp", ".gif", ".jpeg", ".jpg", ".service"}
    excluded = {"__pycache__", "data", ".local", ".venv", "build", "release", "node_modules"}
    for folder in ("packet_inspector", "scripts", "tests", "docs", ".github"):
        for path in (root / folder).rglob("*"):
            relative = path.relative_to(root)
            diagnostic_report = (len(relative.parts) > 3 and relative.parts[:2] == ("scripts", "windows")
                                 and relative.parts[2].startswith(("CodexNetworkTest-", "CodexConfigReport-")))
            if (not path.is_file() or excluded.intersection(relative.parts)
                    or local_development_path(relative) or diagnostic_report):
                continue
            if path.is_symlink() or path.suffix not in suffixes:
                raise SourceError("Unsupported source input; include it explicitly before building: "
                                  + path.relative_to(root).as_posix())
            files.append(path)
    return sorted(set(files))


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


def collect_licenses(source, work):
    licenses=work/"THIRD_PARTY_LICENSES"
    licenses.mkdir(parents=True)
    shutil.copyfile(source/"packet_inspector"/"Codex-LICENSE.txt",licenses/"Codex-LICENSE.txt")
    import sysconfig
    python_names = ("python"+str(sys.version_info.major), "python"+str(sys.version_info.major)+"."+str(sys.version_info.minor))
    for python_license in (Path(sys.base_prefix)/"LICENSE.txt", Path(sysconfig.get_path("stdlib"))/"LICENSE.txt",
                           *(Path(sys.base_prefix)/"share"/"licenses"/name/"LICENSE" for name in python_names)):
        if python_license.is_file():
            shutil.copyfile(python_license,licenses/"Python-LICENSE.txt")
            break
    deps=ROOT/".local"/"builddeps"
    distributions=list(importlib.metadata.distributions())
    if deps.is_dir():distributions.extend(importlib.metadata.distributions(path=[str(deps)]))
    for distribution in distributions:
        name=(distribution.metadata.get("Name") or "").lower()
        if name not in {"pyinstaller","zstandard","tomlkit","cryptography","cffi","pycparser","pillow","urllib3","pywin32"}:continue
        for entry in distribution.files or []:
            if "licenses/" in str(entry).replace("\\","/"):
                source=Path(distribution.locate_file(entry))
                if source.is_file():
                    filename = (str(entry).replace("\\","/").split("licenses/",1)[1].replace("/","-")
                                if name == "pywin32" else source.name)
                    shutil.copyfile(source,licenses/(name+"-"+filename))
    return licenses


def compile_executable(snapshot, mode, portable_dev, work, stage, licenses):
    source=snapshot.source
    build=work/mode
    build.mkdir()
    profile=build/PROFILE_NAME
    profile.write_text(json.dumps(build_profile(mode,portable_dev)),encoding="utf8")
    manifest=build/"_source_manifest.json"
    snapshot.write_manifest(manifest,mode)
    env=dict(os.environ)
    deps=ROOT/".local"/"builddeps"
    env["PYTHONPATH"]=str(source)+(os.pathsep+str(deps) if deps.is_dir() else "")
    name="CodexPacketInspector-Dev" if mode=="dev" else "CodexPacketInspector"
    icon=source/APP_ICON.relative_to(ROOT)
    log=build/"build.log"
    windows=sys.platform=="win32"
    command=[sys.executable,"-m","PyInstaller","--noconfirm","--clean","--onefile",
             *(["--windowed","--icon",str(icon)] if windows else ["--console", "--exclude-module", "webview"]),
             "--name",name,"--distpath",str(stage),
             "--workpath",str(build/"app"),"--specpath",str(build/"spec"),
             "--paths",str(source),"--collect-submodules","zstandard","--hidden-import","zstandard.backend_c",
             "--add-data",str(source/"packet_inspector"/"ui")+os.pathsep+"packet_inspector/ui",
             "--add-data",str(profile)+os.pathsep+"packet_inspector",
             "--add-data",str(manifest)+os.pathsep+"packet_inspector",
             "--add-data",str(icon)+os.pathsep+"packet_inspector/assets",
             "--add-data",str(source/"packet_inspector"/"update_channel.json")+os.pathsep+"packet_inspector",
             "--add-data",str(source/"packet_inspector"/"compress_image.ps1")+os.pathsep+"packet_inspector",
             "--add-data",str(source/"packet_inspector"/"isolated_image_tool.ps1")+os.pathsep+"packet_inspector",
             "--add-data",str(source/"packet_inspector"/"isolated_image_tool.py")+os.pathsep+"packet_inspector",
             "--add-data",str(source/"packet_inspector"/"linux_image_control.py")+os.pathsep+"packet_inspector",
             "--add-data",str(source/"packet_inspector"/"windows_install.ps1")+os.pathsep+"packet_inspector",
              "--add-data",str(source/"packet_inspector"/"provider_migration_worker.py")+os.pathsep+"packet_inspector",
              "--add-data",str(licenses)+os.pathsep+"THIRD_PARTY_LICENSES",
             str(source/"packet_inspector"/"launch.py")]
    with log.open("w",encoding="utf8") as output:
        result=subprocess.run(command,cwd=source,env=env,stdout=output,stderr=subprocess.STDOUT)
    if result.returncode:
        raise SourceError(f"{mode} compilation failed; diagnostics: {log}")
    executable=stage/(name+(".exe" if windows else ""))
    # An EXE copied from an older build cannot pass this comparison.
    if deps.is_dir() and str(deps) not in sys.path:sys.path.insert(0,str(deps))
    from PyInstaller.archive.readers import CArchiveReader
    archive=CArchiveReader(str(executable))
    entries={name.replace('\\','/'):name for name in archive.toc}
    embedded=json.loads(archive.extract(entries['packet_inspector/_source_manifest.json']))
    if embedded!={**snapshot.manifest,"mode":mode}:
        raise SourceError("EXE source manifest does not match this build")
    from packet_inspector.frontend_dev import RESOURCES
    for asset in RESOURCES:
        key='packet_inspector/ui/'+asset
        if hashlib.sha256(archive.extract(entries[key])).hexdigest()!=snapshot.files[key]:
            raise SourceError("EXE contains an older frontend: "+asset)
    pyz=archive.open_embedded_archive(next(name for name in archive.toc if name.startswith('PYZ')))
    required=('packet_inspector.window_chrome',) if windows else ('packet_inspector.server','packet_inspector.transport')
    if any(module not in pyz.toc for module in required):
        raise SourceError("Executable is missing required platform modules")
    if not windows:
        result=subprocess.run([str(executable),"--version"],capture_output=True,text=True,timeout=30)
        if result.returncode or result.stdout.strip()!="Codex Request Optimizer "+VERSION:
            raise SourceError("Linux executable startup check failed: "+(result.stderr or result.stdout)[-800:])
    return executable


def package_release(source, stage, licenses):
    linux=sys.platform.startswith("linux")
    public = stage / ("linux-package" if linux else "publish")
    public.mkdir()
    executable="CodexPacketInspector" if linux else PUBLIC_FILES[0]
    shutil.copyfile(stage / executable, public / executable)
    for name in ("README.md", "LICENSE"):
        shutil.copyfile(source / name, public / name)
    notices = (source / "docs" / "THIRD_PARTY_NOTICES.txt").read_text(encoding="utf8")
    for path in sorted(licenses.iterdir()):
        if path.is_file():
            notices += "\n\n" + "=" * 72 + "\n" + path.name + "\n" + "=" * 72 + "\n"
            notices += path.read_text(encoding="utf8", errors="replace")
    (public / "THIRD_PARTY_NOTICES.txt").write_text(notices, encoding="utf8")
    if linux:
        (public / executable).chmod(0o755)
        shutil.copyfile(source / "docs" / "README.en.md", public / "README.en.md")
        shutil.copyfile(source / "scripts" / "linux" / "codex-packet-inspector.service",
                        public / "codex-packet-inspector.service")
        def permissions(info):
            info.mode=0o755 if info.name.endswith("/"+executable) else 0o644
            info.uid=info.gid=0
            info.uname=info.gname=""
            return info
        with tarfile.open(stage / ("CodexPacketInspector-linux-"+platform.machine()+".tar.gz"), "w:gz") as archive:
            for path in sorted(public.iterdir()):
                archive.add(path, arcname="codex-packet-inspector/"+path.name, filter=permissions)


def main(argv=None):
    parser=argparse.ArgumentParser()
    parser.add_argument("--output-dir",type=Path)
    parser.add_argument("--mode",choices=("dev","release"),default="release")
    parser.add_argument("--portable-dev",action="store_true")
    parser.add_argument("--release-only",action="store_true",help="omit the companion Windows development EXE from a release build")
    parser.add_argument("--local",action="store_true",help="use local Git refs without fetching; retain all source and artifact checks")
    parser.add_argument("--check-source",action="store_true",help="validate the shared source without building")
    args=parser.parse_args(argv)
    if args.release_only and args.mode!="release":
        parser.error("--release-only requires --mode release")
    refresh=args.mode=="release" and not args.local
    try:
        if sys.platform!="win32" and not sys.platform.startswith("linux"):
            raise SourceError("Builds currently support Windows and Linux hosts")
        workspace=Workspace(ROOT)
        with workspace.lock():
            metadata=workspace.validate(args.mode,refresh=refresh)
            metadata["remote_refreshed"]=refresh
            metadata.update(platform=sys.platform,architecture=platform.machine(),libc=list(platform.libc_ver()))
            if metadata['version']!=VERSION:raise SourceError("Imported version differs from the source version")
            if args.check_source:
                print(json.dumps({"source":"ok",**metadata,"canonical_root":str(workspace.canonical)},ensure_ascii=False))
                return 0
            release=output_directory(args.output_dir,args.mode)
            work=ROOT/"build"/("inspector-"+args.mode+"-"+uuid.uuid4().hex[:8])
            work.mkdir(parents=True)
            snapshot=Snapshot(workspace,work/"source",source_files,metadata)
            stage=work/"deliverable"
            stage.mkdir()
            licenses=collect_licenses(snapshot.source,work)
            executable=compile_executable(snapshot,args.mode,args.portable_dev,work,stage,licenses)
            if args.mode=="release":
                if sys.platform=="win32" and not args.release_only:
                    compile_executable(snapshot,"dev",True,work,stage,licenses)
                package_release(snapshot.source,stage,licenses)
            snapshot.write_manifest(stage/"BUILD-SOURCE.json",args.mode)
            snapshot.assert_unchanged()
            Workspace(ROOT).validate(args.mode,refresh=refresh)
            artifacts=[]
            for path in sorted(stage.rglob("*")):
                if path.is_file():
                    with path.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
                    artifacts.append({"file":path.relative_to(stage).as_posix(),"bytes":path.stat().st_size,"sha256":digest})
            (stage/"SHA256SUMS.txt").write_text(''.join(item['sha256']+'  '+item['file']+'\n' for item in artifacts),encoding='ascii')
            output_directory(release,args.mode)
            shutil.copytree(stage,release,dirs_exist_ok=True)
            print(json.dumps({"build":"ok","mode":args.mode,"output_dir":str(release),
                              "executable":str(release/executable.name),"source_sha256":snapshot.manifest['source_sha256'],
                              "git_commit":metadata['git_commit'],"artifacts":artifacts},ensure_ascii=False))
            return 0
    except (ValueError,OSError,subprocess.TimeoutExpired) as exc:
        print(json.dumps({"build":"refused","error":str(exc)},ensure_ascii=False))
        return 1


if __name__=="__main__":
    raise SystemExit(main())
