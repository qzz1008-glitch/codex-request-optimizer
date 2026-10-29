"""Repair only local-address proxy exclusions, at an explicit connect action."""
import ctypes
from ctypes import wintypes
import os
import threading


WINDOWS_LOCAL = ("127.0.0.1", "localhost", "[::1]")
ENV_LOCAL = (*WINDOWS_LOCAL, "::1")
INTERNET_SETTINGS = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
MACHINE_ENVIRONMENT = r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"
PROXY_POLICY = r"Software\Policies\Microsoft\Windows\CurrentVersion\Internet Settings"
GUARD = threading.Lock()


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
        env_names = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")
        process_proxy = any(os.environ.get(name) for name in env_names)
        registered_proxy = False
        for root, path in ((reg.HKEY_CURRENT_USER, "Environment"), (reg.HKEY_LOCAL_MACHINE, MACHINE_ENVIRONMENT)):
            registered_proxy |= any(bool((self._registry_value(root, path, name) or ("",))[0]) for name in env_names)
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


def repair_loopback_proxy(backend=None):
    if backend is None and os.name != "nt":
        return {"state": "not_applicable", "changed": False}
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
            if not (manual or pac or before["environment_proxy"]):
                report["state"] = "not_needed"
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
            report["notifications"] = backend.notify(proxy_changed, environment_changed) if report["changed"] else {}
            report["state"] = "repaired" if report["changed"] else "ok"
            return report
        except Exception as exc:
            report["state"] = "failed"
            raise ProxyRepairError("无法检查或修复本机代理例外，未完成接入。请查看网络诊断日志。", report) from exc
