"""Shared proxy selection and manual addresses for Windows and Linux."""
from base64 import b64encode
from contextlib import closing
from fnmatch import fnmatchcase
import ipaddress
import json
import os
import re
import sqlite3
import socket
import ssl
from urllib import request as urllib_request
from urllib.parse import unquote, urlsplit, urlunsplit
from urllib.response import addinfourl

SETTINGS_KEY = "outbound-proxy-settings-v1"
MAX_PROXY_SETTINGS_BYTES = 64 * 1024
DEFAULT_BYPASS = "localhost,127.0.0.1,::1"
EMPTY_MANUAL = {"address": "", "bypass": DEFAULT_BYPASS}
PROXY_SCHEMES = ("http", "https")


def system_proxies(environment=None):
    if environment is None:
        environment = urllib_request.getproxies_environment()
    # urllib also collects switches like NODE_USE_ENV_PROXY by suffix.
    values = {scheme: environment[scheme] for scheme in PROXY_SCHEMES if environment.get(scheme)}
    windows = {}
    read_registry = getattr(urllib_request, "getproxies_registry", None)
    if read_registry and len(values) < len(PROXY_SCHEMES):
        registry = read_registry()
        windows = {scheme: registry[scheme] for scheme in PROXY_SCHEMES
                   if scheme not in values and registry.get(scheme)}
    if environment.get("no"):
        values["no"] = environment["no"]
    elif windows and os.name == "nt":
        exclusions = _windows_bypass()
        if values:
            values.update({f"no_{scheme}": exclusions for scheme in windows})
        else:
            values["no"] = exclusions
    values.update(windows)
    return values


def proxy_configuration_source(proxies, environment):
    sources = {"environment" if environment.get(scheme) else "windows_registry"
               for scheme in PROXY_SCHEMES if proxies.get(scheme)}
    return "mixed" if len(sources) > 1 else next(iter(sources), "direct")


def _windows_bypass():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as key:
            return str(winreg.QueryValueEx(key, "ProxyOverride")[0])
    except OSError:
        return ""


def proxy_address(value):
    """Validate without including a possibly secret user value in an error."""
    if not isinstance(value, str) or not value.strip() or len(value) > 2048:
        raise ValueError("请填写 HTTP 或 HTTPS 代理地址。")
    value = value.strip()
    if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("代理地址不能包含空格或控制字符。")
    try:
        parsed = urlsplit(value if "://" in value else "http://" + value)
        port = parsed.port
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or port == 0
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
            raise ValueError()
        hostname = parsed.hostname
        if ":" in hostname:
            ipaddress.IPv6Address(hostname)
            hostname = "[" + hostname + "]"
        else:
            hostname = hostname.encode("idna").decode("ascii")
            if any(char in hostname for char in "\\/@#?%"):
                raise ValueError()
    except ValueError:
        raise ValueError("代理地址无效，仅支持 HTTP 或 HTTPS 代理。") from None
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("代理地址不能包含账号或密码。")
    return urlunsplit((parsed.scheme, hostname + (":" + str(port) if port else ""), "", "", ""))


