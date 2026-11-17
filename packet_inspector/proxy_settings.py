"""Shared proxy selection and private manual settings for Windows and Linux."""
from base64 import b64decode, b64encode
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
from urllib.parse import quote, unquote, urlsplit, urlunsplit
from urllib.response import addinfourl

from .sharing_credentials import protect

SETTINGS_KEY = "outbound-proxy-settings-v1"
MAX_PROXY_SETTINGS_BYTES = 64 * 1024
DEFAULT_BYPASS = "localhost,127.0.0.1,::1"
EMPTY_MANUAL = {"address": "", "username": "", "password": "", "bypass": DEFAULT_BYPASS}


def system_proxies():
    values = urllib_request.getproxies()
    # NO_PROXY by itself must not hide Windows' registered system proxy.
    if not (set(values) - {"no"}):
        registry = getattr(urllib_request, "getproxies_registry", lambda: {})()
        values = {**registry, **values}
    if os.name == "nt" and not urllib_request.getproxies_environment() and (values.get("http") or values.get("https")):
        # getproxies_registry omits ProxyOverride; snapshot it for requests
        # instead of consulting unrelated process settings later in flight.
        values = {**values, "no": _windows_bypass()}
    return values


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
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("代理账号和密码请填写在认证字段中。")
        hostname = parsed.hostname
        if ":" in hostname:
            ipaddress.IPv6Address(hostname)
            hostname = "[" + hostname + "]"
        else:
            hostname = hostname.encode("idna").decode("ascii")
            if any(char in hostname for char in "\\/@#?%"):
                raise ValueError()
        return urlunsplit((parsed.scheme, hostname + (":" + str(port) if port else ""), "", "", ""))
    except (ValueError, UnicodeError) as exc:
        if str(exc) == "代理账号和密码请填写在认证字段中。":
            raise ValueError(str(exc)) from None
        raise ValueError("代理地址无效，仅支持 HTTP 或 HTTPS 代理。") from None


def proxy_details(value):
    """Return editable addresses and account names, never a password."""
    try:
        parsed = urlsplit(value if "://" in value else "http://" + value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.port == 0:
            raise ValueError()
        host = parsed.hostname or ""
        host = "[" + host + "]" if ":" in host else host
        return {"address": urlunsplit((parsed.scheme, host + (":" + str(parsed.port) if parsed.port else ""), "", "", "")),
                "username": unquote(parsed.username or ""), "password_saved": parsed.password is not None}
    except (ValueError, TypeError):
        return {"address": "", "username": "", "password_saved": False, "invalid": True}


def bypass_proxy(host, proxies):
    """Use the selected snapshot's exclusions instead of unrelated NO_PROXY."""
    try:
        parsed = urlsplit("//" + host)
        hostname = (parsed.hostname or host).lower().rstrip(".")
    except ValueError:
        hostname = host.lower()
    if hostname in {"localhost", "127.0.0.1", "::1"}:
        return True
    entries = re.split(r"[,;]", proxies.get("no", ""))
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
        super().__init__(proxies)
        self.context = context

    def proxy_open(self, req, proxy, type):
        destination = urlsplit(req.full_url)
        destination_host = req.host + (":" + str(443 if destination.scheme == "https" else 80)
                                       if req.host and destination.port is None else "")
        if destination_host and bypass_proxy(destination_host, self.proxies):
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
        self.windows = os.name == "nt"
        self.cipher = None

    def _cipher(self):
        if self.cipher is None:
            from cryptography.fernet import Fernet
            path = self.vault.path.with_name("proxy-settings.key")
            # The vault transaction also serializes key creation across workers.
            with self.vault.connection():
                if path.exists():
                    key = path.read_bytes()
                else:
                    key = Fernet.generate_key()
                    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as file:
                        file.write(key)
                        file.flush()
                        os.fsync(file.fileno())
                path.chmod(0o600)
                self.cipher = Fernet(key)
        return self.cipher

    def _protect(self, value, decrypt=False):
        if self.windows:
            return protect(value, decrypt)
        cipher = self._cipher()
        return cipher.decrypt(value) if decrypt else cipher.encrypt(value)

    def _decode(self, row):
        if row is None:
            return {"mode": "auto", "manual": dict(EMPTY_MANUAL)}
        try:
            value = json.loads(row[0])
            if (value["mode"] not in {"auto", "manual"} or not isinstance(value["manual"], dict)
                    or any(not isinstance(value["manual"].get(k), str) for k in EMPTY_MANUAL)):
                raise ValueError()
            password = value["manual"]["password"]
            value["manual"]["password"] = self._protect(b64decode(password, validate=True), True).decode() if password else ""
            return value
        except Exception:
            raise ValueError("无法读取已保存的代理配置，请重新保存代理设置。") from None

    def read(self):
        with closing(sqlite3.connect(self.vault.path, timeout=2)) as db:
            row = db.execute("SELECT value FROM meta WHERE key=?", (SETTINGS_KEY,)).fetchone()
        return self._decode(row)

    @staticmethod
    def _candidate(data, saved):
        if not isinstance(data, dict) or set(data) - {"mode", "address", "username", "password", "bypass"}:
            raise ValueError("代理设置无效。")
        if data.get("mode") not in {"auto", "manual"}:
            raise ValueError("请选择自动检测或手动配置。")
        manual = dict(saved["manual"])
        if data["mode"] == "manual":
            manual["address"] = proxy_address(data.get("address", manual["address"]))
            for key, limit in (("username", 512), ("bypass", 4096)):
                incoming = data.get(key, manual[key])
                if not isinstance(incoming, str) or len(incoming) > limit or any(ord(c) < 32 or ord(c) == 127 for c in incoming):
                    raise ValueError("代理账号或例外列表无效。")
                manual[key] = incoming.strip() if key == "bypass" else incoming
            password = data.get("password")
            if password is not None:
                if not isinstance(password, str) or len(password) > 4096 or any(ord(c) < 32 or ord(c) == 127 for c in password):
                    raise ValueError("代理密码无效。")
                manual["password"] = password
            if not manual["username"]:
                manual["password"] = ""
        return {"mode": data["mode"], "manual": manual}

    def configure(self, data):
        # Resolve the Linux key before acquiring the configuration write lock.
        if not self.windows:
            self._cipher()
        with self.vault.connection() as db:
            value = self._decode(db.execute("SELECT value FROM meta WHERE key=?", (SETTINGS_KEY,)).fetchone())
            document = self._candidate(data, value)
            password = document["manual"]["password"]
            document["manual"]["password"] = b64encode(self._protect(password.encode())).decode("ascii") if password else ""
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
        parsed = urlsplit(manual["address"])
        authentication = (quote(manual["username"], safe="") + ":" + quote(manual["password"], safe="") + "@"
                          if manual["username"] else "")
        address = urlunsplit((parsed.scheme, authentication + parsed.netloc, "", "", ""))
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
        source = ("environment" if environment.get("http") or environment.get("https") else "windows_registry"
                  if os.name == "nt" and (detected.get("http") or detected.get("https")) else "direct")
        system = self.system_details()
        selected, mode = self._selection(saved, detected)
        manual = saved["manual"]
        return {"mode": mode, "manual": {k: manual[k] for k in ("address", "username", "bypass")},
                "password_saved": bool(manual["password"]),
                "detected": {**self._view(detected), "source": source,
                    "pac_url": system.get("windows_pac_url") or system.get("AutoConfigURL") or "",
                    "auto_discovery": bool(system.get("windows_auto_detect") or system.get("AutoDetect")),
                    "unsupported_all_proxy": bool(detected.get("all"))},
                "effective": self._view(selected)}
