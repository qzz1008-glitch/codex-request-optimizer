"""Repair local-address proxy exclusions used by Codex."""
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import re
import tempfile
import threading


WINDOWS_LOCAL = ("127.0.0.1", "localhost", "[::1]")
ENV_LOCAL = (*WINDOWS_LOCAL, "::1")
PROXY_ENV_NAMES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")
INTERNET_SETTINGS = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
MACHINE_ENVIRONMENT = r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"
PROXY_POLICY = r"Software\Policies\Microsoft\Windows\CurrentVersion\Internet Settings"
GUARD = threading.Lock()
# Consume complete assignments, including quoted multiline values, so a line
# inside another setting's value cannot be mistaken for NO_PROXY.
_DOTENV_QUOTED = re.compile(r"""'[^']*'|"(?:\\[^\r\n]|[^"\\])*["]""")
_DOTENV_ASSIGNMENT = re.compile(
    r"^[ \t]*(?:export[ \t]+)?(?P<key>[a-zA-Z_][a-zA-Z0-9_.]*)[ \t]*=[ \t]*"
    rf"""(?P<value>(?:(?!#)(?:{_DOTENV_QUOTED.pattern}|\\[^\r\n]|[^ \t\r\n'"\\])+)?)[ \t]*(?:#[^\r\n]*)?\r?$""",
    re.M,
)


class ProxyRepairError(ValueError):
    def __init__(self, message, report):
        super().__init__(message)
        self.report = report


def append_entries(original, entries, separator):
    """Retain original spelling/order, including wildcard and <local> rules."""
    current = {part.strip().lower() for part in original.split(separator) if part.strip()}
    if "*" in current:
        return original
    result = original
    for entry in entries:
        if entry.lower() not in current:
            result += (separator if result and not result.endswith(separator) else "") + entry
            current.add(entry.lower())
    return result


def _dotenv_change(path, before, proxy_configured, relay_host):
    try:
        original = path.read_bytes()
    except FileNotFoundError:
        return None
    text = original.decode("utf-8-sig", errors="surrogateescape")
    settings = {match["key"].upper(): match for match in _DOTENV_ASSIGNMENT.finditer(text)
                if not any("\udc80" <= char <= "\udcff" for char in match["value"])}
    override = settings.get("NO_PROXY")
    dotenv_proxy = any(name in settings and settings[name]["value"] not in ("", "''", '""')
                       for name in PROXY_ENV_NAMES)
    if override is not None and (proxy_configured or dotenv_proxy):
        start, end = override.span("value")
        if _DOTENV_QUOTED.fullmatch(override["value"]):
            start += 1
            end -= 1
        value = text[start:end]
        replacement = append_entries(value, (relay_host,), ",")
        if replacement == value:
            return None
        if not value and text[end:end + 1] == "#":
            replacement += " "
        edited = text[:start] + replacement + text[end:]
    elif override is None and dotenv_proxy:
        # A fresh desktop launch inherits the saved user value over the system
        # value. A launch from a shell may only have a process value.
        saved = before["user_no_proxy"] or before["machine_no_proxy"]
        value = saved[0] if saved is not None else before["process_no_proxy"] or ""
        if append_entries(value, (relay_host,), ",") == value:
            return None
        # Let Codex expand the actual inherited value on each start. Copying
        # a registry or Optimizer value here would freeze its current rules.
        newline = "\r\n" if "\r\n" in text else "\n"
        prefix = "" if not text or text.endswith("\n") else newline
        edited = text + prefix + 'NO_PROXY="${NO_PROXY},' + relay_host + '"' + newline
    else:
        return None
    encoding = "utf-8-sig" if original.startswith(b"\xef\xbb\xbf") else "utf8"
    return original, edited.encode(encoding, errors="surrogateescape")


