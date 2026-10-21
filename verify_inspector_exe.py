"""Exercise the packaged executable against a disposable local-only upstream."""
from __future__ import annotations
import argparse
import hashlib
import http.client
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from http.server import ThreadingHTTPServer
from urllib.request import Request, urlopen

from test_packet_inspector import Upstream, zstandard

ROOT=Path(__file__).resolve().parent


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--keep-open",action="store_true")
    parser.add_argument("--release-dir",type=Path,default=ROOT/"release")
    args=parser.parse_args()
    release=args.release_dir.resolve()
    local=(ROOT/".local").resolve()
    local.mkdir(exist_ok=True)
    temporary=tempfile.TemporaryDirectory(prefix="inspector-smoke-",dir=local)
    folder=Path(temporary.name).resolve()
    assert folder.is_relative_to(local)
    config=folder/"config.toml";config.write_text('model="test-only"',encoding="utf8")
    upstream=ThreadingHTTPServer(("127.0.0.1",0),Upstream)
    upstream.requests=[];upstream.first=threading.Event();upstream.release=threading.Event()
    threading.Thread(target=upstream.serve_forever,daemon=True).start()
    command=[str(release/"CodexPacketInspector.exe"),"--background","--port","0","--data-dir",str(folder),
             "--config",str(config),"--upstream","http://127.0.0.1:"+str(upstream.server_port)+"/v1"]
    process=subprocess.Popen(command,cwd=ROOT,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
    base=None
    settings=None
    def api(path,data=None):
        r=Request(base+path,data=None if data is None else json.dumps(data).encode())
        return json.load(urlopen(r,timeout=5))
    try:
        for _ in range(120):
            try:
                settings=json.loads((folder/"settings.json").read_text())
                runtime=json.loads((folder/"runtime.json").read_text())
                base="http://127.0.0.1:"+str(runtime["port"])
                api("/api/status")
                break
            except (OSError,ValueError):
                if process.poll() is not None:raise RuntimeError("EXE stopped before startup")
                time.sleep(.1)
        if not settings:raise RuntimeError("EXE startup timed out")
        assert api("/api/status")["request_compression"]=="zstd"
        assert api("/api/installation")["supported"] is False
        api("/api/settings",{"request_compression":"off"})
        body={"model":"local-ui-test","input":[{"type":"message","role":"developer","content":[{"type":"input_text","text":"Synthetic local-only UI test."}]},
            {"type":"message","role":"user","content":[{"type":"input_text","text":"packet-size-check"}]},
            {"type":"reasoning","encrypted_content":"synthetic-ciphertext"*120},
            {"type":"function_call","call_id":"test-call","name":"sample","arguments":"{}"},
            {"type":"function_call_output","call_id":"test-call","output":"tool result "*100}],
            "tools":[{"type":"function","name":"sample","parameters":{"type":"object","properties":{}}}]}
        byte_results=[]
        for path,compress in [("/responses",False),("/responses/compact",True)]:
            if compress:body["input"].append({"type":"compaction_trigger"})
            raw=json.dumps(body,ensure_ascii=False).encode()
            outbound=zstandard.ZstdCompressor().compress(raw) if compress else raw
            headers={"Content-Type":"application/json","session_id":"local-only-ui-test","Authorization":"Bearer fake-do-not-store"}
            if compress:headers["Content-Encoding"]="zstd"
            c=http.client.HTTPConnection("127.0.0.1",runtime["port"],timeout=5)
            c.request("POST","/relay/"+settings["relay_token"]+path,outbound,headers)
            r=c.getresponse();assert r.status==200;r.read();c.close()
            assert upstream.requests[-1]["body"]==outbound
            byte_results.append({"encoding":"zstd" if compress else "identity","wire":len(outbound),"decoded":len(raw)})
        rows=api("/api/packets")
        assert len(rows)==2
        detail=api("/api/packets/"+rows[0]["id"])
        assert detail["inspection"]=="json" and detail["is_compaction"]
        assert detail["decoded_bytes"]==byte_results[-1]["decoded"]
        assert api("/api/packets/"+rows[0]["id"]+"/diff")["added"]
        api("/api/settings",{"request_compression":"zstd"})
        compressed_results=[]
        for compact in [False,True]:
            payload={"input":[{"role":"user","content":"Synthetic retained payload "*400}]+([{"type":"compaction_trigger"}] if compact else [])}
            raw=json.dumps(payload,ensure_ascii=False).encode()
            c=http.client.HTTPConnection("127.0.0.1",runtime["port"],timeout=5)
            c.request("POST","/relay/"+settings["relay_token"]+"/responses",raw,{"Content-Type":"application/json"})
            r=c.getresponse();assert r.status==200;r.read();c.close()
            sent=upstream.requests[-1]
            assert sent["headers"]["Content-Encoding"]=="zstd"
            assert zstandard.ZstdDecompressor().decompress(sent["body"])==raw
            compressed_results.append({"kind":"compaction" if compact else "generation","input_bytes":len(raw),"wire_bytes":len(sent["body"])})
        with zipfile.ZipFile(release/"CodexPacketInspector-source.zip") as source:
            names=source.namelist()
            assert {"README.md","LICENSE","packet_inspector/transport.py","packet_inspector/windows_install.ps1",".github/workflows/windows-build.yml"}<=set(names)
            assert "THIRD_PARTY_LICENSES/Codex-LICENSE.txt" in names
            assert not any(n.startswith(("evidence/",".local/",".upstream/","build/")) or n.endswith(("settings.json","auth.json",".sqlite")) for n in names)
            assert not {"install_gate_observer.py","test_gate_observer.py","packet_inspector/observer.py","payload_patch.py","payload_transport.py"}.intersection(names)
            assert not any(b"C:\\Users\\z00611370" in source.read(n) for n in names)
        (local/"inspector-smoke-current.json").write_text(json.dumps({"data_dir":str(folder),"url":base+"/"}),encoding="utf8")
        print(json.dumps({"packaged_smoke":"ok","body_bytes_unchanged":True,"zstd_bundled":True,
                          "analysis_only_requests":byte_results,"standalone_compression":compressed_results,
                          "source_archive_clean":True,"real_config_changed":False}),flush=True)
        if args.keep_open:
            input("Packaged UI fixture ready; press Enter to finish.\n")
    finally:
        if settings:
            try:api("/api/stop",{})
            except OSError:pass
        try:process.wait(timeout=8)
        except subprocess.TimeoutExpired:process.terminate();process.wait(timeout=5)
        upstream.shutdown();upstream.server_close()
        if folder.is_relative_to(local):temporary.cleanup()
        (local/"inspector-smoke-current.json").unlink(missing_ok=True)
    return 0


if __name__=="__main__":
    raise SystemExit(main())
