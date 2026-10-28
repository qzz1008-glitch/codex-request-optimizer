"""Entry point for the desktop window and its independent background service."""
import ctypes
import json
from pathlib import Path
import sys
import threading

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
    tray, preload = None, None
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
                    return run_integrated(arguments, quiet=quiet, force=quiet)
            from packet_inspector.desktop import run
            return run(arguments)

        def started(server):
            nonlocal tray, preload
            from packet_inspector.desktop import service_arguments, start_process
            from packet_inspector.exit_flow import request_exit
            from packet_inspector.server import argument_parser
            from packet_inspector.tray import NativeTray
            options = argument_parser().parse_args(arguments)

            def open_window():
                start_process(service_arguments(options))

            def exit_app():
                try:
                    request_exit(options.data_dir, open_window)
                except OSError:
                    ctypes.windll.user32.MessageBoxW(0, "无法打开退出确认，中转服务继续运行。请稍后重试。", "Codex 请求优化器", 0x40)

            panel_launch_guard = threading.Lock()
            panel_process = None
            def prepare_panel():
                nonlocal panel_process
                from packet_inspector.window_lifecycle import tray_panel_host_running
                with panel_launch_guard:
                    if tray_panel_host_running(options.data_dir):
                        return
                    if panel_process is None or panel_process.poll() is not None:
                        panel_process = start_process(["--tray-preload"] + service_arguments(options))

            def show_panel():
                from packet_inspector.window_lifecycle import request_tray_panel, tray_panel_ready
                if tray_panel_ready(options.data_dir) and request_tray_panel(options.data_dir):
                    return True
                prepare_panel()
                # The existing native menu remains usable during cold startup.
                # Do not queue a late popup over that menu once WebView is ready.
                return False

            tray = NativeTray(open_window, exit_app, status_reader=server.tray_status,
                              show_panel=show_panel).start()
            server.desktop_tray = True
            def warm_panel():
                try:
                    prepare_panel()
                except OSError:
                    pass
            preload = threading.Thread(target=warm_panel, name="tray-panel-warmup", daemon=True)
            preload.start()

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
        if preload:
            preload.join(timeout=2)
        if tray:
            tray.stop()


if __name__ == "__main__":
    raise SystemExit(main())
