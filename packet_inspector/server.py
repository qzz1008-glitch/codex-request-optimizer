from __future__ import annotations

import argparse
import hashlib
from collections import Counter
from contextlib import nullcontext
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import ssl
import sys
import threading
import time
from urllib import request as urllib_request
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler
import webbrowser

from . import VERSION
from .analyzer import (MAX_BODY, clean_text, describe_node, encode, inspect_request,
                       classify_request, decode_body, thread_identity, resolve, safe_path, search_body)
from .config_link import ConfigLink, validate_upstream
from .providers import ProviderManager, config_paths
from .entry_port import ConfiguredPortParser, port_status, save_port
from .updates import Updates
from .token_window import TokenWindows
from .response_usage import packet_usage
from .response_summary import ResponseSummary
from .session_summary import SessionSummaries
from .storage import DEFAULT_STORAGE_BYTES, Store
from .storage_paths import resolve_directory, path_settings
from .transport import (AUTO_IMAGE_ZSTD_LEVEL, DEFAULT_IMAGE_ZSTD_LEVEL, DEFAULT_ZSTD_LEVEL,
                        PreparedRequest, prepare_request_info, validate_zstd_level)
from .request_types import is_json_image_request
from .image_upload import optimize_image_request
from .compression_policy import CompressionPolicy, INITIAL_HIGH_BYTES, is_main_request
from .request_context import request_session_context, temporary_request_kind, trend_packet_metadata
from .installation import manage_installation
from .startup import configure_startup, startup_supported
from .native_retry import (DEFAULT_RETRIES, MAX_RETRIES, DEFAULT_DELAY_SECONDS, MAX_DELAY_SECONDS,
                           NativeRetryStream, prepare_http_error)
from .response_events import notify
from .http_io import UPSTREAM_READ_TIMEOUT_SECONDS
from .retry_diagnostics import RetryJournal, ResponseObservation
from .network_diagnostics import NetworkDiagnostics, DiagnosticHTTPHandler, DiagnosticHTTPSHandler
from .loopback_proxy import repair_loopback_proxy
from .usage_trigger import (raw_chunks, request_size as usage_trigger_size,
                            trigger_threshold, DEFAULT_BUDGET_BYTES, MIN_BUDGET_BYTES)
from .overflow import CheckpointStream, OverflowCompactor, OverflowFailure
from .image_config import IMAGE_MODES, ImageToolConfig, WORKER_HEADER, image_mode
from .image_isolation import ImageIsolation, ImageIsolationFailure, check_image_directory, resolve_image_directory, is_analysis_request
from .sharing import Sharing, SharingError, forwarding_headers, allowed_request as sharing_request_allowed
from .sharing_errors import prepare_shared_error
from .shared_egress import SharedEgress
from .egress_transport import EGRESS_HEADER
from .sharing_credentials import CLIENT_HEADER

HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
       "te", "trailer", "transfer-encoding", "upgrade"}
UI_FILES = {"/": ("index.html", "text/html; charset=utf-8"),
            "/i18n.js": ("i18n.js", "text/javascript; charset=utf-8"),
            "/app.js": ("app.js", "text/javascript; charset=utf-8"),
            "/style.css": ("style.css", "text/css; charset=utf-8"),
            "/sharing.js": ("sharing.js", "text/javascript; charset=utf-8"),
            "/sharing.css": ("sharing.css", "text/css; charset=utf-8")}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def outbound_proxies():
    proxies = urllib_request.getproxies()
    # NO_PROXY alone makes urllib skip the Windows system proxy settings.
    if not (set(proxies) - {"no"}):
        registry = getattr(urllib_request, "getproxies_registry", None)
        if registry is not None:
            proxies = {**registry(), **proxies}
    return proxies


STORAGE_LEGACY_BYTES = 1024 * 1024
MIN_STORAGE_BYTES = 16 * STORAGE_LEGACY_BYTES
MAX_STORAGE_BYTES = 8192 * STORAGE_LEGACY_BYTES
MAX_PACKETS = 100_000