def proxy_details(value):
    """Return the proxy address with any environment credentials stripped."""
    try:
        parsed = urlsplit(value if "://" in value else "http://" + value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.port == 0:
            raise ValueError()
        host = parsed.hostname or ""
        host = "[" + host + "]" if ":" in host else host
        return {"address": urlunsplit((parsed.scheme, host + (":" + str(parsed.port) if parsed.port else ""), "", "", ""))}
    except (ValueError, TypeError):
        return {"address": "", "invalid": True}


def bypass_proxy(host, proxies, scheme=None):
    """Use the selected snapshot's exclusions instead of unrelated NO_PROXY."""
    try:
        parsed = urlsplit("//" + host)
        hostname = (parsed.hostname or host).lower().rstrip(".")
    except ValueError:
        hostname = host.lower()
    if hostname in {"localhost", "127.0.0.1", "::1"}:
        return True
    exclusions = proxies.get(f"no_{scheme}", proxies.get("no", ""))
    entries = re.split(r"[,;]", exclusions)
    for entry in entries:
        entry = entry.strip().lower()
        if not entry:
            continue
        if entry == "<local>" and "." not in hostname and ":" not in hostname:
            return True
        if entry == "*" or fnmatchcase(hostname, entry) or fnmatchcase(host.lower(), entry):
            return True
        if urllib_request.proxy_bypass_environment(host, {"no": entry}):
            return True
    return False


class SelectedProxyHandler(urllib_request.ProxyHandler):
    """ProxyHandler with the chosen configuration's own bypass policy."""
    def __init__(self, proxies, context=None):
        super().__init__({scheme: proxies[scheme] for scheme in PROXY_SCHEMES if proxies.get(scheme)})
        self.proxies = proxies
        self.context = context

    def proxy_open(self, req, proxy, type):
        destination = urlsplit(req.full_url)
        destination_host = req.host + (":" + str(443 if destination.scheme == "https" else 80)
                                       if req.host and destination.port is None else "")
        if destination_host and bypass_proxy(destination_host, self.proxies, destination.scheme):
            return None
        try:
            parsed = urlsplit(proxy if "://" in proxy else "http://" + proxy)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError()
            host = parsed.hostname
            host = "[" + host + "]" if ":" in host else host
            hostport = host + (":" + str(parsed.port) if parsed.port else "")
        except (ValueError, TypeError):
            raise OSError("所选代理地址无效，仅支持 HTTP 或 HTTPS 代理。") from None
        authorization = None
        if parsed.username is not None:
            credential = unquote(parsed.username) + ":" + unquote(parsed.password or "")
            authorization = "Basic " + b64encode(credential.encode()).decode("ascii")
        if parsed.scheme == "https":
            # urllib sends CONNECT in cleartext even for an https:// proxy.
            # Use the existing TLS-in-TLS dependency for this scheme only.
            import urllib3
            manager = urllib3.ProxyManager("https://" + hostport,
                proxy_headers={"Proxy-Authorization": authorization} if authorization else {},
                proxy_ssl_context=self.context or ssl.create_default_context(),
                ssl_context=self.context or ssl.create_default_context(), retries=False)
            try:
                timeout = socket.getdefaulttimeout() if req.timeout is socket._GLOBAL_DEFAULT_TIMEOUT else req.timeout
                response = manager.urlopen(req.get_method(), req.full_url, body=req.data,
                    headers={k: v for k, v in req.header_items() if k.lower() != "proxy-authorization"},
                    redirect=False, retries=False, preload_content=False, decode_content=False,
                    timeout=urllib3.Timeout(connect=timeout, read=timeout))
            except Exception:
                manager.clear()
                raise
            result = _TLSProxyResponse(response, response.headers, req.full_url, response.status)
            result.msg, result.manager = response.reason, manager
            return result
        if authorization:
            req.add_header("Proxy-Authorization", authorization)
        original = req.type
        req.set_proxy(hostport, parsed.scheme)
        if original == parsed.scheme or original == "https":
            return None
        return self.parent.open(req, timeout=req.timeout)


class _TLSProxyResponse(addinfourl):
    def close(self):
        try:
            super().close()
        finally:
            self.manager.clear()


class ProxySettings:
    def __init__(self, vault, auto_proxies=system_proxies, system_details=lambda: {}):
        self.vault, self.auto_proxies, self.system_details = vault, auto_proxies, system_details

    @staticmethod
    def _decode(row):
        if row is None:
            return {"mode": "auto", "manual": dict(EMPTY_MANUAL)}
        try:
            value = json.loads(row[0])
            mode, stored = value["mode"], value["manual"]
            if (mode not in ("auto", "manual") or not isinstance(stored, dict)
                    or any(not isinstance(stored.get(key), str) for key in EMPTY_MANUAL)):
                raise ValueError()
            manual = {key: stored[key] for key in EMPTY_MANUAL}
            if mode == "manual" or manual["address"]:
                manual["address"] = proxy_address(manual["address"])
            return {"mode": mode, "manual": manual}
        except (KeyError, TypeError, ValueError):
            raise ValueError("无法读取已保存的代理配置，请重新保存代理设置。") from None

    def read(self):
        with closing(sqlite3.connect(self.vault.path, timeout=2)) as db:
            row = db.execute("SELECT value FROM meta WHERE key=?", (SETTINGS_KEY,)).fetchone()
        return self._decode(row)

    @staticmethod
    def _candidate(data, saved):
        if not isinstance(data, dict) or set(data) - {"mode", "address", "bypass"}:
            raise ValueError("代理设置无效。")
        mode = data.get("mode")
        if mode not in ("auto", "manual"):
            raise ValueError("请选择自动检测或手动配置。")
        manual = dict(saved["manual"])
        if mode == "manual":
            manual["address"] = proxy_address(data.get("address", manual["address"]))
            bypass = data.get("bypass", manual["bypass"])
            if (not isinstance(bypass, str) or len(bypass) > 4096
                    or any(ord(char) < 32 or ord(char) == 127 for char in bypass)):
                raise ValueError("代理例外列表无效。")
            manual["bypass"] = bypass.strip()
        return {"mode": mode, "manual": manual}

    def configure(self, data):
        with self.vault.connection() as db:
            value = self._decode(db.execute("SELECT value FROM meta WHERE key=?", (SETTINGS_KEY,)).fetchone())
            document = self._candidate(data, value)
            # Older workers still expect both fields when reading the v1 record.
            document["manual"].update(username="", password="")
            db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)",
                       (SETTINGS_KEY, json.dumps(document, ensure_ascii=False).encode()))
        return self.status()

    def preview(self, data):
        candidate = self._candidate(data, self.read())
        return self._selection(candidate, self.auto_proxies() if candidate["mode"] == "auto" else {})

    def selection(self):
        saved = self.read()
        return self._selection(saved, self.auto_proxies() if saved["mode"] == "auto" else {})

    @staticmethod
    def _selection(saved, detected):
        if saved["mode"] == "auto":
            return dict(detected), "auto"
        manual = saved["manual"]
        address = manual["address"]
        return {"http": address, "https": address, "no": manual["bypass"]}, "manual"

    @staticmethod
    def _view(values):
        return {"http": proxy_details(values["http"]) if values.get("http") else None,
                "https": proxy_details(values["https"]) if values.get("https") else None,
                "bypass": values.get("no", "")}

    def status(self):
        saved = self.read()
        detected = dict(self.auto_proxies())
        environment = urllib_request.getproxies_environment()
        source = proxy_configuration_source(detected, environment)
        system = self.system_details()
        selected, mode = self._selection(saved, detected)
        return {"mode": mode, "manual": saved["manual"],
                "detected": {**self._view(detected), "source": source,
                    "pac_url": system.get("windows_pac_url") or system.get("AutoConfigURL") or "",
                    "auto_discovery": bool(system.get("windows_auto_detect") or system.get("AutoDetect")),
                    "unsupported_all_proxy": bool(environment.get("all"))},
                "effective": self._view(selected)}