def _replace_dotenv(path, original, edited):
    # Resolve an existing link so an atomic replacement does not overwrite it.
    # Use ReplaceFile on Windows to retain the original file's access controls.
    path = path.resolve(strict=True)
    stream = tempfile.NamedTemporaryFile(dir=path.parent, prefix=".env.", suffix=".tmp", delete=False)
    staging = Path(stream.name)
    try:
        with stream:
            stream.write(edited)
            stream.flush()
            os.fsync(stream.fileno())
        if path.read_bytes() != original:
            raise ValueError("Codex .env changed during connection")
        if os.name == "nt":
            replace = ctypes.WinDLL("kernel32", use_last_error=True).ReplaceFileW
            replace.argtypes = [wintypes.LPCWSTR] * 3 + [wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p]
            replace.restype = wintypes.BOOL
            if not replace(str(path), str(staging), None, 0, None, None):
                raise ctypes.WinError(ctypes.get_last_error())
        else:
            staging.replace(path)
        if path.read_bytes() != edited:
            raise ValueError("could not verify Codex .env update")
    finally:
        staging.unlink(missing_ok=True)


class _OptionValue(ctypes.Union):
    _fields_ = [("number", wintypes.DWORD), ("string", ctypes.c_void_p), ("filetime", wintypes.FILETIME)]


class _Option(ctypes.Structure):
    _fields_ = [("option", wintypes.DWORD), ("value", _OptionValue)]


class _OptionList(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("connection", wintypes.LPWSTR),
                ("count", wintypes.DWORD), ("error", wintypes.DWORD), ("options", ctypes.POINTER(_Option))]


