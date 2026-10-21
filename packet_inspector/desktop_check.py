"""Hidden native-window checks, available only with an isolated --desktop-check."""
from __future__ import annotations

import json
import time

from .frontend_dev import CONFIG_NAME, FrontendDevelopment


def check_window(window, options, instance):
    if options.desktop_check.with_suffix(".hold").exists():
        from .desktop_upgrade_check import check_window as check_upgrade
        return check_upgrade(window, options, instance)
    report = {"passed": False}
    loads = [0]

    def loaded():
        loads[0] += 1

    window.events.loaded += loaded

    def wait_until(predicate, message, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if predicate():
                    return
            except Exception:
                pass  # A navigation may briefly replace the JavaScript context.
            time.sleep(.05)
        raise AssertionError(message)

    def state():
        return window.evaluate_js("""({
          ready: document.getElementById('app-version').textContent.includes('0.'),
          refreshHidden: document.getElementById('ui-refresh').hidden,
          developerHidden: document.getElementById('ui-development-settings').hidden,
          enabled: document.getElementById('ui-development').checked,
          dialog: document.getElementById('settings-dialog').open
        })""")

    def ready():
        wait_until(lambda: state()["ready"], "native UI did not become ready")

    def toggle(enabled):
        window.evaluate_js("document.getElementById('settings-open').click(); true")
        wait_until(lambda: state()["dialog"], "settings did not open")
        previous = loads[0]
        window.evaluate_js("setTimeout(() => document.getElementById('ui-development').click(), 50); true")
        wait_until(lambda: loads[0] > previous, "developer switch did not reload the native window")
        ready()
        result = state()
        assert result["enabled"] is enabled
        assert result["refreshHidden"] is not enabled
        assert not result["developerHidden"]

    def key(expression, should_reload):
        previous = loads[0]
        window.evaluate_js("setTimeout(() => window.dispatchEvent(new KeyboardEvent('keydown', " + expression + ")), 50); true")
        if should_reload:
            wait_until(lambda: loads[0] > previous, "native refresh shortcut did not reload")
            ready()
        else:
            time.sleep(.3)
            assert loads[0] == previous, "a development shortcut was still active while off"

    try:
        ready()
        mode = FrontendDevelopment(options.data_dir / CONFIG_NAME)
        if not mode.available:
            result = state()
            assert result["refreshHidden"] and result["developerHidden"]
            key("{key:'F5'}", False)
            key("{key:'r',ctrlKey:true}", False)
            report.update(passed=True, release_developer_controls_hidden=True,
                          release_shortcuts_inactive=True)
        else:
            assert mode.enabled
            assert mode.directory.is_relative_to(options.data_dir.resolve()), "check resources must be isolated"
            assert not state()["refreshHidden"]
            key("{key:'F5'}", True)
            key("{key:'r',ctrlKey:true}", True)
            previous = loads[0]
            css = mode.directory / "style.css"
            css.write_bytes(css.read_bytes() + b"\n/* isolated native auto-refresh check */\n")
            wait_until(lambda: loads[0] > previous, "source change did not reload the native window")
            ready()
            toggle(False)
            assert not FrontendDevelopment(options.data_dir / CONFIG_NAME).enabled
            key("{key:'F5'}", False)
            key("{key:'r',ctrlKey:true}", False)
            toggle(True)
            assert FrontendDevelopment(options.data_dir / CONFIG_NAME).enabled
            report.update(passed=True, native_f5=True, native_ctrl_r=True,
                          native_auto_refresh=True, off_hides_refresh=True,
                          off_shortcuts_inactive=True, toggle_persisted=True)
    except Exception as exc:
        report["error"] = type(exc).__name__ + ": " + str(exc)
    finally:
        options.desktop_check.write_text(json.dumps(report), encoding="utf8")
        window.destroy()
