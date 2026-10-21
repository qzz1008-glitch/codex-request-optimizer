"""Keep an isolated, hidden desktop window open for a real upgrade test."""
import json
import os
import threading
import time

from . import VERSION


def check_window(window, options, instance):
    report = options.desktop_check
    closed = threading.Event()
    window.events.closed += closed.set
    try:
        window.evaluate_js("document.getElementById('settings-open').click()")
        result = None
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            result = window.evaluate_js("""({
                title: document.title,
                settings: document.getElementById('settings-dialog').open,
                versions: document.getElementById('updates-versions').textContent,
                check: !!document.getElementById('updates-check'),
                install: !!document.getElementById('updates-install')
            })""")
            if result and VERSION in result.get("versions", ""):
                break
            time.sleep(.2)
        passed = bool(result and result["settings"] and result["check"] and result["install"]
                      and VERSION in result["versions"] and result["title"] == "Codex 请求优化器")
        report.write_text(json.dumps({"passed": passed, "version": VERSION, "pid": os.getpid(),
                                      "updates_ui": passed}), encoding="utf8")
        if passed:
            deadline = time.monotonic() + 120
            while report.with_suffix(".hold").exists() and time.monotonic() < deadline:
                if closed.wait(.2):
                    break
    except Exception as exc:
        report.write_text(json.dumps({"passed": False, "error": type(exc).__name__}), encoding="utf8")
    finally:
        if not closed.is_set():
            window.destroy()
