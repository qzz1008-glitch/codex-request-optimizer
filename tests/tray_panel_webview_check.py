"""Run explicitly on Windows with pywebview/WebView2; never touches a live relay."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ctypes
from ctypes import wintypes
import json
import math
from pathlib import Path
import shutil
import tempfile
import threading
import time
from urllib.parse import parse_qs, urlsplit

from packet_inspector.desktop_ui import DesktopUI, UI_DIR, ui_build_id
from packet_inspector.exit_flow import REQUEST_FILE
from packet_inspector.frontend_dev import RESOURCES
from packet_inspector.tray_panel import TrayPanel


class StatusHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        self.server.requests.append(self.path)
        self.server.started.set()
        self.server.release.wait(10)
        target=urlsplit(self.path)
        if target.path == "/api/tray-status":
            value=self.server.snapshot
        elif target.path == "/api/status":
            value={"storage":self.server.legacy_storage}
        elif target.path == "/api/packets":
            direction=parse_qs(target.query).get("direction",[""])[0]
            value=[{"created":self.server.bounds[0 if direction=="asc" else 1]}]
        else:
            value={}
        raw = json.dumps(value).encode()
        self.send_response(200 if target.path in {"/api/tray-status","/api/status","/api/packets"} else 404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def wait_until(check, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(.1)
    raise AssertionError("WebView condition timed out")


def is_visible(window):
    user = ctypes.WinDLL("user32")
    user.IsWindowVisible.argtypes = [wintypes.HWND]
    return bool(user.IsWindowVisible(int(window.native.Handle.ToInt64())))


def sized_for_content(panel):
    # WebView defers its viewport layout while hidden; check the measured page
    # and native client area without showing it or stealing the user's focus.
    bounds = panel.window.evaluate_js("({width:document.body.getBoundingClientRect().width,height:document.body.getBoundingClientRect().height})")
    expected = tuple(math.ceil(bounds[key]) for key in ("width", "height"))
    user = ctypes.WinDLL("user32")
    user.GetDpiForWindow.argtypes = [wintypes.HWND]
    user.GetDpiForWindow.restype = wintypes.UINT
    form = panel.window.native
    scale = (user.GetDpiForWindow(int(form.Handle.ToInt64())) or 96) / 96
    actual = form.ClientSize.Width, form.ClientSize.Height
    return panel._size == expected and actual == tuple(int(value * scale) for value in expected)


def run(*, legacy_host=False):
    import webview

    with tempfile.TemporaryDirectory(prefix="tray-panel-check-") as temporary:
        root = Path(temporary)
        for name in RESOURCES:
            shutil.copyfile(UI_DIR / name, root / name)
        backend = ThreadingHTTPServer(("127.0.0.1", 0), StatusHandler)
        backend.daemon_threads = True
        backend.requests = []
        backend.started, backend.release = threading.Event(), threading.Event()
        backend.release.set()
        backend.snapshot = {"connection_state": "connected", "relay_state": "running", "language": "zh",
                            "recorded_requests": 5000, "protected_requests": 34,
                            "recorded_sessions": 12, "first_recorded_at": time.time()-3600, "last_recorded_at": time.time(),
                            "average_compact_input_tokens": 254080.5, "compaction_samples": 31}
        backend.bounds=backend.snapshot["first_recorded_at"],backend.snapshot["last_recorded_at"]
        backend.legacy_storage={"count":5001,"threads":[{"thread":"s0","count":4980}]+
                                [{"thread":f"s{i}","count":1} for i in range(1,12)]+[{"thread":"","count":10}]}
        threading.Thread(target=backend.serve_forever, kwargs={"poll_interval": .05}, daemon=True).start()
        ui = DesktopUI(backend.server_port, ui_dir=root, live_reload=True).start()
        master = webview.create_window("Isolated tray check", "about:blank", hidden=True)
        actions, result = [], {}
        def create_hidden(*args, **kwargs):
            panel.visible = False  # Keep the runtime-created test window hidden even if its page loads immediately.
            return webview.create_window(*args, **kwargs)
        panel = TrayPanel(create_hidden, ui.ui_url, root, lambda: actions.append("main"))
        if legacy_host:
            panel.MIN_WIDTH = 240
            panel._size = (288, 280)
            panel.panel_state = lambda: {"visible": panel.visible and not panel._closed.is_set()}
        expected_width = 240
        master.events.closed += panel.destroy

        def check():
            try:
                panel.prepare()
                assert panel._loaded.wait(25), "panel did not load"
                evaluate = panel.window.evaluate_js
                wait_until(lambda: evaluate("!!window.trayPanel && !!window.pywebview?.api?.panel_ready"))
                result["stage"] = "initial sizing"
                wait_until(lambda: sized_for_content(panel))
                style = evaluate("({background:getComputedStyle(document.body).backgroundColor,"
                                 "width:document.body.getBoundingClientRect().width,"
                                 "overflow:document.body.scrollWidth>document.body.getBoundingClientRect().width,buttons:document.querySelectorAll('.tray-panel button').length})")
                assert style == {"background": "rgb(255, 255, 255)", "width": expected_width, "overflow": False, "buttons": 2}, style
                height = panel._size[1]
                evaluate("const error=document.getElementById('tray-error');error.textContent='Test message';error.hidden=false;window.trayPanel.render({});true")
                result["stage"] = "inline error without resize"
                wait_until(lambda: evaluate("!document.getElementById('tray-error').hidden && getComputedStyle(document.getElementById('tray-status')).display==='none'"))
                assert evaluate("document.querySelector('.floating-feedback').hidden"), "error opened a floating notification"
                assert panel._size[1] == height and sized_for_content(panel), "feedback resized the tray"
                assert not is_visible(panel.window), "resizing exposed a hidden popup"
                evaluate("document.getElementById('tray-error').hidden=true;window.trayPanel.render({});true")
                result["stage"] = "shrinking content"
                wait_until(lambda: panel._size[1] == height and sized_for_content(panel))
                assert not is_visible(panel.window), "check must keep the popup hidden"
                assert backend.requests == [], "startup warmup queried statistics"
                backend.release.clear()
                panel.visible = True
                panel._anchor = (1900, 1040), (0, 0, 1920, 1040)
                evaluate("Object.defineProperty(document,'hidden',{configurable:true,get:()=>false});window.trayPanel.setVisible(true);true")
                assert backend.started.wait(3), "status request did not start"
                assert evaluate("document.querySelector('[data-stat=recorded_requests]').textContent") == "—"
                evaluate("document.dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowDown'}));true")
                assert evaluate("document.activeElement.id") == "tray-open", "menu was not interactive during slow statistics"
                backend.release.set()
                wait_until(lambda: evaluate("document.querySelector('[data-stat=recorded_requests]').textContent==='5,000'"))
                assert evaluate("document.querySelector('[data-stat=average_compact_input_tokens]').textContent") == "254,081"
                assert evaluate("document.querySelector('[data-stat=recorded_sessions]').textContent") == "12"
                assert " – " in evaluate("document.getElementById('tray-record-range').textContent")
                backend.snapshot = {**backend.snapshot, "language": "en", "recorded_requests": 5001}
                wait_until(lambda: evaluate("document.documentElement.lang==='en'&&document.querySelector('[data-stat=recorded_requests]').textContent==='5,001'"))
                assert evaluate("document.getElementById('tray-open').textContent.trim()") == "Open main window"
                assert not evaluate("document.body.scrollWidth>document.body.getBoundingClientRect().width"), "English menu overflows"
                assert set(backend.requests)=={"/api/tray-status"}, "new backend unnecessarily used compatibility queries"
                complete=dict(backend.snapshot)
                backend.snapshot={key:value for key,value in complete.items() if key not in {"recorded_sessions","first_recorded_at","last_recorded_at"}}
                result["stage"]="legacy record scope"
                wait_until(lambda: "/api/status" in backend.requests)
                wait_until(lambda: evaluate("document.querySelector('[data-stat=recorded_sessions]').textContent==='12'&&document.getElementById('tray-record-range').textContent!=='—'"))
                assert evaluate("document.querySelector('[data-stat=recorded_requests]').textContent") == "5,001"
                backend.legacy_storage={**backend.legacy_storage,"threads":backend.legacy_storage["threads"][:1]}
                evaluate("window.__trayTestNow=Date.now;Date.now=()=>window.__trayTestNow()+6000;true")
                result["stage"]="truncated legacy group list"
                wait_until(lambda: evaluate("document.querySelector('[data-stat=recorded_sessions]').textContent==='—'"))
                assert evaluate("document.querySelector('[data-stat=recorded_requests]').textContent") == "5,001"
                assert evaluate("document.getElementById('tray-record-range').textContent") != "—"
                backend.snapshot={**complete,"recorded_requests":0,"recorded_sessions":0,"first_recorded_at":None,"last_recorded_at":None}
                wait_until(lambda: evaluate("document.querySelector('[data-stat=recorded_sessions]').textContent==='0'&&document.getElementById('tray-record-range').textContent==='—'"))
                backend.snapshot=complete
                evaluate("Date.now=window.__trayTestNow;delete window.__trayTestNow;true")
                wait_until(lambda: evaluate("document.querySelector('[data-stat=recorded_sessions]').textContent==='12'"))
                panel.visible = False
                evaluate("window.trayPanel.setVisible(false);true")
                time.sleep(.2)
                requests = len(backend.requests)
                time.sleep(1.2)
                assert len(backend.requests) == requests, "hidden popup kept polling statistics"
                original = ui_build_id(root)
                with (root / "tray.css").open("a", encoding="utf8") as stylesheet:
                    stylesheet.write("\n:root{--tray-width:304px}\n")
                wait_until(lambda: evaluate("document.body.getBoundingClientRect().width===304"))
                wait_until(lambda: sized_for_content(panel))
                assert ui_build_id(root) == original, "tray edit invalidated the main window"
                assert not is_visible(panel.window), "hot reload exposed a hidden popup"
                assert not panel.panel_state()["visible"]
                panel.visible = True
                panel.window.destroy()  # Alt+F4 closes a form through the same cancellable event.
                wait_until(lambda: not panel.panel_state()["visible"])
                assert not panel._closed.is_set(), "Alt+F4 permanently destroyed the reusable panel"
                evaluate("document.getElementById('tray-open').click();true")
                wait_until(lambda: len(actions) == 1)
                evaluate("document.getElementById('tray-exit').click();true")
                wait_until(lambda: len(actions) == 2 and (root / REQUEST_FILE).is_file())
                assert all(urlsplit(path).path in {"/api/tray-status","/api/status","/api/packets"} for path in backend.requests), "panel called a business or stop endpoint"
                for path in backend.requests:
                    if urlsplit(path).path=="/api/packets":
                        query=parse_qs(urlsplit(path).query)
                        assert query.get("limit")==["1"] and query.get("include_get")==["1"] and query.get("view")==["trend"], "legacy fallback loaded request history"
                result.update(passed=True, checks=["hidden startup warmup", f"{expected_width}px layout", "content sizing", "Chinese/English", "responsive before statistics", "record scope",
                                                  "legacy backend scope", "truncated legacy groups", "empty store resets scope",
                                                  "visible polling", "hidden idle", "independent hot reload", "Alt+F4 dismissal", "Open/Exit bridge"])
                result.pop("stage", None)
            except Exception as exc:
                result.update(passed=False, error=f"{type(exc).__name__}: {exc}")
                if panel.window and panel.window.native:
                    form = panel.window.native
                    result["native"] = {"requested": panel._size, "size": [form.Size.Width, form.Size.Height],
                                        "client": [form.ClientSize.Width, form.ClientSize.Height], "border": str(form.FormBorderStyle),
                                        "configured": panel._configured}
                if panel._loaded.is_set():
                    result["geometry"] = panel.window.evaluate_js("({width:innerWidth,height:innerHeight,bodyWidth:document.body.getBoundingClientRect().width,bodyHeight:document.body.getBoundingClientRect().height})")
            finally:
                master.destroy()

        try:
            webview.start(check, gui="edgechromium", private_mode=True, debug=False)
            if not panel._closed.wait(3):
                result.update(passed=False, error="Main window closure did not destroy the panel")
                panel.destroy()
        finally:
            backend.release.set()
            ui.close()
            backend.shutdown()
            backend.server_close()
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(run())
