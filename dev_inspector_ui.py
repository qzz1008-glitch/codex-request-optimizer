"""Read-only live frontend preview; never start or restart a forwarding service."""
from __future__ import annotations

import argparse
import http.client
import json
from pathlib import Path
import threading
import webbrowser

from packet_inspector.desktop_ui import DesktopUI
from packet_inspector.frontend_dev import register


def check_relay(port):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        connection.request("GET", "/health")
        response = connection.getresponse()
        health = json.loads(response.read(65536))
        if response.status != 200 or health.get("app") != "codex-packet-inspector":
            raise ValueError("not an inspector service")
        return health.get("version", "unknown")
    finally:
        connection.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=47845, help="frontend preview port")
    parser.add_argument("--relay-port", type=int, default=47844, help="existing forwarding service port")
    parser.add_argument("--open", action="store_true")
    parser.add_argument("--desktop-mode", choices=("on", "off"), help="configure local desktop frontend development")
    parser.add_argument("--data-dir", type=Path, default=Path.home() / ".codex-packet-inspector")
    args = parser.parse_args(argv)
    if args.desktop_mode:
        path = register(args.data_dir, Path(__file__).resolve().parent / "packet_inspector" / "ui",
                        args.desktop_mode == "on")
        print(json.dumps({"desktop_development": args.desktop_mode == "on", "local_config": str(path)}))
        return 0
    if not 1 <= args.port <= 65535 or not 1 <= args.relay_port <= 65535 or args.port == args.relay_port:
        parser.error("Preview and forwarding service require different valid ports.")
    try:
        relay_version = check_relay(args.relay_port)
    except (OSError, http.client.HTTPException, ValueError, AttributeError):
        parser.error("Existing forwarding service unavailable. Preview does not start or restart services.")
    try:
        ui = DesktopUI(args.relay_port, ui_port=args.port, live_reload=True, read_only=True).start()
    except OSError:
        parser.error("Preview port is already in use. Choose another --port; no process was stopped.")
    print(json.dumps({"preview": ui.ui_url, "live_reload": True, "read_only": True,
                      "relay_port": args.relay_port, "relay_version": relay_version}, ensure_ascii=False), flush=True)
    try:
        if args.open:
            webbrowser.open(ui.ui_url)
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        ui.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
