"""Native WebView2 integration checks, restricted to an isolated data directory."""
from __future__ import annotations

import json
import time
import ctypes


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
        result = wait_until(lambda: state()["ready"], "status did not render")
        wait_until(lambda: window.evaluate_js("typeof connectChecking === 'undefined' || !connectChecking"),
                   "first-run connection check did not finish")
        window.evaluate_js("if(document.getElementById('connect-confirm-dialog')?.open)document.getElementById('connect-confirm-cancel').click(); true")
        return result

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
        wait_until(lambda: window.evaluate_js("document.documentElement.classList.contains('desktop-titlebar')"),
                   "desktop title bar did not initialize")
        user = ctypes.WinDLL("user32", use_last_error=True)
        user.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user.GetWindowLongW.restype = ctypes.c_long
        hwnd = int(window.native.Handle.ToInt64())

        def native_frame():
            style = user.GetWindowLongW(hwnd, -16)
            assert not style & 0x00C00000, "duplicate native caption is still visible"
            assert style & 0x00040000, "native resize border was removed"

        native_frame()
        header = window.evaluate_js("""(() => {
          const header=document.querySelector('.app-header'),r=header.getBoundingClientRect();
          const groups=[...header.children].filter(n=>n.getBoundingClientRect().width);
          return {height:r.height,controls:!document.getElementById('window-controls').hidden,
            fits:groups.every(n=>{const b=n.getBoundingClientRect();return b.left>=r.left&&b.right<=r.right&&b.top>=r.top&&b.bottom<=r.bottom;})};
        })()""")
        assert header == {"height": 48, "controls": True, "fits": True}, header
        window.evaluate_js("document.getElementById('window-maximize').click(); true")
        wait_until(lambda: str(window.native.WindowState) == "Maximized", "maximize button did not maximize")
        wait_until(lambda: window.evaluate_js("document.documentElement.classList.contains('desktop-maximized')"),
                   "maximize icon did not update")
        window.evaluate_js("document.getElementById('window-maximize').click(); true")
        wait_until(lambda: str(window.native.WindowState) == "Normal", "restore button did not restore")
        native_frame()
        window.evaluate_js("document.getElementById('window-minimize').click(); true")
        wait_until(lambda: str(window.native.WindowState) == "Minimized", "minimize button did not minimize")
        window.restore()
        wait_until(lambda: str(window.native.WindowState) == "Normal", "window did not restore after minimize")
        window.evaluate_js("document.getElementById('window-close').click(); true")
        lifecycle = window._optimizer_lifecycle
        wait_until(lambda: lifecycle.hidden.is_set() or str(window.native.WindowState) == "Minimized",
                   "close button did not use the existing tray lifecycle")
        assert not lifecycle.closed.is_set(), "close button destroyed the desktop process"
        window.restore()
        report.update(single_title_bar=True, native_resize_border=True, window_buttons=True)
        attention = window._optimizer_lifecycle.attention
        assert attention.hwnd, "taskbar attention suppression was not installed"
        report["taskbar_attention_suppression"] = True
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
        try:
            report["ui_diagnostic"] = window.evaluate_js("""({
              notice:document.getElementById('notice')?.textContent?.slice(0,300),
              dialogs:[...document.querySelectorAll('dialog[open]')].map(n=>n.id)
            })""")
        except Exception:
            pass
    finally:
        options.desktop_check.write_text(json.dumps(report), encoding="utf8")
        getattr(window, "_optimizer_exit", window.destroy)()