class WindowsProxySettings:
    def __init__(self):
        import winreg
        self.registry = winreg
        self.internet = ctypes.WinDLL("wininet", use_last_error=True)
        self.internet.InternetQueryOptionW.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
        self.internet.InternetQueryOptionW.restype = wintypes.BOOL
        self.internet.InternetSetOptionW.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
        self.internet.InternetSetOptionW.restype = wintypes.BOOL
        self.free = ctypes.WinDLL("kernel32").GlobalFree
        self.free.argtypes, self.free.restype = [ctypes.c_void_p], ctypes.c_void_p

    def _registry_value(self, root, path, name):
        try:
            with self.registry.OpenKey(root, path) as key:
                return self.registry.QueryValueEx(key, name)
        except FileNotFoundError:
            return None

    def _native(self):
        options = (_Option * 4)()
        for option, field in zip(options, (1, 2, 3, 4)):  # flags, server, bypass, PAC URL
            option.option = field
        value = _OptionList(ctypes.sizeof(_OptionList), None, len(options), 0, options)
        size = wintypes.DWORD(ctypes.sizeof(value))
        try:
            if not self.internet.InternetQueryOptionW(None, 75, ctypes.byref(value), ctypes.byref(size)):
                raise ctypes.WinError(ctypes.get_last_error())
            return {"flags": int(options[0].value.number),
                    "proxy": ctypes.wstring_at(options[1].value.string) if options[1].value.string else "",
                    "bypass": ctypes.wstring_at(options[2].value.string) if options[2].value.string else "",
                    "pac": bool(ctypes.wstring_at(options[3].value.string)) if options[3].value.string else False}
        finally:
            for option in list(options)[1:]:
                if option.value.string:
                    self.free(option.value.string)

    def read(self):
        reg = self.registry
        native = self._native()
        policy = self._registry_value(reg.HKEY_LOCAL_MACHINE, PROXY_POLICY, "ProxySettingsPerUser")
        machine_policy = bool(policy and policy[0] == 0)
        registry_bypass = self._registry_value(reg.HKEY_LOCAL_MACHINE if machine_policy else reg.HKEY_CURRENT_USER,
                                               INTERNET_SETTINGS, "ProxyOverride")
        user_no_proxy = self._registry_value(reg.HKEY_CURRENT_USER, "Environment", "NO_PROXY")
        machine_no_proxy = self._registry_value(reg.HKEY_LOCAL_MACHINE, MACHINE_ENVIRONMENT, "NO_PROXY")
        for entry in (registry_bypass, user_no_proxy, machine_no_proxy):
            if entry and (entry[1] not in {reg.REG_SZ, reg.REG_EXPAND_SZ} or not isinstance(entry[0], str)):
                raise ValueError("unsupported proxy exclusion value type")
        process_proxy = any(os.environ.get(name) for name in PROXY_ENV_NAMES)
        registered_proxy = False
        for root, path in ((reg.HKEY_CURRENT_USER, "Environment"), (reg.HKEY_LOCAL_MACHINE, MACHINE_ENVIRONMENT)):
            registered_proxy |= any(bool((self._registry_value(root, path, name) or ("",))[0]) for name in PROXY_ENV_NAMES)
        return {"native": native, "registry_bypass": registry_bypass,
                "user_no_proxy": user_no_proxy, "machine_no_proxy": machine_no_proxy,
                "process_no_proxy": os.environ.get("NO_PROXY"), "process_proxy": process_proxy,
                "registered_proxy": registered_proxy, "environment_proxy": process_proxy or registered_proxy,
                "machine_proxy_policy": machine_policy}

    def write_bypass(self, before, value):
        if before["machine_proxy_policy"]:
            raise PermissionError("Windows proxy settings are managed per-machine")
        current = self._native()
        registry = self._registry_value(self.registry.HKEY_CURRENT_USER, INTERNET_SETTINGS, "ProxyOverride")
        if current["bypass"] != before["native"]["bypass"] or registry != before["registry_bypass"]:
            raise ValueError("proxy exclusions changed during connection")
        text = ctypes.create_unicode_buffer(value)
        option = (_Option * 1)()
        option[0].option, option[0].value.string = 3, ctypes.cast(text, ctypes.c_void_p).value
        options = _OptionList(ctypes.sizeof(_OptionList), None, 1, 0, option)
        # Setting only BYPASS preserves server, enable flags, auto-detect and PAC.
        if not self.internet.InternetSetOptionW(None, 75, ctypes.byref(options), ctypes.sizeof(options)):
            raise ctypes.WinError(ctypes.get_last_error())
        if self._native()["bypass"] != value:
            raise ValueError("Windows did not retain proxy exclusions")
        registry = self._registry_value(self.registry.HKEY_CURRENT_USER, INTERNET_SETTINGS, "ProxyOverride")
        if not registry or registry[0] != value:
            raise ValueError("Windows registry proxy exclusions did not match the applied settings")

    def write_user_no_proxy(self, before, value):
        reg = self.registry
        with reg.CreateKeyEx(reg.HKEY_CURRENT_USER, "Environment", 0, reg.KEY_QUERY_VALUE | reg.KEY_SET_VALUE) as key:
            try:
                current = reg.QueryValueEx(key, "NO_PROXY")
            except FileNotFoundError:
                current = None
            if current != before["user_no_proxy"]:
                raise ValueError("NO_PROXY changed during connection")
            kind = current[1] if current else reg.REG_SZ
            reg.SetValueEx(key, "NO_PROXY", 0, kind, value)
            if reg.QueryValueEx(key, "NO_PROXY") != (value, kind):
                raise ValueError("Windows did not retain NO_PROXY")

    def write_process_no_proxy(self, before, value):
        if os.environ.get("NO_PROXY") != before["process_no_proxy"]:
            raise ValueError("process NO_PROXY changed during connection")
        os.environ["NO_PROXY"] = value

    def notify(self, proxy_changed, environment_changed):
        result = {}
        if proxy_changed:
            for flag in (39, 37):  # settings changed, refresh
                result[str(flag)] = bool(self.internet.InternetSetOptionW(None, flag, None, 0))
        if environment_changed:
            # Broadcast can wait per window. Keep it outside the connect request
            # and retain the string buffer until the synchronous call returns.
            def broadcast():
                user = ctypes.WinDLL("user32", use_last_error=True)
                send = user.SendMessageTimeoutW
                send.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPCWSTR,
                                 wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]
                send.restype = ctypes.c_ssize_t
                output = ctypes.c_size_t()
                send(0xFFFF, 0x001A, 0, "Environment", 2, 1000, ctypes.byref(output))
            thread = threading.Thread(target=broadcast, daemon=True, name="proxy-environment-notification")
            thread.start()
            thread.join(.2)
            result["environment"] = "requested"
        return result


