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
import tomllib
from http.server import ThreadingHTTPServer
from urllib.request import Request, urlopen

from tests.test_packet_inspector import Upstream, zstandard
from tests.test_image_isolation import request_body
from packet_inspector.image_config import find_codex_cli
from scripts.build_inspector import PUBLIC_FILES

ROOT=Path(__file__).resolve().parents[2]


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
        return json.load(urlopen(r,timeout=20))
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
        assert api("/api/status")["connection_state"]=="disconnected"
        assert api("/api/installation")["supported"] is False
        api("/api/link",{"action":"restore"})
        for suffix,headers in [("/responses", {}), ("/responses/?api-version=test", {
                "Connection":"Upgrade", "Upgrade":"websocket", "Sec-WebSocket-Version":"13",
                "Sec-WebSocket-Key":"dGhlIHNhbXBsZSBub25jZQ=="})]:
            c=http.client.HTTPConnection("127.0.0.1",runtime["port"],timeout=5)
            c.request("GET","/relay/"+settings["relay_token"]+suffix,headers=headers)
            r=c.getresponse()
            assert r.status==426 and json.loads(r.read())["error"]["code"]=="websocket_not_supported"
            c.close()
        assert not upstream.requests
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
        api("/api/link",{"action":"activate"})
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
        image_status=api("/api/status")
        assert image_status["image_save_dir_supported"]
        assert image_status["image_save_dir"]==str((folder/"image-attachments").resolve())
        custom_image_folder=folder/"图片目录 with spaces"
        selected=api("/api/settings",{"image_save_dir":str(custom_image_folder)})
        assert selected["image_save_dir"]==str(custom_image_folder.resolve())
        assert custom_image_folder.is_dir()
        assert json.loads((folder/"settings.json").read_text(encoding="utf8"))["image_save_dir"]==str(custom_image_folder.resolve())
        image_install="skipped_cli_unavailable"
        image_path=None
        if find_codex_cli():
            original_config=tomllib.loads(config.read_text(encoding="utf8"))
            assert "isolated_vision" not in original_config.get("mcp_servers",{})
            model_requests=len(upstream.requests)
            installed=api("/api/settings",{"image_isolation_enabled":True})
            assert installed["image_tool"]["managed_tool_installed"]
            image_config=tomllib.loads(config.read_text(encoding="utf8"))
            assert image_config["tools"]["view_image"] is False
            registration=image_config["mcp_servers"]["isolated_vision"]
            assert "command" in registration and "url" not in registration
            tool_file=Path(registration["args"][-1])
            assert tool_file.is_file()
            messages=[{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18"}},
                      {"jsonrpc":"2.0","id":2,"method":"tools/list"}]
            tool_check=subprocess.run([registration["command"],*registration["args"]],
                input="".join(json.dumps(message)+"\n" for message in messages),
                env={**os.environ,**registration["env"]},capture_output=True,text=True,encoding="utf8",timeout=15,
                creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
            assert tool_check.returncode==0,tool_check.stderr[:500]
            replies=[json.loads(line) for line in tool_check.stdout.splitlines() if line.strip()]
            assert [tool["name"] for tool in replies[-1]["result"]["tools"]]==["analyze_image"]
            api("/api/link",{"action":"restore"})
            connection=http.client.HTTPConnection("127.0.0.1",runtime["port"],timeout=5)
            connection.request("POST","/relay/"+settings["relay_token"]+"/responses",json.dumps(request_body()).encode(),{"Content-Type":"application/json"})
            response=connection.getresponse();assert response.status==200;response.read();connection.close()
            image_part=json.loads(upstream.requests[-1]["body"])["input"][-1]["content"][-1]
            image_path=Path(image_part["text"])
            assert image_part["type"]=="input_text" and image_path.is_absolute()
            assert image_path.parent==custom_image_folder.resolve() and image_path.is_file()
            api("/api/settings",{"image_isolation_enabled":False})
            expected_native=json.loads(json.dumps(original_config))
            expected_native.setdefault("tools",{})["view_image"]=True
            if expected_native.get("features",{}).get("view_image") is False:
                expected_native["features"].pop("view_image")
                if not expected_native["features"]:expected_native.pop("features")
            assert tomllib.loads(config.read_text(encoding="utf8"))==expected_native
            assert not tool_file.exists()
            assert len(upstream.requests)==model_requests+1
            image_install="verified"
        reset=api("/api/settings",{"image_save_dir":""})
        assert reset["image_save_dir"]==reset["image_save_dir_default"]
        assert json.loads((folder/"settings.json").read_text(encoding="utf8"))["image_save_dir"]==""
        if image_path:assert image_path.is_file()
        public = release / "publish"
        assert {path.name for path in public.iterdir()} == set(PUBLIC_FILES)
        notices = (public / "THIRD_PARTY_NOTICES.txt").read_text(encoding="utf8")
        assert "Codex-LICENSE.txt" in notices and "Apache License" in notices
        (local/"inspector-smoke-current.json").write_text(json.dumps({"data_dir":str(folder),"url":base+"/"}),encoding="utf8")
        print(json.dumps({"packaged_smoke":"ok","body_bytes_unchanged":True,"zstd_bundled":True,
                          "analysis_only_requests":byte_results,"standalone_compression":compressed_results,
                          "bundled_image_tool_auto_install":image_install,
                          "image_save_directory":"verified",
                          "public_files_verified":True,"real_config_changed":False}),flush=True)
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
