"""Entry point for the desktop window and its independent background service."""
import ctypes
import json
from pathlib import Path
import sys
from urllib.error import HTTPError

from packet_inspector.server import main as service_main, default_data_dir, argument_parser


def installed_entry(arguments):
    """The installed stable path shares the application binary with workers."""
    if not getattr(sys, "frozen", False):
        return False
    options = argument_parser().parse_known_args(arguments)[0]
    data = options.data_dir.resolve()
    return (Path(sys.executable).resolve() == data / "bin" / "CodexPacketInspector.exe"
            and (data / "router-state.json").is_file())


def main(argv=None):
    provided = list(sys.argv[1:] if argv is None else argv)
    upgrade_requested = "--upgrade" in provided
    quiet = "--quiet" in provided
    provided = [arg for arg in provided if arg not in {"--upgrade", "--quiet"}]
    background, use_tray = "--background" in provided, "--tray" in provided
    arguments = [arg for arg in provided if arg not in {"--background", "--tray"}]
    tray = None
    try:
        if upgrade_requested:
            from packet_inspector.upgrade import run_integrated
            return run_integrated(arguments, quiet=quiet, force=True)
        if installed_entry(arguments):
            from packet_inspector.router import main as entry_main
            return entry_main(provided)
        if not background:
            if getattr(sys, "frozen", False) and "--desktop-check" not in arguments:
                options = argument_parser().parse_known_args(arguments)[0]
                if not Path(sys.executable).resolve().is_relative_to(options.data_dir.resolve() / "bin" / "versions"):
                    from packet_inspector.upgrade import run_integrated
                    return run_integrated(arguments, quiet=quiet)
            from packet_inspector.desktop import run
            return run(arguments)

        def started(server):
            nonlocal tray
            from packet_inspector.desktop import service_arguments, start_process, local_api
            from packet_inspector.server import argument_parser
            from packet_inspector.tray import NativeTray
            options = argument_parser().parse_args(arguments)

            def open_window():
                start_process(service_arguments(options))

            def exit_app():
                try:
                    local_api(server.port, "/api/stop", {}, timeout=5)
                except HTTPError as exc:
                    try:
                        reason = json.load(exc).get("error", "退出未完成，请稍后重试。")
                    except Exception:
                        reason = "退出未完成，请稍后重试。"
                    if reason == "requests_in_flight":
                        reason = "仍有请求正在转发，服务继续运行。请在请求完成后从托盘退出。"
                    open_window()
                    ctypes.windll.user32.MessageBoxW(0, str(reason), "Codex 请求优化器", 0x40)
                except OSError:
                    pass

            tray = NativeTray(open_window, exit_app).start()
            server.desktop_tray = True

        return service_main(arguments, on_started=started if use_tray else None)
    except Exception as exc:
        try:
            folder = argument_parser().parse_known_args(arguments)[0].data_dir
        except (Exception, SystemExit):
            folder = default_data_dir()
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "startup-error.txt").write_text(type(exc).__name__ + ": " + str(exc), encoding="utf8")
        if sys.platform == "win32" and not background and "--desktop-check" not in arguments:
            ctypes.windll.user32.MessageBoxW(0, "启动失败：" + str(exc)[:350] + "\n\n详细记录：" + str(folder / "startup-error.txt"),
                                           "Codex 请求优化器", 0x10)
        return 1
    finally:
        if tray:
            tray.stop()


if __name__ == "__main__":
    raise SystemExit(main())
