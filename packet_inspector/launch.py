"""Entry point for the desktop window and its independent background service."""
import ctypes
import json
import sys
from urllib.error import HTTPError

from packet_inspector.server import main as service_main, default_data_dir, argument_parser


def main(argv=None):
    provided = list(sys.argv[1:] if argv is None else argv)
    background, use_tray = "--background" in provided, "--tray" in provided
    arguments = [arg for arg in provided if arg not in {"--background", "--tray"}]
    tray = None
    try:
        if not background:
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
                        reason = json.load(exc).get("error", "请先恢复原出口，再退出程序。")
                    except Exception:
                        reason = "请先恢复原出口，再退出程序。"
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