def repair_loopback_proxy(backend=None, *, codex_home=None, relay_host=None, codex_only=False):
    if backend is None and os.name != "nt":
        return {"state": "not_applicable", "changed": False}
    dotenv = Path(codex_home) / ".env" if codex_home is not None and relay_host in ENV_LOCAL else None
    if codex_only and (dotenv is None or not dotenv.exists()):
        return {"state": "not_needed", "changed": False}
    report = {"state": "checking", "changed": False, "changes": []}
    with GUARD:
        try:
            backend = backend or WindowsProxySettings()
            before = backend.read()
            native = before["native"]
            manual = bool(native["flags"] & 2 and native["proxy"])
            pac = bool(native["pac"] or native["flags"] & 12)
            report["sources"] = {"windows_manual": manual, "windows_auto_proxy": pac,
                                 "environment": bool(before["environment_proxy"])}
            proxy_configured = bool(manual or pac or before["environment_proxy"])
            edit = _dotenv_change(dotenv, before, proxy_configured, relay_host) if dotenv is not None else None
            if edit is not None:
                report["changes"].append({"setting": "codex_dotenv_NO_PROXY", "address": relay_host, "applied": False})
                _replace_dotenv(dotenv, *edit)
                report["changes"][-1]["applied"] = True
                report["changed"] = True
            if codex_only or not proxy_configured:
                report["state"] = "repaired" if report["changed"] else "not_needed"
                return report
            # An explicit environment NO_PROXY takes precedence over Windows
            # bypass rules. Repair both when that configuration is already used.
            old_windows = native["bypass"]
            registry_windows = (before["registry_bypass"] or ("",))[0]
            # Prefer the saved rules if a WinINet cache still has an older list.
            # Combining both would resurrect exclusions the user removed.
            saved_windows = registry_windows if before["registry_bypass"] is not None else old_windows
            new_windows = append_entries(saved_windows, WINDOWS_LOCAL, ";")
            proxy_changed = new_windows != old_windows or new_windows != registry_windows
            if proxy_changed and (manual or pac):
                if before["machine_proxy_policy"]:
                    raise PermissionError("Windows proxy settings are managed per-machine")
                report["changes"].append({"setting": "ProxyOverride", "before": old_windows,
                                          "registry_before": registry_windows, "after": new_windows, "applied": False})
                backend.write_bypass(before, new_windows)
                report["changes"][-1]["applied"] = True
                report["changed"] = True
            else:
                proxy_changed = False
            user = (before["user_no_proxy"] or ("",))[0]
            machine = (before["machine_no_proxy"] or ("",))[0]
            process = before["process_no_proxy"] or ""
            environment_changed = False
            if before["registered_proxy"] or user or machine:
                # User values override machine values. Never union inactive or
                # stale inherited exclusions back into the user's configuration.
                inherited = user if before["user_no_proxy"] is not None else machine
                value = append_entries(inherited, ENV_LOCAL, ",")
                if value != inherited:
                    report["changes"].append({"setting": "user_NO_PROXY", "before": user, "after": value, "applied": False})
                    backend.write_user_no_proxy(before, value)
                    report["changes"][-1]["applied"] = True
                    report["changed"] = environment_changed = True
            if before["process_proxy"] or process:
                process_value = append_entries(process, ENV_LOCAL, ",")
                if process_value != process:
                    report["changes"].append({"setting": "process_NO_PROXY", "before": process,
                                              "after": process_value, "applied": False})
                    backend.write_process_no_proxy(before, process_value)
                    report["changes"][-1]["applied"] = True
                    report["changed"] = True
            report["notifications"] = backend.notify(proxy_changed, environment_changed) if proxy_changed or environment_changed else {}
            report["state"] = "repaired" if report["changed"] else "ok"
            return report
        except Exception as exc:
            report["state"] = "failed"
            raise ProxyRepairError("无法检查或修复本机代理例外，未完成接入。请查看网络诊断日志。", report) from exc
