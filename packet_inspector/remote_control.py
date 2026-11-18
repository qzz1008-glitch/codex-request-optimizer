"""Fixed, standard-library-only script sent over the user's existing SSH session."""
from .entry_port import DEFAULT_PORT

RESULT_PREFIX = "INSPECTOR_CONNECTION_RESULT:"

SCRIPT = r'''
import hashlib, json, os, pathlib, re, shutil, stat, subprocess
from urllib.parse import urlsplit
from urllib.request import build_opener, ProxyHandler, HTTPRedirectHandler, Request
from urllib.error import HTTPError

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None

def native_codex(home):
    unsupported = {"supported": False, "reason": "远程 Codex 不支持原生后台重载，请在 ChatGPT 中重新连接此服务器。"}
    search = str(pathlib.Path(os.environ.get("CODEX_INSTALL_DIR") or pathlib.Path.home() / ".local/bin")) + os.pathsep + os.environ.get("PATH", "")
    cli = shutil.which("codex", path=search)
    if not cli:
        return unsupported, None
    env = dict(os.environ, CODEX_HOME=str(home))
    try:
        value = native_command(cli, "version", env, 8)
        managed = pathlib.Path(value.get("managedCodexPath", ""))
        socket = pathlib.Path(value.get("socketPath", ""))
        if value.get("status") != "running" or not managed.is_absolute() or not managed.is_file():
            return unsupported, None
        if not socket.is_absolute():
            return unsupported, None
        # Codex can link the control socket to a shorter path outside CODEX_HOME.
        socket_dir = socket.parent.resolve()
        resolved_home = home.resolve()
        if socket_dir != resolved_home and resolved_home not in socket_dir.parents:
            return unsupported, None
        endpoint = socket.stat()
        if not stat.S_ISSOCK(endpoint.st_mode) or endpoint.st_uid != os.getuid():
            return unsupported, None
        return {"supported": True, "reason": ""}, (str(managed), env, str(socket))
    except (OSError, ValueError, subprocess.SubprocessError):
        return unsupported, None

def native_command(cli, action, env, timeout):
    result = subprocess.run([cli, "app-server", "daemon", action], env=env, cwd="/",
                            capture_output=True, timeout=timeout)
    if result.returncode:
        detail = result.stderr[-1500:].decode("utf-8", errors="replace").strip()
        raise ValueError("Codex 原生后台命令失败：" + (detail or str(result.returncode)))
    if len(result.stdout) > 65536:
        raise ValueError("Codex 原生后台返回内容过大，未确认重连结果。")
    value = json.loads(result.stdout)
    if not isinstance(value, dict):
        raise ValueError("Codex 原生后台未返回有效状态。")
    return value

def restart_native(context):
    if context is None:
        raise ValueError("远程 Codex 不支持原生后台重载，请在 ChatGPT 中重新连接此服务器。")
    cli, env, socket = context
    result = native_command(cli, "restart", env, 120)
    if result.get("status") != "restarted" or result.get("socketPath") != socket:
        raise ValueError("Codex 原生后台未确认重载完成，请刷新状态后检查。")
    ready = native_command(cli, "version", env, 8)
    if ready.get("status") != "running" or ready.get("socketPath") != socket:
        raise ValueError("Codex 原生后台尚未就绪，请刷新状态后检查。")

def config_digest(config):
    with config.open("rb") as source:
        value = source.read(2 * 1024 * 1024 + 1)
    if len(value) > 2 * 1024 * 1024:
        raise ValueError("Codex 配置过大，无法确认重连范围。")
    return hashlib.sha256(value).digest()

def control():
    options = json.loads(INPUT)
    home = pathlib.Path(os.environ.get("CODEX_HOME") or pathlib.Path.home() / ".codex").expanduser()
    config = home / "config.toml"
    origins = []
    def add(value):
        try:
            u = urlsplit(value)
            if u.scheme == "http" and u.hostname in {"127.0.0.1", "localhost", "::1"} and u.port and not u.username and not u.password:
                origin = "http://127.0.0.1:" + str(u.port)
                if origin not in origins:
                    origins.append(origin)
        except (TypeError, ValueError):
            pass
    try:
        if config.stat().st_size < 2 * 1024 * 1024:
            text = config.read_text(encoding="utf-8-sig")
            for match in re.finditer(r"(?m)^\s*(?:openai_base_url|base_url)\s*=\s*['\"]([^'\"\r\n]+)", text):
                if "/relay/" in match[1]:
                    add(match[1])
    except OSError:
        pass
    data = pathlib.Path.home() / ".codex-packet-inspector"
    for filename in ("runtime.json", "entry-port.json"):
        try:
            port = json.loads((data / filename).read_text(encoding="utf-8-sig")).get("port")
            if type(port) is int and 1024 <= port <= 65535:
                add("http://127.0.0.1:" + str(port))
        except (OSError, ValueError, AttributeError):
            pass
    add(options.get("origin"))
    add("http://127.0.0.1:" + str(DEFAULT_PORT))
    opener = build_opener(ProxyHandler({}), NoRedirect())
    def request(origin, path, body=None):
        req = Request(origin + path, data=json.dumps(body).encode() if body is not None else None,
                      headers={"Content-Type": "application/json", "Origin": origin})
        with opener.open(req, timeout=60 if body is not None else 2) as response:
            raw = response.read(512 * 1024 + 1)
            if len(raw) > 512 * 1024:
                raise ValueError("response_too_large")
            return json.loads(raw)
    status, selected = None, ""
    for origin in origins[:8]:
        try:
            candidate = request(origin, "/api/status")
            profile = candidate.get("profile", {})
            # A different Codex installation or another local HTTP service is
            # never a valid target, even when its port happens to match.
            if candidate.get("connection_policy") != 1 or not isinstance(profile, dict):
                continue
            if candidate.get("worker_state", "active") != "active":
                continue
            if candidate.get("public_port", urlsplit(origin).port) != urlsplit(origin).port:
                continue
            if pathlib.Path(profile.get("config_path", "")).resolve() != config.resolve():
                continue
            status, selected = candidate, origin
            break
        except Exception:
            continue
    if status is None:
        return {"state": "error", "can_connect": False, "can_disconnect": False,
                "error": "未找到对应的远程中转服务，请确认服务已运行且使用当前 SSH 用户的 Codex 配置。"}
    profile, result = status["profile"], {}
    capability, native_context = native_codex(home)
    action = options.get("action")
    reconnect = None
    if action == "reconnect":
        try:
            digest = config_digest(config)
            notice = status.get("restart_notice", {})
            restart_native(native_context)
            reconnect = {"state": "reloaded", "error": "", "warning": ""}
            status = request(selected, "/api/status")
            if config_digest(config) != digest or status.get("restart_notice", {}).get("revision") != notice.get("revision"):
                reconnect["warning"] = "Codex 已重载，但配置又有变化，请再次重新连接。"
            elif not notice.get("error") and type(notice.get("revision")) is int and any(
                    item.get("target") == "chatgpt" for item in notice.get("items", []) if isinstance(item, dict)):
                try:
                    request(selected, "/api/restart-notice", {"target": "chatgpt", "revision": notice["revision"]})
                except Exception:
                    reconnect["warning"] = "Codex 已重载，待生效提醒尚未确认，请刷新状态后检查。"
            status = request(selected, "/api/status")
            profile = status["profile"]
        except subprocess.TimeoutExpired:
            reconnect = {"state": "unconfirmed", "error": "重连结果未确认，请刷新状态后检查；待生效提醒已保留。"}
        except Exception as exc:
            reconnect = {"state": "unconfirmed", "error": str(exc)[:1500]}
    elif action in {"activate", "restore"}:
        try:
            result = request(selected, "/api/link", {"action": action})
        except HTTPError as exc:
            try:
                message = json.loads(exc.read(4096)).get("error")
            except Exception:
                message = None
            finally:
                exc.close()
            return {"state": "error", "error": str(message or ("远程接入请求返回 HTTP " + str(exc.code)))[:1000], "origin": selected}
        status = request(selected, "/api/status")
        profile = status["profile"]
    error = profile.get("config_error") or status.get("provider_sync_error") or ""
    warnings = result.get("warnings", [])
    value = {"state": "error" if error else "connected" if profile.get("active") else "disconnected",
            "can_connect": not bool(error), "can_disconnect": bool(profile.get("can_restore") or profile.get("active")),
            "error": str(error)[:1000], "origin": selected,
            "reload_required": bool(result.get("reload_required") or any(
                item.get("key") == "connection" for item in status.get("restart_notice", {}).get("items", []) if isinstance(item, dict))),
            "warning_count": len(warnings) if isinstance(warnings, list) else 0,
            "codex_restart": capability}
    if reconnect is not None:
        value["codex_reconnect"] = reconnect
    return value

try:
    result = control()
except Exception as exc:
    result = {"state": "error", "error": "远程接入操作未完成：" + type(exc).__name__ + "；请刷新状态后检查远程中转日志。"}
print(RESULT_PREFIX + json.dumps(result, ensure_ascii=True))
'''.replace("DEFAULT_PORT", repr(DEFAULT_PORT)).replace("RESULT_PREFIX", repr(RESULT_PREFIX))
