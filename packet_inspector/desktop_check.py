"""Native WebView2 integration checks, restricted to an isolated data directory."""
from __future__ import annotations

import json
import time


def check_window(window, options, instance, ui):
    report = {"passed": False, "development": ui.live_reload}
    loads = [0]

    def loaded():
        loads[0] += 1

    window.events.loaded += loaded

    def wait_until(predicate, message, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                result = predicate()
                if result:
                    return result
            except Exception:
                pass
            time.sleep(.05)
        raise AssertionError(message)

    def state():
        return window.evaluate_js("""({
          ready: typeof state !== 'undefined' && !!state.status?.version,
          refreshHidden: document.getElementById('ui-refresh')?.hidden,
          developerSettings: !!document.getElementById('ui-development-settings'),
          reloadScript: !!document.querySelector('script[src="/__dev__/reload.js"]'),
          dialog: document.getElementById('settings-dialog')?.open
        })""")

    def ready():
        return wait_until(lambda: state()["ready"], "status did not render")

    def reload(action, expected=True):
        prior = loads[0]
        window.evaluate_js("setTimeout(() => {" + action + "}, 50); true")
        if expected:
            wait_until(lambda: loads[0] > prior, "native page did not reload")
            ready()
        else:
            time.sleep(.3)
            assert loads[0] == prior, "release unexpectedly reloaded"

    try:
        ready()
        current = state()
        assert not current["developerSettings"], "runtime developer settings still exist"
        assert current["refreshHidden"] is (not ui.live_reload)
        assert current["reloadScript"] is ui.live_reload
        window.evaluate_js("document.getElementById('settings-open').click(); true")
        wait_until(lambda: state()["dialog"], "settings did not open")
        window.evaluate_js("document.getElementById('settings-dialog').close(); true")
        reload("window.dispatchEvent(new KeyboardEvent('keydown', {key:'F5'}))", ui.live_reload)
        reload("window.dispatchEvent(new KeyboardEvent('keydown', {key:'r', ctrlKey:true}))", ui.live_reload)
        report.update(settings_open=True, refresh_visibility=True, shortcut_behavior=True)
        if ui.live_reload:
            reload("document.getElementById('ui-refresh').click()")
            report["refresh_button"] = True
            if options.desktop_check_ui:
                css = ui.ui_dir / "style.css"
                original = css.read_bytes()
                try:
                    # A hidden integration-test window must still exercise the polling path.
                    window.evaluate_js("Object.defineProperty(document, 'hidden', {configurable:true, get:()=>false}); true")
                    prior = loads[0]
                    css.write_bytes(original + b"\n/* native developer refresh check */\n")
                    wait_until(lambda: loads[0] > prior, "saved frontend did not refresh")
                    ready()
                    report["automatic_refresh"] = True
                finally:
                    css.write_bytes(original)
        report["passed"] = True
    except Exception as exc:
        report["error"] = type(exc).__name__ + ": " + str(exc)
    finally:
        options.desktop_check.write_text(json.dumps(report), encoding="utf8")
        getattr(window, "_optimizer_exit", window.destroy)()