def storage_settings(saved):
    values = dict(saved)
    legacy = values.get("max_storage_mb")
    if type(legacy) is not int or not 16 <= legacy <= 8192:
        legacy = None
    size = values.get("max_storage_bytes")
    if (type(size) is not int or not MIN_STORAGE_BYTES <= size <= MAX_STORAGE_BYTES
            or (legacy is not None and (size + STORAGE_LEGACY_BYTES - 1) // STORAGE_LEGACY_BYTES != legacy)):
        size = legacy * STORAGE_LEGACY_BYTES if legacy is not None else DEFAULT_STORAGE_BYTES
    values["max_storage_bytes"] = size
    # Older clients require whole legacy units; round up so a downgrade cannot shrink the limit.
    values["max_storage_mb"] = (size + STORAGE_LEGACY_BYTES - 1) // STORAGE_LEGACY_BYTES
    return values


def retention_settings(saved):
    values = storage_settings(saved)
    if values.get("retention_policy") != "records_first" and values["max_storage_bytes"] == 512 * STORAGE_LEGACY_BYTES:
        # Migrate the old factory quota once; later explicit quota choices survive restarts.
        values["max_storage_bytes"] = DEFAULT_STORAGE_BYTES
        values["max_storage_mb"] = (DEFAULT_STORAGE_BYTES + STORAGE_LEGACY_BYTES - 1) // STORAGE_LEGACY_BYTES
    values["retention_policy"] = "records_first"
    # Read compatibility for older clients; details have no separate product limit.
    values["max_detail_packets"] = values.get("max_packets", 2000)
    return values


def protection_settings(saved, *, default_enabled=False):
    values = dict(saved)
    values.pop("request_compression", None)  # Legacy analysis/optimization modes.
    values.pop("image_upload_optimization_enabled", None)
    if "packet_size_protection_enabled" not in values:
        legacy = ("overflow_compaction_enabled", "native_compact_trigger_enabled")
        values["packet_size_protection_enabled"] = (any(values.get(key) is True for key in legacy)
                                                    if any(key in values for key in legacy) else default_enabled)
    for key in ("overflow_compaction_enabled", "native_compact_trigger_enabled"):
        values.pop(key, None)
    budget = values.get("budget_bytes", DEFAULT_BUDGET_BYTES)
    values["budget_bytes"] = (max(MIN_BUDGET_BYTES, min(MAX_BODY, budget))
                              if type(budget) is int else DEFAULT_BUDGET_BYTES)
    values["auto_zstd_level_enabled"] = (values.get("auto_zstd_level_enabled") is True
                                         and values["packet_size_protection_enabled"] is True)
    for key, default in (("sharing_port", 47846), ("egress_port", 47847)):
        value = values.get(key)
        values[key] = value if type(value) is int and 1 <= value <= 65535 else default
    return retention_settings(values)


class InspectorServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, data_dir, config_path, upstream=None, *, worker_id="", public_port=None, worker_token=""):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.settings_path = self.data_dir / "settings.json"
        self.link = ConfigLink(Path(config_path), self.data_dir / "codex-link.json")
        self.guard = threading.RLock()
        settings_exist = self.settings_path.exists()
        saved = json.loads(self.settings_path.read_text(encoding="utf8")) if settings_exist else {}
        self.settings = {"relay_token": secrets.token_urlsafe(32),
                         "capture_enabled": True, "budget_bytes": DEFAULT_BUDGET_BYTES,
                         "max_packets": 2000,
                         "zstd_level": DEFAULT_ZSTD_LEVEL,
                         "image_zstd_level": DEFAULT_IMAGE_ZSTD_LEVEL,
                         "packet_size_protection_enabled": False,
                         "ui_language": "zh",
                         "start_on_login": False, "capacity_retry_enabled": False,
                         "capacity_retry_max_retries": DEFAULT_RETRIES,
                         "capacity_retry_delay_seconds": DEFAULT_DELAY_SECONDS,
                         "image_isolation_enabled": False, "image_analysis_mode": "native", "image_save_dir": "",
                         "auto_detect_providers": True, "provider_routes": {},
                          "log_dir": "", "cache_dir": "",
                         **protection_settings(saved)}
        self.settings.pop("ui_token", None)
        self.settings["image_analysis_mode"] = image_mode(saved)
        self.settings["image_isolation_enabled"] = self.settings["image_analysis_mode"] == "optimized"
        for key, default in (("zstd_level", DEFAULT_ZSTD_LEVEL), ("image_zstd_level", DEFAULT_IMAGE_ZSTD_LEVEL)):
            try:
                self.settings[key] = validate_zstd_level(self.settings[key])
            except ValueError:
                self.settings[key] = default
        limit = self.settings["capacity_retry_max_retries"]
        self.settings["capacity_retry_max_retries"] = (
            min(limit, MAX_RETRIES) if isinstance(limit, int) and not isinstance(limit, bool) and limit >= 1
            else DEFAULT_RETRIES)
        delay = self.settings["capacity_retry_delay_seconds"]
        self.settings["capacity_retry_delay_seconds"] = (
            min(delay, MAX_DELAY_SECONDS) if isinstance(delay, int) and not isinstance(delay, bool) and delay >= 1
            else DEFAULT_DELAY_SECONDS)
        if upstream:
            self.settings["upstream"] = validate_upstream(upstream)
        elif not self.settings.get("upstream"):
            self.settings["upstream"] = self.link.profile()["upstream"]
        validate_upstream(self.settings["upstream"])
        self.capture_errors = 0
        self.active_requests = 0
        self.worker_id, self.worker_token = worker_id, worker_token
        self.presentation = None
        self.worker_state = "standby" if worker_id else "active"
        self.maintenance_guard = threading.RLock()
        self.maintenance_enabled = not bool(worker_id)
        super().__init__(address, Handler)
        self.port = self.server_address[1]
        self.public_port = public_port or self.port
        self.provider_guard = threading.RLock()
        self.sharing = Sharing(self)
        self.egress = SharedEgress(self)
        self.provider_fingerprint = None
        self.provider_sync_error = ""
        self.provider_sync_result = {}
        self.connection_reload_required = bool(self.sharing.recovered)
        self.provider_next_scan = 0
        self.upgrade_draining = False
        self.token_windows = TokenWindows()
        self.session_summaries = SessionSummaries(self.token_windows)
        self.cache_dir = resolve_directory(self.data_dir, self.settings["cache_dir"])
        self.log_dir = resolve_directory(self.data_dir, self.settings["log_dir"])
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.store = Store(self.cache_dir, self.settings["max_storage_bytes"],
                           self.settings["max_packets"], capture_owner="relay:" + worker_id if worker_id else "relay")
        self.retry_journal = RetryJournal(self.log_dir)
        if not worker_id:
            self.save_settings()
        ssl_context = ssl.create_default_context()
        proxies = outbound_proxies()
        self.network_diagnostics = NetworkDiagnostics(self.log_dir, proxies)
        self.opener = build_opener(ProxyHandler(proxies), DiagnosticHTTPHandler(),
                                  DiagnosticHTTPSHandler(context=ssl_context), NoRedirect())
        self.local_opener = build_opener(ProxyHandler({}), DiagnosticHTTPHandler(), DiagnosticHTTPSHandler(), NoRedirect())
        self.overflow = OverflowCompactor(self.cache_dir)
        self.compression_policy = CompressionPolicy(self.store)
        self.image_config = ImageToolConfig(self.link, self.data_dir, lambda: self.endpoint)
        self.image_isolation = ImageIsolation(self.data_dir, self.settings["image_save_dir"])
        self.default_image_save_dir = resolve_image_directory(self.data_dir)
        if self.settings["image_isolation_enabled"] and self.image_config.state.get("enabled"):
            try:
                with self.image_config.change("optimized"):
                    pass
            except (OSError, ValueError) as exc:
                self.image_config.installation_error = clean_text(str(exc))[:250]
        self.updates = Updates(self.data_dir, self.link.config, self.public_port)
        self.analysis_errors = 0
        self.maintenance_errors = 0
        self.installation_guard = threading.Lock()
        self.startup_state_known = True
        if not worker_id and startup_supported(self.data_dir):
            # Reflect the existing login task without changing it during upgrade.
            try:
                installed = manage_installation("Status", self.data_dir, self.link.config, self.port)
                self.save_settings({**self.settings, "start_on_login": bool(installed.get("login_start"))})
            except Exception:
                # An optional Windows setting must not prevent the relay from starting.
                self.startup_state_known = False
        self.analysis_stop = threading.Event()
        self.analysis_thread = threading.Thread(target=self._classify_pending, daemon=True)
        self.analysis_thread.start()

    def _classify_pending(self):
        # Update classifications of stored records without delaying list requests.
        while not self.analysis_stop.is_set():
            with self.maintenance_guard:
                processed = self._maintenance_step() if self.maintenance_enabled else 0
            self.analysis_stop.wait(0.02 if processed else 2)

    def _maintenance_step(self):
        try:
            processed = self.store.compact_usage_records(limit=16) + self.store.reclassify_pending(limit=8)
        except Exception:
            self.analysis_errors += 1
            processed = 0
        try:
            if self.settings["auto_detect_providers"] and self.provider_fingerprint is not None:
                fingerprint = self._provider_fingerprint()
                if fingerprint != self.provider_fingerprint or time.monotonic() >= self.provider_next_scan:
                    self.auto_sync_providers()
            self.overflow.maintenance()
            self.updates.poll()
            self.store.reclaimer.run(self.overflow.busy)
            self.retry_journal.prune()
            self.retry_journal.reclaimer.run(self.overflow.busy)
        except Exception:
            self.maintenance_errors += 1
        return processed

    def worker_control(self, data):
        action = data.get("action")
        with self.maintenance_guard:
            if action == "pause":
                self.maintenance_enabled = False
                self.worker_state = "draining"
            elif action == "activate":
                with self.guard:
                    if self.settings_path.exists():
                        saved = json.loads(self.settings_path.read_text(encoding="utf8"))
                        self.settings = {**self.settings, **protection_settings(saved)}
                    if "public_port" in data:
                        from .entry_port import validate_port
                        self.public_port = validate_port(data["public_port"])
                    self.save_settings()
                    self.store.configure_limits(self.settings["max_storage_bytes"],
                                                self.settings["max_packets"])
                if self.settings["auto_detect_providers"]:
                    self.auto_sync_providers()
                self.worker_state = "active"
                self.maintenance_enabled = bool(data.get("maintenance", True))
            elif action == "maintenance":
                self.maintenance_enabled = self.worker_state == "active" and bool(data.get("enabled"))
            elif action == "recover":
                for owner in data.get("owners", []):
                    if not isinstance(owner, str) or not re.fullmatch(r"relay(?::[0-9a-f]{32})?", owner):
                        raise ValueError("invalid capture owner")
                    self.store.recover_owner(owner)
            elif action == "installation_refresh":
                if startup_supported(self.data_dir):
                    installed = manage_installation("Status", self.data_dir, self.link.config, self.public_port)
                    manage_installation("Install", self.data_dir, self.link.config, self.public_port)
                    if installed.get("login_start"):
                        configure_startup(True, self.data_dir, self.link.config, self.public_port)
                    self.save_settings({**self.settings, "start_on_login": bool(installed.get("login_start"))})
                    self.startup_state_known = True
            elif action == "desktop_refresh":
                if self.presentation is None:
                    raise ValueError("desktop integration is unavailable")
                self.presentation.commit()
            else:
                raise ValueError("invalid worker action")
        return {"worker_id": self.worker_id, "state": self.worker_state}

    def _provider_fingerprint(self):
        paths = config_paths(self.link.config)
        paths.append(self.link.config.parent / "auth.json")
        return tuple((str(path), path.stat().st_mtime_ns, path.stat().st_size)
                     for path in paths if path.exists())

    def auto_sync_providers(self):
        # Only explicit activation may change a disconnected installation.
        if not self.settings["auto_detect_providers"] or not self.connection_profile()["active"]:
            return {"enabled": False}
        return self.sync_providers()

    def sync_providers(self):
        with self.provider_guard:
            if self.sharing.remote:
                changed = self.sharing.provider.set_retry(self.settings["capacity_retry_max_retries"]
                    if self.settings["capacity_retry_enabled"] else None)
                self.connection_reload_required |= changed
                self.provider_fingerprint = self._provider_fingerprint()
                self.provider_next_scan = time.monotonic() + 30
                self.provider_sync_error = ""
                self.provider_sync_result = {"active": True, "changed": changed, "reload_required": changed,
                                            "provider_count": 1, "config_files": 1, "backups": [], "warnings": []}
                return self.provider_sync_result
            if not self.settings["auto_detect_providers"]:
                return {"enabled": False}
            manager = ProviderManager(self.link, self.endpoint, self.settings["upstream"])
            def publish(routes):
                with self.guard:
                    self.save_settings({**self.settings, "provider_routes": routes})
            try:
                result = manager.sync(self.settings["provider_routes"], publish,
                                      retry_limit=self.settings["capacity_retry_max_retries"]
                                      if self.settings["capacity_retry_enabled"] else None)
            except Exception as exc:
                self.provider_sync_error = type(exc).__name__
                self.provider_fingerprint = self._provider_fingerprint()
                self.provider_next_scan = time.monotonic() + 30
                raise
            self.provider_fingerprint = self._provider_fingerprint()
            self.provider_sync_error = ""
            self.provider_sync_result = result
            self.connection_reload_required |= result.get("reload_required", False)
            self.provider_next_scan = time.monotonic() + 30
            return result

    def set_connection(self, connected):
        with self.provider_guard:
            if self.sharing.remote:
                self.sharing.disconnect()
            previous = self.settings["auto_detect_providers"]
            before = self._diagnostic_profile() if connected else {}
            proxy_repair = None
            try:
                if connected:
                    proxy_repair = repair_loopback_proxy()
                    self.save_settings({**self.settings, "auto_detect_providers": True})
                    result = self.sync_providers()
                    result["loopback_proxy"] = proxy_repair
                    result["reload_required"] = bool(result.get("reload_required") or proxy_repair["changed"])
                    self.connection_reload_required |= result["reload_required"]
                    self.network_diagnostics.connection(before, self._diagnostic_profile(),
                        self.settings["provider_routes"], self.settings, result=result)
                    return result
                state = self.link._state()
                if not state:
                    result = {"active": False, "reload_required": False}
                else:
                    # Upgrade legacy recovery data before disconnecting, so old
                    # chat IDs retain a direct, native-compression-capable route.
                    if not state.get("connection_policy"):
                        self.save_settings({**self.settings, "auto_detect_providers": True})
                        self.sync_providers()
                    result = self.link.restore()
                self.save_settings({**self.settings, "auto_detect_providers": False})
                self.connection_reload_required |= result.get("reload_required", False)
                return result
            except Exception as exc:
                if connected:
                    self.network_diagnostics.connection(before, self._diagnostic_profile(),
                        self.settings["provider_routes"], self.settings,
                        result={"loopback_proxy": proxy_repair}, error=exc)
                try:
                    keep_active = self.connection_profile()["active"]
                    if not connected or proxy_repair is not None:
                        keep_active |= previous
                except (OSError, ValueError):
                    keep_active = previous if not connected or proxy_repair is not None else False
                self.save_settings({**self.settings, "auto_detect_providers": keep_active})
                raise

    def _diagnostic_profile(self):
        try:
            profile = self.link.profile()
            _, data = self.link._read()
            provider = data.get("model_providers", {}).get(profile.get("provider"), {})
            profile["connection_settings"] = {
                "wire_api": provider.get("wire_api", "responses"),
                "supports_websockets": provider.get("supports_websockets"),
                "requires_openai_auth": provider.get("requires_openai_auth"),
                "http_header_names": [str(k)[:160] for k in list(provider.get("http_headers", {}))[:32]],
                "env_header_names": [str(k)[:160] for k in list(provider.get("env_http_headers", {}))[:32]],
                "native_request_compression": data.get("features", {}).get("enable_request_compression")}
            return profile
        except Exception as exc:
            return {"read_error": type(exc).__name__}

    def server_close(self):
        # TCPServer also calls this when binding fails, before worker setup.
        sharing = getattr(self, "sharing", None)
        if sharing is not None:
            try:
                sharing.close()
            except Exception:
                pass  # Retain recovery metadata, but always close the ordinary relay.
        egress = getattr(self, "egress", None)
        if egress is not None:
            try:
                egress.close()
            except Exception:
                pass  # Optional exit cleanup must not prevent ordinary relay cleanup.
        presentation = getattr(self, "presentation", None)
        if presentation is not None:
            presentation.stop()
        stop = getattr(self, "analysis_stop", None)
        if stop is not None:
            stop.set()
        worker = getattr(self, "analysis_thread", None)
        if worker is not None and worker.is_alive():
            worker.join(timeout=2)
        super().server_close()

    def save_settings(self, candidate=None):
        staging = self.settings_path.with_suffix(".new")
        with self.guard:
            value = protection_settings(self.settings if candidate is None else candidate)
            staging.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf8")
            staging.replace(self.settings_path)
            self.settings = value

    @property
    def ui_url(self):
        return f"http://127.0.0.1:{self.port}/"

    @property
    def endpoint(self):
        return f"http://127.0.0.1:{self.public_port}/relay/{self.settings['relay_token']}"

    def connection_profile(self):
        profile = self.link.profile()
        actual = profile.pop("base_url", "")
        prefix = self.endpoint + "/p/"
        matches = (actual == self.endpoint or isinstance(actual, str) and actual.startswith(prefix)
                   and actual[len(prefix):] in self.settings["provider_routes"])
        matches |= bool(self.sharing.remote and actual == self.sharing.client_endpoint)
        profile["endpoint_matches"] = bool(matches)
        profile["active"] = bool(matches)
        return profile

    def tray_status(self):
        try:
            connection = "connected" if self.connection_profile().get("active") else "disconnected"
        except Exception:
            connection = "unknown"
        try:
            statistics = self.store.tray_statistics()
        except (OSError, sqlite3.Error):
            statistics = {}
        return {"connection_state": connection,
                "relay_state": "running" if self.worker_state == "active" and not self.upgrade_draining else "switching",
                "language": self.settings["ui_language"], **statistics}

    def status(self):
        try:
            profile = self.connection_profile()
        except Exception as exc:
            profile = {"active": False, "config_error": type(exc).__name__}
        storage = self.store.stats()
        return {"version": VERSION, "ui_version": VERSION, "capture_enabled": self.settings["capture_enabled"],
                "connection_state": "connected" if profile.get("active") else "disconnected",
                "connection_policy": 1, "connection_reload_required": self.connection_reload_required,
                "trend_packets_supported": True,
                "zstd_level": self.settings["zstd_level"],
                "image_zstd_level": self.settings["image_zstd_level"],
                "auto_zstd_level_enabled": self.settings["auto_zstd_level_enabled"],
                "auto_image_zstd_level": AUTO_IMAGE_ZSTD_LEVEL,
                "ui_language": self.settings["ui_language"],
                "packet_size_protection_enabled": self.settings["packet_size_protection_enabled"],
                "overflow_compaction_enabled": self.settings["packet_size_protection_enabled"],
                "native_compact_trigger_enabled": self.settings["packet_size_protection_enabled"],
                "native_compact_trigger_bytes": trigger_threshold(self.settings["budget_bytes"]),
                "packet_size_min_bytes": MIN_BUDGET_BYTES, "packet_size_max_bytes": MAX_BODY,
                "start_on_login": self.settings["start_on_login"],
                "start_on_login_supported": startup_supported(self.data_dir),
                "startup_state_known": self.startup_state_known,
                "capacity_retry_enabled": self.settings["capacity_retry_enabled"],
                "capacity_retry_max_retries": self.settings["capacity_retry_max_retries"],
                "capacity_retry_delay_seconds": self.settings["capacity_retry_delay_seconds"],
                "capacity_retry_mode": "native",
                "capacity_retry_supported": True,
                "sharing_settings_supported": True,
                "sharing_port": self.settings["sharing_port"], "egress_port": self.settings["egress_port"],
                "capacity_retry_diagnostics_supported": True,
                "capacity_retry_diagnostics_errors": self.retry_journal.errors,
                "capacity_retry_diagnostics_last_error": self.retry_journal.last_error,
                "network_diagnostics_supported": True,
                "loopback_proxy_repair_supported": True,
                "network_diagnostics_errors": self.network_diagnostics.errors,
                "storage_paths_supported": True,
                "log_dir": str(resolve_directory(self.data_dir, self.settings["log_dir"])),
                "cache_dir": str(resolve_directory(self.data_dir, self.settings["cache_dir"])),
                "active_log_dir": str(self.log_dir), "active_cache_dir": str(self.cache_dir),
                "storage_paths_pending": (resolve_directory(self.data_dir, self.settings["log_dir"]) != self.log_dir
                                          or resolve_directory(self.data_dir, self.settings["cache_dir"]) != self.cache_dir),
                "image_isolation_enabled": self.settings["image_isolation_enabled"],
                "image_analysis_mode": self.settings["image_analysis_mode"],
                "image_isolation_supported": True, "image_tool": self.image_config.status(),
                "image_save_dir": str(self.image_isolation.directory),
                "image_save_dir_default": str(self.default_image_save_dir), "image_save_dir_supported": True,
                "budget_bytes": self.settings["budget_bytes"], "upstream": self.settings["upstream"],
                "max_storage_mb": self.settings["max_storage_mb"], "max_packets": self.settings["max_packets"],
                "packet_limit_max": MAX_PACKETS,
                "max_storage_bytes": self.settings["max_storage_bytes"],
                "storage_limit_min_bytes": MIN_STORAGE_BYTES, "storage_limit_max_bytes": MAX_STORAGE_BYTES,
                "max_detail_packets": self.settings["max_detail_packets"],
                "retention_policy": self.settings["retention_policy"],
                "capture_errors": self.capture_errors, "analysis_errors": self.analysis_errors,
                "maintenance_errors": self.maintenance_errors, "overflow_cache": self.overflow.cache_status(),
                "active_requests": self.active_requests,
                "relay_active_requests": self.active_requests,
                "auto_detect_providers": self.settings["auto_detect_providers"],
                "provider_routing_supported": True, "upgrade_drain_supported": True,
                "updates_supported": True,
                "entry_port_supported": True, "entry_port": port_status(self.data_dir, self.public_port),
                "worker_id": self.worker_id, "worker_state": self.worker_state,
                "public_port": self.public_port,
                "provider_count": self.provider_sync_result.get("provider_count", len({r["provider"] for r in self.settings["provider_routes"].values()})),
                "provider_sync_error": self.provider_sync_error,
                "provider_warnings": self.provider_sync_result.get("warnings", []),
                "desktop_tray": bool(getattr(self, "desktop_tray", False)),
                "desktop_refresh_supported": self.presentation is not None,
                "desktop_integration_error": self.presentation.error if self.presentation else "",
                "data_dir": str(self.data_dir), "profile": profile, "storage": storage,
                "scope": "HTTP requests through configured providers; external overrides and WebSockets are not captured",
                "capture_point": "Codex app exit, before any existing downstream gateway"}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "CodexPacketInspector/" + VERSION

    def log_message(self, *_args):
        pass  # Do not log URLs containing capabilities, request bodies, or headers.

    def _valid_host(self):
        allowed = {f"{host}:{port}" for host in ("127.0.0.1", "localhost")
                   for port in (self.server.port, self.server.public_port)}
        return self.headers.get("Host", "") in allowed

    def _valid_origin(self):
        origin = self.headers.get("Origin")
        return not origin or origin in {f"http://{host}:{port}" for host in ("127.0.0.1", "localhost")
                                       for port in (self.server.port, self.server.public_port)}

    def _json(self, status, payload, headers=None):
        self._bytes(status, encode(payload), "application/json; charset=utf-8", headers)

    def _bytes(self, status, data, content_type, headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Connection", "close")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)
        self.close_connection = True

    def _body(self, limit=MAX_BODY):
        transfer = self.headers.get("Transfer-Encoding", "").lower()
        if transfer and transfer != "chunked":
            raise ValueError("unsupported transfer encoding")
        if transfer:
            if self.headers.get("Content-Length"):
                raise ValueError("ambiguous request framing")
            chunks, size = [], 0
            while True:
                line = self.rfile.readline(128)
                length = int(line.split(b";", 1)[0].strip(), 16)
                if length < 0 or size + length > limit:
                    raise ValueError("request too large")
                if not length:
                    # Trailers are framing, not an additional request body.
                    trailer_bytes = 0
                    while True:
                        trailer = self.rfile.readline(8192)
                        trailer_bytes += len(trailer)
                        if trailer_bytes > 65536:
                            raise ValueError("trailers too large")
                        if trailer in {b"\r\n", b"\n", b""}:
                            break
                    return b"".join(chunks)
                chunk = self.rfile.read(length)
                if len(chunk) != length or self.rfile.read(2) != b"\r\n":
                    raise ValueError("incomplete chunked body")
                chunks.append(chunk)
                size += length
        length = int(self.headers.get("Content-Length", "0"))
        if length < 0 or length > limit:
            raise ValueError("request too large")
        result = self.rfile.read(length)
        if len(result) != length:
            raise ValueError("incomplete request body")
        return result

    def _handle(self):
        self.connection.settimeout(600)
        if not self._valid_host() or not self._valid_origin():
            self._json(403, {"error": "local_origin_required"})
            return
        path = urlsplit(self.path).path
        if path == "/__worker__/control":
            if (self.command != "POST" or not self.server.worker_id or
                    not secrets.compare_digest(self.headers.get("X-Inspector-Control", ""), self.server.worker_token)):
                self._json(403, {"error": "worker_control_required"})
                return
            try:
                data = json.loads(self._body(8192))
                if not isinstance(data, dict):
                    raise ValueError("invalid worker control")
                if data.get("action") == "stop":
                    with self.server.guard:
                        if self.server.active_requests:
                            self._json(409, {"error": "requests_in_flight"})
                            return
                        self.server.upgrade_draining = True
                    self._json(200, {"stopped": True})
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                else:
                    self._json(200, self.server.worker_control(data))
            except (OSError, ValueError, TypeError, RuntimeError) as exc:
                self._json(400, {"error": type(exc).__name__})
            return
        relay_prefix = "/relay/" + self.server.settings["relay_token"]
        if path.startswith("/mcp/image/"):
            self._json(404, {"error": "image_tool_uses_stdio"})
            return
        if path.startswith(relay_prefix + "/"):
            sharing_prefix = urlsplit(self.server.sharing.client_endpoint).path
            if path.startswith(sharing_prefix + "/"):
                self._relay(sharing_prefix)
                return
            if path.startswith(relay_prefix + "/sharing/"):
                self._json(404, {"error": "unknown_sharing_route"})
                return
            self._relay(relay_prefix)
            return
        if path in UI_FILES and self.command == "GET":
            name, kind = UI_FILES[path]
            self._bytes(200, (Path(__file__).with_name("ui") / name).read_bytes(), kind)
            return
        if path == "/health" and self.command == "GET":
            self._json(200, {"ok": True, "app": "codex-packet-inspector", "version": VERSION,
                             "worker_id": self.server.worker_id})
            return
        try:
            self._api(path)
        except (KeyError, IndexError):
            self._json(404, {"error": "记录或字段不存在"})
        except (ValueError, TypeError) as exc:
            self._json(400, {"error": clean_text(str(exc))[:250]})
        except Exception as exc:
            self._json(500, {"error": type(exc).__name__})

    def _api(self, path):
        query = parse_qs(urlsplit(self.path).query)
        one = lambda k, default="": query.get(k, [default])[0]
        if path == "/api/status" and self.command == "GET":
            self._json(200, self.server.status())
        elif path == "/api/sharing" and self.command == "GET":
            self.server.sharing.refresh_peers()
            self._json(200, self.server.sharing.status())
        elif path == "/api/sharing" and self.command == "POST":
            data = json.loads(self._body(4096))
            if not isinstance(data, dict):
                raise ValueError("invalid sharing action")
            try:
                with self.server.provider_guard:
                    action = data.get("action")
                    if action == "credential_issue":
                        if data.get("kind") == "quota":
                            result = self.server.sharing.issue(label=data.get("label", ""),
                                expires_at=data.get("expires_at"), budget=data.get("budget", 1),
                                weekly_reset=data.get("weekly_reset", False))
                        elif data.get("kind") == "exit":
                            issued = self.server.egress.issue(label=data.get("label", ""), expires_at=data.get("expires_at"))
                            result = {**self.server.sharing.status(), "link": issued["link"], "issued_id": issued["issued_id"]}
                        else:
                            raise ValueError("invalid credential kind")
                    elif action in {"credential_edit", "credential_reset"}:
                        vault = self.server.sharing.vault
                        reset = action == "credential_reset"
                        draft = vault.update(data.get("id"), data, reset=reset, dry_run=True)
                        controller = self.server.sharing if draft["kind"] == "quota" else self.server.egress
                        if draft["direction"] == "out" and data.get("enabled") is True:
                            controller.ensure_service()
                        vault.update(data.get("id"), data, reset=reset)
                        if draft["direction"] == "out" and not any(row["kind"] == draft["kind"] and row["direction"] == "out"
                                   and row["enabled"] and not row["expired"] for row in vault.rows()):
                            if draft["kind"] == "quota":
                                self.server.sharing.stop_host()
                            else:
                                self.server.egress.stop()
                        result = self.server.sharing.status()
                    elif action == "credential_delete":
                        result = self.server.sharing.delete_credential(data.get("id"))
                    elif action == "credential_copy":
                        rows = self.server.sharing.vault.rows()
                        row = next((row for row in rows if row["id"] == data.get("id")), None)
                        if not row or row["direction"] != "out":
                            raise SharingError("凭据不存在。")
                        controller = self.server.sharing if row["kind"] == "quota" else self.server.egress
                        controller.ensure_service()
                        result = {**self.server.sharing.status(), "link": controller.credential_link(row["id"])}
                    elif action == "credential_use":
                        if data.get("kind") == "quota":
                            result = self.server.sharing.connect(data.get("link"), label=data.get("label", ""), identifier=data.get("id"))
                        elif data.get("kind") == "exit":
                            self.server.egress.select("peer", data.get("link", ""), label=data.get("label", ""), identifier=data.get("id"))
                            result = self.server.sharing.status()
                        else:
                            raise ValueError("invalid credential kind")
                    elif action == "host_resume":
                        self.server.sharing.ensure_service()
                        result = self.server.sharing.status()
                    elif action == "egress_resume":
                        self.server.egress.ensure_service()
                        result = self.server.sharing.status()
                    elif action == "host_start":
                        if "token_limit" in data:
                            raise SharingError("共享额度现按美元设置，请更新客户端。")
                        result = self.server.sharing.start_host(data.get("address"), data.get("port"),
                            data.get("hours", 24), budget=data.get("budget", 1))
                    elif action == "host_stop":
                        result = self.server.sharing.stop_host()
                    elif action == "host_rotate":
                        result = self.server.sharing.rotate()
                    elif action == "client_connect":
                        result = self.server.sharing.connect(data.get("link"))
                    elif action == "client_disconnect":
                        result = self.server.sharing.disconnect()
                    elif action in {"egress_start", "egress_stop", "egress_rotate", "egress_select", "egress_link"}:
                        if action == "egress_start":
                            exit_result = self.server.egress.start(data.get("address"), data.get("port"), data.get("hours", 24))
                        elif action == "egress_stop":
                            exit_result = self.server.egress.stop()
                        elif action == "egress_rotate":
                            exit_result = self.server.egress.rotate()
                        elif action == "egress_select":
                            exit_result = self.server.egress.select(data.get("mode"), data.get("link", ""))
                        else:
                            exit_result = {"link": self.server.egress.local_link}
                        result = self.server.sharing.status()
                        if exit_result.get("link"):
                            result["egress_link"] = exit_result["link"]
                    else:
                        raise ValueError("invalid sharing action")
                self._json(200, result)
            except SharingError as exc:
                self._json(400, {"error": str(exc)})
        elif path == "/api/tray-status" and self.command == "GET":
            self._json(200, self.server.tray_status())
        elif path == "/api/network-diagnostics" and self.command == "GET":
            headers = ({"Content-Disposition": 'attachment; filename="codex-network-diagnostics.json"'}
                       if one("download") == "1" else None)
            self._json(200, self.server.network_diagnostics.export(), headers)
        elif path == "/api/retry-diagnostics" and self.command == "GET":
            self._json(200, {"records": self.server.retry_journal.list(
                session_id=one("session_id"), packet_id=one("packet_id"), limit=int(one("limit", "50")),
                before=float(one("before")) if one("before") else None)})
        elif path.startswith("/api/retry-diagnostics/") and self.command == "GET":
            record = self.server.retry_journal.get(path.rsplit("/", 1)[1])
            headers = ({"Content-Disposition": 'attachment; filename="codex-retry-' + record["id"] + '.json"'}
                       if one("download") == "1" else None)
            self._json(200, record, headers)
        elif path == "/api/updates" and self.command == "GET":
            self._json(200, self.server.updates.status())
        elif path == "/api/updates" and self.command == "POST":
            data = json.loads(self._body(8192))
            if not isinstance(data, dict):
                raise ValueError("invalid update request")
            self.server.updates.port = self.server.public_port
            result = (self.server.updates.configure(data) if data.get("action") == "configure"
                      else self.server.updates.start(data.get("action")))
            self._json(200, result)
        elif path == "/api/session-summaries" and self.command == "GET":
            try:
                self._json(200, self.server.session_summaries.read(self.server.cache_dir))
            except (OSError, ValueError, TypeError, AttributeError, sqlite3.Error):
                self._json(200, {"state": "unavailable", "sessions": []})
        elif path == "/api/installation" and self.command in {"GET", "POST"}:
            action = "Status" if self.command == "GET" else json.loads(self._body(1024)).get("action")
            if action not in {"Status", "Install", "Remove"}:
                raise ValueError("invalid installation action")
            with self.server.installation_guard:
                result = manage_installation(action, self.server.data_dir,
                                             self.server.link.config, self.server.port)
            self._json(200, result)
        elif path == "/api/context-settings" and self.command == "GET":
            with self.server.guard:
                result = self.server.link.context_settings()
            self._json(200, result)
        elif path == "/api/context-settings" and self.command == "POST":
            data = json.loads(self._body(8192))
            with self.server.guard:
                result = self.server.link.save_context_settings(data)
            self._json(200, result)
        elif path == "/api/packet-filters" and self.command == "GET":
            self._json(200, self.server.store.filter_options())
        elif path == "/api/packets" and self.command == "GET":
            before = float(one("before")) if one("before") else None
            since = float(one("since")) if one("since") else None
            self._json(200, self.server.store.list(limit=min(300, max(1, int(one("limit", "150")))),
                        thread=one("thread")[:200], query=one("q")[:200], before=before,
                        include_get=one("include_get") == "1", sort=one("sort", "created"),
                        direction=one("direction", "desc"), after=one("after")[:64] or None,
                        kind=one("kind"), status=one("status"), thread_unknown=one("thread_unknown") == "1",
                        since=since, trend=one("view") == "trend"))
        elif path == "/api/entry-port" and self.command == "GET":
            self._json(200, port_status(self.server.data_dir, self.server.public_port))
        elif path == "/api/entry-port" and self.command == "POST":
            data = json.loads(self._body(8192))
            if not isinstance(data, dict):
                raise ValueError("invalid entry port settings")
            with self.server.guard:
                result = save_port(self.server.data_dir, self.server.public_port, data.get("port"))
            self._json(200, result)
        elif path == "/api/settings" and self.command == "POST":
            data = json.loads(self._body(8192))
            checked_paths = path_settings(self.server.data_dir, data)
            with self.server.provider_guard, self.server.guard:
                candidate = dict(self.server.settings)
                candidate.update(checked_paths)
                image_isolation = self.server.image_isolation
                if "image_save_dir" in data:
                    directory = resolve_image_directory(self.server.data_dir, data["image_save_dir"])
                    check_image_directory(directory)
                    candidate["image_save_dir"] = "" if directory == self.server.default_image_save_dir else str(directory)
                    image_isolation = ImageIsolation(self.server.data_dir, candidate["image_save_dir"])
                if "request_compression" in data:
                    raise ValueError("仅支持接入和断开；接入后由本软件管理 zstd 压缩。")
                for key in ("zstd_level", "image_zstd_level"):
                    if key in data:
                        candidate[key] = validate_zstd_level(data[key])
                if "ui_language" in data:
                    if not isinstance(data["ui_language"], str) or data["ui_language"] not in {"zh", "en"}:
                        raise ValueError("invalid interface language")
                    candidate["ui_language"] = data["ui_language"]
                protection_values = []
                for key in ("packet_size_protection_enabled", "overflow_compaction_enabled", "native_compact_trigger_enabled"):
                    if key in data:
                        if not isinstance(data[key], bool):
                            raise ValueError("invalid boolean setting: " + key)
                        protection_values.append(data[key])
                if len(set(protection_values)) > 1:
                    raise ValueError("请求大小保护开关值冲突")
                if protection_values:
                    candidate["packet_size_protection_enabled"] = protection_values[0]
                if "auto_zstd_level_enabled" in data:
                    if not isinstance(data["auto_zstd_level_enabled"], bool):
                        raise ValueError("invalid boolean setting: auto_zstd_level_enabled")
                    if data["auto_zstd_level_enabled"] and not candidate["packet_size_protection_enabled"]:
                        raise ValueError("请先开启请求大小保护")
                    candidate["auto_zstd_level_enabled"] = data["auto_zstd_level_enabled"]
                if not candidate["packet_size_protection_enabled"]:
                    candidate["auto_zstd_level_enabled"] = False
                for key in ("capture_enabled",
                            "start_on_login", "capacity_retry_enabled"):
                    if key in data:
                        if not isinstance(data[key], bool):
                            raise ValueError("invalid boolean setting: " + key)
                        candidate[key] = data[key]
                if "image_isolation_enabled" in data and not isinstance(data["image_isolation_enabled"], bool):
                    raise ValueError("invalid boolean setting: image_isolation_enabled")
                if "image_analysis_mode" in data:
                    mode = data["image_analysis_mode"]
                    if not isinstance(mode, str) or mode not in IMAGE_MODES:
                        raise ValueError("invalid image analysis mode")
                    if "image_isolation_enabled" in data and data["image_isolation_enabled"] != (mode == "optimized"):
                        raise ValueError("图片解析设置冲突")
                    candidate["image_analysis_mode"] = mode
                elif "image_isolation_enabled" in data:
                    candidate["image_analysis_mode"] = "optimized" if data["image_isolation_enabled"] else "native"
                candidate["image_isolation_enabled"] = candidate["image_analysis_mode"] == "optimized"
                for key, low, high in (("budget_bytes", MIN_BUDGET_BYTES, MAX_BODY), ("max_storage_mb", 16, 8192),
                                       ("max_storage_bytes", MIN_STORAGE_BYTES, MAX_STORAGE_BYTES),
                                       ("max_packets", 10, MAX_PACKETS), ("max_detail_packets", 0, MAX_PACKETS),
                                       ("sharing_port", 1, 65535), ("egress_port", 1, 65535),
                                       ("capacity_retry_max_retries", 1, MAX_RETRIES),
                                       ("capacity_retry_delay_seconds", 1, MAX_DELAY_SECONDS)):
                    if key in data:
                        value = data[key]
                        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                            raise ValueError("设置超出有效范围: " + key)
                        candidate[key] = value
                if {"sharing_port", "egress_port"}.intersection(data) and candidate["sharing_port"] == candidate["egress_port"]:
                    raise ValueError("额度端口与出口端口不能相同。")
                if "max_storage_bytes" in data:
                    legacy = (candidate["max_storage_bytes"] + STORAGE_LEGACY_BYTES - 1) // STORAGE_LEGACY_BYTES
                    if "max_storage_mb" in data and candidate["max_storage_mb"] != legacy:
                        raise ValueError("存储上限设置冲突")
                    candidate["max_storage_mb"] = legacy
                elif "max_storage_mb" in data:
                    candidate["max_storage_bytes"] = candidate["max_storage_mb"] * STORAGE_LEGACY_BYTES
                if "max_detail_packets" in data and candidate["max_detail_packets"] > candidate["max_packets"]:
                    raise ValueError("详情上限不能大于记录上限")
                candidate["max_detail_packets"] = candidate["max_packets"]
                previous = self.server.settings
                change = (self.server.image_config.change(candidate["image_analysis_mode"])
                          if {"image_isolation_enabled", "image_analysis_mode"} & data.keys() else nullcontext())
                with change:
                    self.server.save_settings(candidate)
                    retry_config_applied = False
                    try:
                        retry_keys = ("capacity_retry_enabled", "capacity_retry_max_retries")
                        if any(candidate[key] != previous[key] for key in retry_keys) and self.server.connection_profile()["active"]:
                            self.server.sync_providers()
                            retry_config_applied = True
                        if "start_on_login" in data and (
                                candidate["start_on_login"] != previous["start_on_login"] or not self.server.startup_state_known):
                            with self.server.installation_guard:
                                configure_startup(candidate["start_on_login"], self.server.data_dir,
                                                      self.server.link.config, self.server.port)
                            self.server.startup_state_known = True
                    except Exception:
                        self.server.save_settings(previous)
                        if retry_config_applied:
                            try:
                                self.server.sync_providers()
                            except Exception:
                                pass  # sync_providers retains the configuration error in status.
                        raise
                self.server.image_isolation = image_isolation
                self.server.store.configure_limits(candidate["max_storage_bytes"],
                                                   candidate["max_packets"])
                if candidate["auto_zstd_level_enabled"] != previous["auto_zstd_level_enabled"]:
                    self.server.compression_policy.clear()
            self._json(200, self.server.status())
        elif path == "/api/link" and self.command == "POST":
            data = json.loads(self._body(8192))
            if data.get("action") == "restore":
                result = self.server.set_connection(False)
            elif data.get("action") == "activate":
                result = self.server.set_connection(True)
            else:
                raise ValueError("invalid link action")
            self._json(200, result)
        elif path == "/api/providers/sync" and self.command == "POST":
            self._body(8192)
            result = self.server.sync_providers() if self.server.settings["auto_detect_providers"] else {"enabled": False}
            self._json(200, result)
        elif path == "/api/upgrade/stop" and self.command == "POST":
            self._body(8192)
            with self.server.guard:
                if self.server.active_requests:
                    self._json(409, {"error": "requests_in_flight", "active_requests": self.server.active_requests})
                    return
                self.server.upgrade_draining = True
            self._json(200, {"stopped": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
        elif path == "/api/stop" and self.command == "POST":
            self._body(8192)
            with self.server.guard:
                if self.server.active_requests:
                    self._json(409, {"error": "requests_in_flight"})
                    return
                self.server.upgrade_draining = True
            self._json(200, {"stopped": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
        elif path.startswith("/api/packets/") and self.command == "GET":
            parts = path.split("/")
            packet_id = parts[3]
            action = parts[4] if len(parts) > 4 else ""
            if action == "diff":
                self._json(200, self.server.store.compare(packet_id))
                return
            meta, content = self.server.store.get(packet_id, content=bool(action) and action != "token-window")
            if action == "token-window":
                try:
                    if packet_usage(meta) is not None:
                        self._json(200, self.server.token_windows.lookup_ready(meta))
                        return
                    following = self.server.store.list(limit=1, thread=meta.get("thread_id", ""),
                                                       sort="created", direction="asc", after=packet_id)
                    next_created = following[0].get("created") if following else None
                    self._json(200, self.server.token_windows.lookup_ready(meta, next_created))
                except (OSError, ValueError, TypeError, AttributeError, sqlite3.Error):
                    self._json(200, {"state": "unavailable"})
            elif action == "node":
                if not content:
                    raise ValueError("此请求没有可解析的 JSON 正文")
                self._json(200, describe_node(content["body"], one("path"), content["source_sizes"],
                                             max(0, int(one("offset", "0"))), 100))
            elif action == "text":
                if not content:
                    raise ValueError("此请求没有可解析的 JSON 正文")
                node = resolve(content["body"], one("path"))
                offset = max(0, int(one("offset", "0")))
                text = node if isinstance(node, str) else json.dumps(node, ensure_ascii=False, indent=2)
                self._json(200, {"text": text[offset:offset + 32768], "offset": offset,
                                 "next_offset": offset + 32768 if len(text) > offset + 32768 else None,
                                 "total_chars": len(text)})
            elif action == "search":
                self._json(200, search_body(content["body"], one("q")[:200]) if content else [])
            elif action == "export":
                exported = {"metadata": meta, "view": content,
                            "notice": "Sanitized viewing copy, not raw replay input. Exact sizes were measured before redaction."}
                self._bytes(200, encode(exported), "application/json; charset=utf-8",
                            {"Content-Disposition": 'attachment; filename="codex-packet-' + packet_id + '.json"'})
            elif not action:
                if one("view") == "trend":
                    self._json(200, trend_packet_metadata(meta))
                    return
                diagnostic = meta.get("capacity_retry_diagnostic")
                if isinstance(diagnostic, dict):
                    diagnostic["journal_available"] = self.server.retry_journal.available(diagnostic.get("id", ""))
                self._json(200, meta)
            else:
                self._json(404, {"error": "unknown operation"})
        else:
            self._json(404, {"error": "not_found"})

    def _relay(self, prefix):
        with self.server.guard:
            if self.server.upgrade_draining:
                self.close_connection = True
                self._json(503, {"error": "service_upgrading"})
                return
            self.server.active_requests += 1
        try:
            with self.server.overflow.request_scope() as scope:
                self._relay_request(prefix, scope)
        finally:
            with self.server.guard:
                self.server.active_requests -= 1

    def _checkpoint(self, upstream, suffix, headers, body, encoding, *, packet_id=None, session_id=None, opener=None):
        remove = {"content-encoding", "accept", "accept-encoding", "content-md5", "digest",
                  "x-request-id", "x-codex-turn-metadata"}
        compact_headers = {k: v for k, v in headers.items() if k.lower() not in remove}
        compact_headers.update({"Content-Encoding": encoding, "Accept": "text/event-stream", "Accept-Encoding": "identity"})
        trace = self.server.network_diagnostics.request(upstream + suffix, "POST", compact_headers, body,
            packet_id=packet_id, session_id=session_id, kind="relay_compaction", outgoing_encoding=encoding)
        try:
            with trace.active():
                if opener is not None:
                    return self._checkpoint_request(upstream, suffix, compact_headers, body, trace, opener=opener)
                return self._checkpoint_request(upstream, suffix, compact_headers, body, trace)
        except Exception as exc:
            trace.finish(exc)
            raise
        finally:
            trace.finish()

    def _checkpoint_request(self, upstream, suffix, compact_headers, body, network_trace, *, opener=None):
        # This is a separate, tool-free summary call. The original turn has not
        # been uploaded. Never follow redirects or retry either request.
        local = urlsplit(upstream).hostname in {"localhost", "127.0.0.1", "::1"}
        opener = opener or (self.server.local_opener if local else self.server.egress.outgoing(own_request=True))
        try:
            response = opener.open(Request(upstream + suffix, data=body, headers=compact_headers, method="POST"),
                                   timeout=UPSTREAM_READ_TIMEOUT_SECONDS)
        except HTTPError as exc:
            network_trace.response(exc)
            detail = ""
            try:
                error_bytes = exc.read(128 * 1024)
                network_trace.feed(error_bytes)
                error = json.loads(error_bytes)
                if isinstance(error, dict):
                    error = error.get("error", error)
                if isinstance(error, dict) and isinstance(error.get("message"), str):
                    detail = clean_text(error["message"])[:300]
            except (OSError, TypeError, ValueError):
                pass
            finally:
                exc.close()
            raise OverflowFailure("overflow_compaction_upstream_error", "上游压缩请求返回 HTTP " + str(exc.code)
                                  + ("：" + detail if detail else "") + "；原请求未发送。", 502) from None
        parser = CheckpointStream(on_error=network_trace.stream_error)
        with response:
            network_trace.response(response)
            if response.status != 200:
                raise OverflowFailure("overflow_compaction_upstream_error", "上游没有接受压缩请求；原请求未发送。", 502)
            reader = getattr(response, "read1", response.read)
            while parser.completed is None:
                chunk = reader(65536)
                if not chunk:
                    break
                network_trace.feed(chunk)
                parser.feed(chunk)
        return parser.finish()

    def _relay_request(self, prefix, scope):
        if self.command not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}:
            self._json(405, {"error": "method_not_supported"})
            return
        try:
            body = self._body()
        except (ValueError, OSError):
            self._json(400, {"error": "invalid_request_body"})
            return
        suffix = self.path[len(prefix):]
        if not suffix.startswith("/") or suffix.startswith("//"):
            self._json(400, {"error": "invalid_request_path"})
            return
        shared_client = prefix == urlsplit(self.server.sharing.client_endpoint).path
        remote = None
        if shared_client:
            if not sharing_request_allowed(self.command, suffix):
                if self.command == "GET" and suffix == "/responses":
                    self._json(426, {"error": {"code": "websocket_not_supported", "message": "Use HTTP POST."}})
                else:
                    self._json(404, {"error": "sharing_route_not_found"})
                return
            try:
                remote = self.server.sharing.client_route()
                sharing_upstream = remote["upstream"]
            except SharingError as exc:
                self._json(503, {"error": {"code": "sharing_unavailable", "message": str(exc)}})
                return
        with self.server.guard:
            upstream = self.server.settings["upstream"]
            if suffix.startswith("/p/"):
                parts = suffix.split("/", 3)
                route = self.server.settings["provider_routes"].get(parts[2])
                if len(parts) != 4 or not route:
                    self._json(404, {"error": "unknown_provider_route"})
                    return
                upstream = route["upstream"]
                suffix = "/" + parts[3]
                if suffix.startswith("//"):
                    self._json(400, {"error": "invalid_request_path"})
                    return
        if shared_client:
            upstream = sharing_upstream
        # GET on the Responses creation endpoint is a WebSocket transport probe.
        # Intermediaries may remove every Upgrade header. Return Codex's HTTP
        # fallback status locally instead of forwarding a GET that yields 405
        # and consumes its reconnect retries. GET /responses/{id} still relays.
        if self.command == "GET" and urlsplit(suffix).path.rstrip("/") == "/responses":
            self.close_connection = True
            self._json(426, {"error": {"code": "websocket_not_supported",
                                      "message": "Use HTTP POST for Responses API requests through this relay."}})
            return
        started = time.monotonic()
        with self.server.guard:
            # Disconnected clients may briefly retain an old relay URL until
            # ChatGPT restarts. Keep forwarding them without extra zstd work.
            compression = "zstd" if self.server.settings["auto_detect_providers"] or shared_client else "off"
            json_image = is_json_image_request(suffix, self.headers)
            zstd_level = self.server.settings["image_zstd_level" if json_image else "zstd_level"]
            protection_enabled = self.server.settings["packet_size_protection_enabled"]
            budget = self.server.settings["budget_bytes"]
            image_enabled = self.server.settings["image_isolation_enabled"]
            image_isolation = self.server.image_isolation
            image_configured = self.server.image_config.state.get("enabled", False)
            image_worker = self.server.image_config.is_worker(self.headers.get(WORKER_HEADER, ""))
            auto_zstd_level = (protection_enabled and self.server.settings["auto_zstd_level_enabled"]
                               and compression == "zstd" and not image_worker)
            auto_eligible = (auto_zstd_level and self.command == "POST"
                             and not json_image and urlsplit(suffix).path.rstrip("/").endswith("/responses")
                             and self.headers.get("Content-Encoding", "identity").strip().lower() in {"", "identity", "zstd"})
            auto_ticket = self.server.compression_policy.reserve() if auto_eligible and not json_image else None
        if (image_enabled and not image_worker and self.command == "POST"
                and urlsplit(suffix).path.rstrip("/").endswith(("/responses", "/responses/compact"))
                and is_analysis_request(body, self.headers)):
            image_worker = True
            auto_zstd_level, auto_ticket = False, None
        packet_id = None
        capture_meta = {}
        if self.server.settings["capture_enabled"]:
            try:
                meta, content = inspect_request(body, dict(self.headers.items()), suffix)
                capture_meta = meta
                meta.update(method=self.command, path=safe_path(suffix), source="live",
                            capture_point="app_exit_before_downstream")
                packet_id = self.server.store.add(meta, content)
            except Exception:
                self.server.capture_errors += 1
        def update(**fields):
            if packet_id:
                try:
                    self.server.store.update(packet_id, **fields)
                except Exception:
                    self.server.capture_errors += 1
        connection_tokens = {s.strip().lower() for s in self.headers.get("Connection", "").split(",")}
        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in HOP | connection_tokens | {"host", "content-length", "x-codex-inspector-packet-id", WORKER_HEADER.lower(), EGRESS_HEADER.lower(), CLIENT_HEADER.lower()}}
        if shared_client:
            headers = forwarding_headers(headers)
            headers["Authorization"] = "Bearer " + remote["token"]
            headers[CLIENT_HEADER] = self.server.sharing.vault.client_id
            try:
                exit_link = self.server.egress.request_exit(remote)
                if not exit_link:
                    raise SharingError("本机出口尚未就绪。")
            except (OSError, SharingError):
                update(state="relay_error", relay_error="shared_exit_unavailable", status=503)
                self._json(503, {"error": {"code": "shared_exit_unavailable", "message": "所选出口未就绪，请检查共享出口设置。"}})
                return
            headers[EGRESS_HEADER] = exit_link
        local_upstream = urlsplit(upstream).hostname in {"localhost", "127.0.0.1", "::1"}
        request_opener = (remote["opener"] if shared_client else self.server.local_opener
                          if local_upstream else self.server.egress.outgoing(own_request=True))
        # urllib preserves content bytes, does not decompress responses, and
        # uses the OS/environment proxy. Redirects and inference retries are disabled.
        trigger_size = None
        applied_zstd_level = None
        compression_scope = hashlib.sha256(upstream.encode()).hexdigest()
        auto_key, auto_decision = None, None
        try:
            if auto_eligible:
                if capture_meta.get("inspection") != "json":
                    try:
                        source_encoding = self.headers.get("Content-Encoding", "identity")
                        decoded = decode_body(body, source_encoding)
                        value = json.loads(decoded)
                        capture_meta = {**classify_request(value, headers, suffix),
                                        **request_session_context(value, headers),
                                        "thread_id": thread_identity(headers, value), "decoded_bytes": len(decoded)}
                        kind = temporary_request_kind(value)
                        if kind:
                            capture_meta["request_kind"] = kind
                    except (ValueError, TypeError):
                        capture_meta = {}
                zstd_level = 1
                thread = capture_meta.get("thread_id")
                original_bytes = capture_meta.get("decoded_bytes")
                if type(original_bytes) is int:
                    auto_key = (compression_scope, thread) if thread else None
                    if auto_key and is_main_request(capture_meta):
                        auto_decision = self.server.compression_policy.begin(
                            auto_key, auto_ticket, original_bytes, budget)
                        zstd_level = auto_decision.level
                        update(relay_auto_zstd_phase=auto_decision.phase,
                               relay_auto_zstd_previous_phase=auto_decision.previous_phase,
                               relay_auto_zstd_ticket=auto_decision.ticket,
                               relay_compression_scope=compression_scope,
                               relay_auto_zstd_initial_threshold_bytes=INITIAL_HIGH_BYTES,
                               relay_auto_zstd_budget_bytes=budget)
                    else:
                        zstd_level = self.server.compression_policy.level(auto_key, original_bytes, budget)
            if image_enabled and not image_configured:
                raise ImageIsolationFailure("image_configuration_missing", "图片隔离配置不完整，请在设置中重新启用。", 503)
            prepared = None
            source_body = body
            if image_enabled and not image_worker and self.command == "POST" and urlsplit(suffix).path.rstrip("/").endswith(("/responses", "/responses/compact")):
                try:
                    prepared = image_isolation.prepare(body, headers, suffix, compression, zstd_level=zstd_level,
                                                        auto_zstd_level=auto_zstd_level, budget_bytes=budget)
                except (ImageIsolationFailure, ImportError):
                    raise
                except Exception:
                    raise ImageIsolationFailure("image_preparation_failed", "图片隔离准备失败，原请求未发送。", 503) from None
            if prepared is not None:
                outbound_body, outbound_encoding, image_meta = prepared
                update(**image_meta)
                applied_zstd_level = image_meta.get("relay_zstd_level")
                source_body = outbound_body
                headers = {k: v for k, v in headers.items() if k.lower() not in {"content-md5", "digest", "content-encoding"}}
                headers["Content-Encoding"] = outbound_encoding
            else:
                encoded = prepare_request_info(body, headers, self.command, suffix, compression, zstd_level,
                                                auto_zstd_level=auto_zstd_level, budget_bytes=budget)
                outbound_body, outbound_encoding, applied_zstd_level = encoded
            if protection_enabled and json_image and self.command == "POST" and compression == "zstd":
                prepared_image, upload_meta = optimize_image_request(body, headers, suffix,
                    PreparedRequest(outbound_body, outbound_encoding, applied_zstd_level), budget_bytes=budget,
                    zstd_level=AUTO_IMAGE_ZSTD_LEVEL if auto_zstd_level else zstd_level)
                outbound_body, outbound_encoding, applied_zstd_level = prepared_image
                update(**upload_meta)
                if upload_meta.get("image_upload_optimized"):
                    headers = {key:value for key,value in headers.items() if key.lower() not in {"content-md5", "digest"}}
            # The displayed compressed packet is measured before summary
            # substitution. Preserve its encoding/level before overflow can
            # rebuild a small identity body and replace the final relay fields.
            update(transport_content_encoding=outbound_encoding, transport_zstd_level=applied_zstd_level)
            if auto_decision is not None:
                next_phase = self.server.compression_policy.finish(auto_key, auto_decision, len(outbound_body), budget)
                update(relay_auto_zstd_next_phase=next_phase)
            if protection_enabled and not image_worker:
                trigger_size = usage_trigger_size(body, dict(self.headers.items()), self.command, suffix, len(outbound_body), budget)
                if trigger_size is not None:
                    update(usage_override_request_bytes=trigger_size, usage_override_trigger_bytes=trigger_threshold(budget))
            if image_worker:
                update(image_isolation_worker=True)
            if protection_enabled and not image_worker and self.command == "POST" and urlsplit(suffix).path.rstrip("/").endswith("/responses"):
                outbound_body, outbound_encoding, overflow_meta = self.server.overflow.prepare(
                    outbound_body, outbound_encoding, source_body, headers, suffix, upstream, compression, budget,
                    lambda compact_body, encoding: self._checkpoint(upstream, suffix, headers, compact_body, encoding,
                        packet_id=packet_id, session_id=capture_meta.get("thread_id"),
                        opener=request_opener),
                    request_scope=scope, zstd_level=zstd_level, auto_zstd_level=auto_zstd_level)
                applied_zstd_level = overflow_meta.get("relay_zstd_level", applied_zstd_level)
                update(**overflow_meta)
        except ImageIsolationFailure as exc:
            self.server.network_diagnostics.local_error(upstream + suffix, self.command, headers, body, exc, exc.status, packet_id)
            update(state="relay_error", relay_error=exc.code, status=exc.status)
            self._json(exc.status, {"error": {"code": exc.code, "message": exc.message}})
            return
        except ImportError as exc:
            self.server.network_diagnostics.local_error(upstream + suffix, self.command, headers, body, exc, 503, packet_id)
            update(state="relay_error", relay_error="zstd_unavailable", status=503)
            self._json(503, {"error": {"code": "zstd_unavailable", "message": "缺少 zstd 依赖，请重新安装完整程序或关闭传输压缩。"}})
            return
        except OverflowFailure as exc:
            self.server.network_diagnostics.local_error(upstream + suffix, self.command, headers, body, exc, exc.status, packet_id)
            update(state="relay_error", relay_error=exc.code, status=exc.status,
                   overflow_original_wire_bytes=len(outbound_body),
                   duration_ms=round((time.monotonic() - started) * 1000, 1))
            self._json(exc.status, {"error": {"code": exc.code, "message": exc.message}})
            return
        except Exception as exc:
            update(state="relay_error", relay_error="overflow_" + type(exc).__name__, status=503)
            self.server.network_diagnostics.local_error(upstream + suffix, self.command, headers, body, exc, 503, packet_id)
            self._json(503, {"error": {"code": "overflow_preparation_failed", "message": "本地上传包准备失败；原请求未发送。"}})
            return
        if outbound_body is not body:
            headers = {k: v for k, v in headers.items() if k.lower() != "content-encoding"}
            headers["Content-Encoding"] = outbound_encoding
        update(relay_wire_bytes=len(outbound_body), relay_content_encoding=outbound_encoding,
               compression_applied=outbound_body is not body,
               relay_zstd_level=applied_zstd_level, relay_auto_zstd_level=auto_zstd_level,
               relay_compression_scope=compression_scope)
        update(native_retry_mode="native")
        retry_settings = self.server.settings
        capacity_retry = (retry_settings["capacity_retry_enabled"] and self.command == "POST"
                          and urlsplit(suffix).path.rstrip("/").endswith("/responses"))
        def current_retry_policy():
            settings = self.server.settings
            return settings["capacity_retry_enabled"], settings["capacity_retry_delay_seconds"]
        native_policy = current_retry_policy if capacity_retry else None
        response_path = urlsplit(suffix).path.rstrip("/")
        diagnostic = None
        if self.command == "POST" and (response_path.endswith("/responses") or response_path.endswith("/responses/compact")):
            try:
                diagnostic = self.server.retry_journal.begin(
                    packet_id=packet_id, session_id=capture_meta.get("thread_id") or thread_identity(dict(self.headers), {}),
                    endpoint="compact" if response_path.endswith("/compact") else "responses",
                    enabled=retry_settings["capacity_retry_enabled"], eligible=response_path.endswith("/responses"),
                    max_retries=retry_settings["capacity_retry_max_retries"],
                    on_summary=lambda summary: update(capacity_retry_count=summary["retry_count"],
                                                       capacity_retry_diagnostic=summary))
            except Exception as exc:
                self.server.retry_journal._error(exc)
        if capacity_retry or trigger_size is not None:
            headers = {key: value for key, value in headers.items() if key.lower() != "accept-encoding"}
            headers["Accept-Encoding"] = "identity"
        request = Request(upstream + suffix,
                          data=outbound_body if body or self.command in {"POST", "PUT", "PATCH"} else None,
                          headers=headers, method=self.command)
        network_trace = self.server.network_diagnostics.request(upstream + suffix, self.command, headers, outbound_body,
            packet_id=packet_id, session_id=capture_meta.get("thread_id"),
            kind="compact" if response_path.endswith("/compact") else "responses",
            ingress_bytes=len(body), ingress_encoding=self.headers.get("Content-Encoding", "identity"),
            outgoing_encoding=outbound_encoding, zstd_level=applied_zstd_level,
            preparation_ms=round((time.monotonic() - started) * 1000, 1))
        sent_headers = False
        received = 0
        observation = None
        response_summary = None
        network_error = None
        relay_state, relay_error, diagnostic_error = "relay_error", "", ""
        relay_phase = "upstream_open"
        try:
            try:
                # A separate local upstream can transform the body again, so its
                # final upload is unknown. Direct uploads use these exact bytes.
                if not local_upstream:
                    update(outbound_wire_bytes=len(outbound_body),
                           outbound_content_encoding=outbound_encoding)
                opener = request_opener
                notify(diagnostic.record if diagnostic else None, "attempt_started", attempt=1)
                with network_trace.active():
                    response = opener.open(request, timeout=UPSTREAM_READ_TIMEOUT_SECONDS)
            except HTTPError as exc:
                response = exc
            if shared_client:
                response = prepare_shared_error(response)
            network_trace.response(response)
            notify(diagnostic.record if diagnostic else None, "response_headers", status=response.status)
            response = prepare_http_error(response, native_policy, update, diagnostic.record if diagnostic else None)
            with response:
                status = response.status
                if diagnostic:
                    try:
                        observation = ResponseObservation(diagnostic, response.headers, on_error=network_trace.stream_error)
                    except Exception as exc:
                        self.server.retry_journal._error(exc)
                observe_usage = self.command == "POST" and urlsplit(suffix).path.rstrip("/").endswith("/responses")
                if packet_id:
                    response_summary = ResponseSummary(response)
                usage_stream = NativeRetryStream(response, update, rewrite_usage=trigger_size is not None,
                                                policy=native_policy, on_event=diagnostic.record if diagnostic else None) if observe_usage else None
                rewrite_response = bool(getattr(response, "changed", False) or (
                    usage_stream is not None and usage_stream.enabled and usage_stream.rewrite_response))
                relay_phase = "downstream_headers"
                update(status=status, state="streaming", response_type=response.headers.get("Content-Type", ""),
                       headers_ms=round((time.monotonic() - started) * 1000, 1))
                self.send_response(status, response.reason)
                response_tokens = {s.strip().lower() for s in response.headers.get("Connection", "").split(",")}
                changed_headers = {"content-md5", "digest", "etag"} if rewrite_response else set()
                for key, value in response.headers.items():
                    if key.lower() not in HOP | response_tokens | {"content-length"} | changed_headers:
                        self.send_header(key, value)
                if response.headers.get("Content-Length") is not None and not rewrite_response:
                    self.send_header("Content-Length", response.headers["Content-Length"])
                self.send_header("Connection", "close")
                self.end_headers()
                sent_headers = True
                notify(diagnostic.record if diagnostic else None, "downstream_headers_sent", status=status)
                if self.command != "HEAD":
                    relay_phase = "upstream_read"
                    for chunk in usage_stream.chunks() if usage_stream is not None else raw_chunks(response):
                        network_trace.feed(chunk)
                        if observation:
                            observation.feed(chunk)
                        if response_summary:
                            response_summary.feed(chunk)
                        relay_phase = "downstream_write"
                        self.wfile.write(chunk)
                        self.wfile.flush()
                        network_trace.forwarded_bytes += len(chunk)
                        if not received:
                            notify(diagnostic.record if diagnostic else None, "downstream_first_bytes_sent")
                        received += len(chunk)
                        relay_phase = "upstream_read"
                relay_state = "complete"
        except (BrokenPipeError, ConnectionResetError) as exc:
            network_error = exc
            relay_state = "client_disconnected"
            diagnostic_error = type(exc).__name__
        except Exception as exc:
            network_error = exc
            relay_error = diagnostic_error = type(exc).__name__
            if not sent_headers:
                self._json(502, {"error": {"code": "inspector_upstream_unreachable",
                            "message": "上游连接失败: " + type(exc).__name__}})
        finally:
            try:
                if observation:
                    observation.finish()
            except Exception as exc:
                self.server.retry_journal._error(exc)
            try:
                if diagnostic:
                    diagnostic.finish(relay_state, error_type=diagnostic_error,
                                      error_phase=relay_phase if diagnostic_error else "",
                                      sent_headers=sent_headers, response_bytes=received)
            except Exception as exc:
                self.server.retry_journal._error(exc)
            network_trace.record.update(downstream_headers_sent=sent_headers, relay_state=relay_state)
            network_trace.finish(network_error, phase=relay_phase)
            fields = {"relay_error": relay_error} if relay_error else {}
            if response_summary:
                fields["response_summary"] = response_summary.finish(relay_state)
            update(state=relay_state, response_bytes=received,
                   duration_ms=round((time.monotonic() - started) * 1000, 1), **fields)
            self.close_connection = True

    do_GET = _handle
    do_POST = _handle
    do_PUT = _handle
    do_PATCH = _handle
    do_DELETE = _handle
    do_HEAD = _handle
    do_OPTIONS = _handle


def default_data_dir():
    # A per-user directory outside AppData also works when Windows starts the
    # observer outside an MSIX app's virtualized filesystem.
    return Path.home() / ".codex-packet-inspector"


def argument_parser():
    parser = ConfiguredPortParser(description="Local Codex packet inspector")
    parser.add_argument("--port", type=int, default=None, help="override the configured entry port")
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    parser.add_argument("--config", type=Path, default=Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "config.toml")
    parser.add_argument("--upstream")
    parser.add_argument("--worker-id", default="", help=argparse.SUPPRESS)
    parser.add_argument("--public-port", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--open", action="store_true")
    return parser


def main(argv=None, on_started=None):
    args = argument_parser().parse_args(argv)
    worker_token = ""
    if args.worker_id:
        if not re.fullmatch(r"[0-9a-f]{32}", args.worker_id):
            raise ValueError("invalid worker identity")
        control = json.loads((args.data_dir / "router-workers" / (args.worker_id + ".json")).read_text(encoding="utf8"))
        worker_token = control["token"]
    try:
        server = InspectorServer(("127.0.0.1", args.port), args.data_dir, args.config, args.upstream,
                                 worker_id=args.worker_id, public_port=args.public_port, worker_token=worker_token)
    except OSError as exc:
        if args.worker_id:
            raise RuntimeError("worker port is occupied") from None
        # Reopening the app reuses its own local service.
        settings_file = args.data_dir / "settings.json"
        if settings_file.exists():
            from urllib.request import urlopen
            try:
                url = f"http://127.0.0.1:{args.port}"
                health = json.load(urlopen(url + "/health", timeout=2))
                req = Request(url + "/api/status")
                status = json.load(urlopen(req, timeout=2))
                if health.get("app") == "codex-packet-inspector":
                    if (Path(status["data_dir"]).resolve() != args.data_dir.resolve()
                            or Path(status["profile"]["config_path"]).resolve() != args.config.resolve()):
                        raise ValueError("existing service uses a different profile")
                    if status.get("provider_routing_supported") and status.get("auto_detect_providers"):
                        sync = Request(url + "/api/providers/sync", data=b"{}", headers={"Content-Type": "application/json"})
                        json.load(urlopen(sync, timeout=10))
                    if args.open:
                        webbrowser.open(url + "/")
                    return 0
            except Exception:
                pass
        raise RuntimeError("本地端口已被占用或程序无法启动: " + type(exc).__name__) from None
    runtime = {"pid": os.getpid(), "port": server.port, "version": VERSION}
    runtime_path = (args.data_dir / "router-workers" / (args.worker_id + ".runtime.json")
                    if args.worker_id else args.data_dir / "runtime.json")
    runtime_path.write_text(json.dumps(runtime), encoding="utf8")
    try:
        if args.worker_id:
            from .presentation import WorkerPresentation
            server.presentation = WorkerPresentation(server, enabled=os.name == "nt" and control.get("desktop_tray") is True).start()
        if not args.worker_id and server.settings["auto_detect_providers"]:
            try:
                server.auto_sync_providers()
            except (OSError, ValueError, TypeError):
                pass  # Keep already-published routes serving; expose the error in status.
        if on_started:
            on_started(server)
        if args.open:
            threading.Timer(0.4, lambda: webbrowser.open(server.ui_url)).start()
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
