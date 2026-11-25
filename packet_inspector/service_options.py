"""Parse service options without loading the HTTP relay."""
import argparse
import os
from pathlib import Path

from . import VERSION
from .entry_port import ConfiguredPortParser


def default_data_dir():
    # A per-user directory outside AppData also works outside the MSIX app's
    # virtualized filesystem.
    return Path.home() / ".codex-packet-inspector"


def argument_parser():
    parser = ConfiguredPortParser(description="Local Codex packet inspector")
    parser.add_argument("--version", action="version", version="Codex Request Optimizer " + VERSION)
    parser.add_argument("--port", type=int, default=None, help="override the configured entry port")
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    parser.add_argument("--config", type=Path, default=Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "config.toml")
    parser.add_argument("--ui-forward-port", type=int, help="allow this local SSH forwarding port for the browser interface")
    parser.add_argument("--upstream")
    parser.add_argument("--worker-id", default="", help=argparse.SUPPRESS)
    parser.add_argument("--public-port", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--open", action="store_true")
    return parser
