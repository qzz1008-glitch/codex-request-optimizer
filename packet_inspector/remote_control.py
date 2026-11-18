"""Fixed, standard-library-only script sent over the user's existing SSH session."""
from .entry_port import DEFAULT_PORT
from .native_state import LABEL_READER

RESULT_PREFIX = "INSPECTOR_CONNECTION_RESULT:"
SSH_LABEL_BATCH = 32

SCRIPT = LABEL_READER + r'''
from contextlib import contextmanager
import base64, hashlib, json, os, pathlib, re, select, shutil, signal, socket, stat, struct, subprocess, tempfile, time
from http.client import HTTPException, HTTPResponse
from urllib.parse import urlsplit
from urllib.request import build_opener, ProxyHandler, HTTPRedirectHandler, Request
from urllib.error import HTTPError

RECONNECT_SOCKET_UNVERIFIED = "无法确认远程 Codex 控制套接字属于当前 SSH 账号和配置目录。"
RECONNECT_PROCESS_CHANGED = "远程 Codex 后台进程刚发生变化，请等 ChatGPT 恢复连接后再试。"
RECONNECT_STATUS_UNAVAILABLE = "无法读取远程 Codex 后台状态。"
RECONNECT_NOT_READY = "远程 Codex 后台尚未就绪，请等 ChatGPT 恢复连接后再试。"
RECONNECT_UNCONFIRMED = "重连结果未确认，请刷新状态后检查；待生效提醒已保留。"
SSH_DRAIN_TIMEOUT = 60
SSH_RECONNECT_TIMEOUT = 100

class ReconnectProbeError(ValueError):
    """A fixed diagnostic that can be returned without exposing the SSH environment."""

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None

def managed_daemon_running(value):
    # The native version command omits backend when the socket serves an unmanaged app server.
    backend = value.get("backend")
    return value.get("status") == "running" and isinstance(backend, str) and bool(backend)

def owned_native_socket(home, socket_path):
    if not socket_path.is_absolute():
        return False
    # Codex can link the control socket to a shorter runtime path outside CODEX_HOME.
    parent, resolved_home = socket_path.parent.resolve(), home.resolve()
    if parent != resolved_home and resolved_home not in parent.parents:
        return False
    endpoint = socket_path.stat()
    return stat.S_ISSOCK(endpoint.st_mode) and endpoint.st_uid == os.getuid()

def socket_peer_pid(socket_path):
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
            peer.settimeout(1)
            peer.connect(str(socket_path.resolve(strict=True)))
            pid, uid, _ = struct.unpack("iII", peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("iII")))
            if pid <= 0 or uid != os.getuid():
                raise ReconnectProbeError("无法确认远程 Codex 后台进程属于当前 SSH 账号。")
            # Close normally so Codex does not report failed WebSocket handshakes on status checks.
            key = base64.b64encode(os.urandom(16))
            peer.sendall(b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: Upgrade\r\nUpgrade: websocket\r\n"
                         b"Sec-WebSocket-Version: 13\r\nSec-WebSocket-Key: " + key + b"\r\n\r\n")
            with HTTPResponse(peer) as response:
                response.begin()
                if response.status != 101:
                    raise ReconnectProbeError("远程 Codex 后台未通过 WebSocket 握手检查。")
            peer.sendall(b"\x88\x80" + os.urandom(4))
            peer.recv(128)
    except (OSError, HTTPException) as exc:
        raise ReconnectProbeError("无法通过控制套接字连接远程 Codex 后台。") from exc
    return pid

def ssh_process_identity(pid):
    image = (pathlib.Path("/proc") / str(pid) / "exe").stat()
    return pid, image.st_dev, image.st_ino

def confirm_ssh_process(socket_path, identity):
    pid = socket_peer_pid(socket_path)
    if pid != identity[0] or ssh_process_identity(pid) != identity:
        raise ReconnectProbeError(RECONNECT_PROCESS_CHANGED)

@contextmanager
def verified_ssh_app_server(home, socket_path):
    if not (hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal") and hasattr(socket, "SO_PEERCRED")):
        raise ReconnectProbeError("远程 Python 环境缺少安全重载 Codex 所需的进程接口。")
    if not owned_native_socket(home, socket_path):
        raise ReconnectProbeError(RECONNECT_SOCKET_UNVERIFIED)
    pid = socket_peer_pid(socket_path)
    pidfd = os.pidfd_open(pid)
    try:
        identity = ssh_process_identity(pid)
        process = pathlib.Path("/proc") / str(pid)
        args = (process / "cmdline").read_bytes().split(b"\0")
        server_args = args[args.index(b"app-server", 1) + 1:] if b"app-server" in args[1:] else []
        default_socket = b"--listen=unix://" in server_args or (b"--listen", b"unix://") in zip(server_args, server_args[1:])
        if server_args[:1] == [b"proxy"] or not default_socket:
            raise ReconnectProbeError("远程 Codex 后台未通过 Unix 套接字启动参数检查。")
        paths = dict(item.split(b"=", 1) for item in (process / "environ").read_bytes().split(b"\0")
                     if item.startswith((b"CODEX_HOME=", b"HOME=")))
        server_home = paths.get(b"CODEX_HOME") or os.path.join(paths.get(b"HOME", b""), b".codex")
        server_home = pathlib.Path(os.fsdecode(server_home))
        if not server_home.is_absolute() or server_home.resolve() != home.resolve():
            raise ReconnectProbeError("远程 Codex 后台的配置目录与当前连接不匹配。")
        # Codex handles SIGHUP as a graceful-only drain, including repeated requests.
        with (process / "status").open() as status:
            caught = next((int(line.split()[1], 16) for line in status if line.startswith("SigCgt:")), 0)
        if not caught & (1 << (signal.SIGHUP - 1)):
            raise ReconnectProbeError("远程 Codex 后台未注册重载所需的 SIGHUP 处理程序。")
        confirm_ssh_process(socket_path, identity)
        yield identity, pidfd
    finally:
        os.close(pidfd)

def native_codex(home):
    search = str(pathlib.Path(os.environ.get("CODEX_INSTALL_DIR") or pathlib.Path.home() / ".local/bin")) + os.pathsep + os.environ.get("PATH", "")
    cli = shutil.which("codex", path=search)
    if not cli:
        return {"supported": False, "reason": "本次 SSH 登录找不到 Codex 命令。"}, None
    supported = {"supported": True, "reason": ""}
    env = dict(os.environ, CODEX_HOME=str(home))
    reason = RECONNECT_STATUS_UNAVAILABLE
    try:
        value = native_command(cli, "version", env, 8)
        if value.get("status") != "running":
            raise ReconnectProbeError(RECONNECT_NOT_READY)
        socket_path = value.get("socketPath")
        if not isinstance(socket_path, str):
            raise ReconnectProbeError("远程 Codex 后台未报告控制套接字地址。")
        socket_path = pathlib.Path(socket_path)
        reason = "无法读取远程 Codex 的控制套接字或进程信息。"
        if managed_daemon_running(value):
            managed = value.get("managedCodexPath")
            managed_path = pathlib.Path(managed) if isinstance(managed, str) else None
            if managed_path is None or not managed_path.is_absolute() or not managed_path.is_file():
                raise ReconnectProbeError("无法确认远程 Codex 守护进程使用的可执行文件。")
            if not owned_native_socket(home, socket_path):
                raise ReconnectProbeError(RECONNECT_SOCKET_UNVERIFIED)
            return supported, (str(managed_path), env, socket_path, None)
        with verified_ssh_app_server(home, socket_path) as (identity, _):
            return supported, (cli, env, socket_path, identity)
    except ReconnectProbeError as exc:
        reason = str(exc)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        pass
    return {"supported": False, "reason": reason}, None

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

def restart_native(context, unavailable_reason):
    if context is None:
        raise ReconnectProbeError(unavailable_reason)
    cli, env, socket_path, identity = context
    if identity is not None:
        restart_ssh_app_server(cli, env, socket_path, identity)
        return
    result = native_command(cli, "restart", env, 120)
    if result.get("status") != "restarted" or result.get("socketPath") != str(socket_path):
        raise ValueError("Codex 原生后台未确认重载完成，请刷新状态后检查。")
    ready = native_command(cli, "version", env, 8)
    if not managed_daemon_running(ready) or ready.get("socketPath") != str(socket_path):
        raise ValueError("Codex 原生后台尚未就绪，请刷新状态后检查。")

def confirm_ssh_app_server(cli, env, socket_path, identity, timeout):
    # The CLI's version command initializes a connection to the app server.
    # A launcher or a different CLI installation can probe the same running executable.
    try:
        ready = native_command(cli, "version", env, timeout)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        raise ReconnectProbeError(RECONNECT_STATUS_UNAVAILABLE) from exc
    if ready.get("status") != "running":
        raise ReconnectProbeError(RECONNECT_NOT_READY)
    if ready.get("socketPath") != str(socket_path):
        raise ReconnectProbeError(RECONNECT_PROCESS_CHANGED)
    confirm_ssh_process(socket_path, identity)
    return ready

def restart_ssh_app_server(cli, env, socket_path, expected_identity):
    home = pathlib.Path(env["CODEX_HOME"])
    with verified_ssh_app_server(home, socket_path) as (identity, pidfd):
        if identity != expected_identity:
            raise ReconnectProbeError(RECONNECT_PROCESS_CHANGED)
        ready = confirm_ssh_app_server(cli, env, socket_path, identity, 8)
        if managed_daemon_running(ready):
            raise ReconnectProbeError(RECONNECT_PROCESS_CHANGED)
        exit_event = select.poll()
        exit_event.register(pidfd, select.POLLIN)
        signal.pidfd_send_signal(pidfd, signal.SIGHUP)
        if not exit_event.poll(SSH_DRAIN_TIMEOUT * 1000):
            raise ValueError(RECONNECT_UNCONFIRMED)
        # Desktop owns the proxy and may need its WebSocket heartbeat to detect the disconnect.
        deadline = time.monotonic() + SSH_RECONNECT_TIMEOUT
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                with verified_ssh_app_server(home, socket_path) as (replacement, _):
                    if replacement[0] != identity[0]:
                        confirm_ssh_app_server(cli, env, socket_path, replacement, min(8, remaining))
                        return
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
                pass
            time.sleep(max(0, min(0.5, deadline - time.monotonic())))
    raise ValueError(RECONNECT_UNCONFIRMED)

def config_digest(config):
    with config.open("rb") as source:
        value = source.read(2 * 1024 * 1024 + 1)
    if len(value) > 2 * 1024 * 1024:
        raise ValueError("Codex 配置过大，无法确认重连范围。")
    return hashlib.sha256(value).digest()

def config_bytes(path):
    try:
        with path.open("rb") as source:
            value = source.read(512 * 1024 + 1)
    except FileNotFoundError:
        return None
    if len(value) > 512 * 1024:
        raise ValueError("远程 Codex 配置过大。")
    return value

def remote_files(home):
    paths = [home / "config.toml", *sorted(home.glob("*.config.toml"))]
    if len(paths) > 32:
        raise ValueError("远程 Codex 配置文件过多。")
    files = {path.name: config_bytes(path) for path in paths}
    if sum(len(raw or b"") for raw in files.values()) > 512 * 1024:
        raise ValueError("远程 Codex 配置过大。")
    return files

def remote_settings(home):
    def load(path):
        try:
            value = json.loads(config_bytes(path) or b"{}")
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}
    auth = load(home / "auth.json")
    mode = auth.get("auth_mode") or ("apikey" if auth.get("OPENAI_API_KEY") else "chatgpt")
    old = load(pathlib.Path.home() / ".codex-packet-inspector" / "settings.json")
    routes = old.get("provider_routes", {})
    return {"api_key": mode == "apikey", "legacy_token": old.get("relay_token"),
            "legacy_upstream": old.get("upstream"), "legacy_routes": {
                key: value.get("upstream") for key, value in routes.items() if isinstance(value, dict)
            } if isinstance(routes, dict) else {}}

def write_config(path, raw, mode):
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".inspector-", delete=False) as stage:
        temp = pathlib.Path(stage.name)
        try:
            stage.write(raw)
            stage.flush()
            os.fsync(stage.fileno())
            stage.close()
            temp.chmod(mode)
            os.replace(temp, path)
        finally:
            if temp.exists():
                temp.unlink()

def write_remote_files(home, options):
    import fcntl
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    with os.fdopen(os.open(str(home / ".inspector-ssh.lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), "rb") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        current = remote_files(home)
        expected = {name: hashlib.sha256(raw).hexdigest() if raw is not None else None
                    for name, raw in current.items()}
        if expected != options["expected"] or not set(options["updates"]) <= set(current):
            raise ValueError("远程 Codex 配置已变化，请重新尝试。")
        updates = {name: base64.b64decode(raw, validate=True) for name, raw in options["updates"].items()}
        paths = {name: (home / name).resolve() for name in updates}
        if len(set(paths.values())) != len(paths):
            raise ValueError("多个远程配置指向同一文件，接入已停止。")
        revision = time.time_ns()
        applied = []
        try:
            for name, raw in updates.items():
                path, before = paths[name], current[name]
                if config_bytes(path) != before:
                    raise ValueError("远程 Codex 配置已变化，请重新尝试。")
                mode = stat.S_IMODE(path.stat().st_mode) if before is not None else 0o600
                if before is not None:
                    write_config(path.with_name(path.name + ".inspector-backup-ssh-" + str(revision)), before, 0o600)
                write_config(path, raw, mode)
                applied.append((path, before, raw, mode))
        except (OSError, ValueError):
            for path, before, raw, mode in reversed(applied):
                if config_bytes(path) == raw:
                    if before is None:
                        path.unlink()
                    else:
                        write_config(path, before, mode)
            raise
    return {"changed": bool(updates)}

def tunnel_control(home, options):
    home = home.resolve()
    action = options["action"]
    account = {"home": str(home), "uid": os.getuid()}
    if action == "tunnel_labels":
        ids = options.get("ids")
        if not isinstance(ids, list) or not 0 < len(ids) <= SSH_LABEL_BATCH or any(
                not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", value)
                for value in ids):
            raise ValueError("会话 ID 无效。")
        if any(key in options and options[key] != value for key, value in account.items()):
            raise ValueError("SSH 登录账号或 Codex 配置目录已变化。")
        return {**account, **read_label_data(home, ids)}
    if action == "tunnel_snapshot":
        files = {name: base64.b64encode(raw).decode("ascii") if raw is not None else None
                 for name, raw in remote_files(home).items()}
        value = {**account, "files": files, **remote_settings(home)}
        if options.get("native"):
            value["codex_restart"], _ = native_codex(home)
        return value
    if any(options.get(key) != value for key, value in account.items()):
        raise ValueError("SSH 登录账号或 Codex 配置目录已变化，请重新确认操作。")
    if action == "tunnel_probe":
        port, token = options["port"], options["token"]
        if type(port) is not int or not 1024 <= port <= 65535 or not re.fullmatch(r"[0-9a-f]{48}", token):
            raise ValueError("SSH 转发地址无效。")
        url = f"http://127.0.0.1:{port}/relay/{token}/ready"
        opener = build_opener(ProxyHandler({}), NoRedirect())
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            try:
                with opener.open(url, timeout=0.5) as response:
                    answer = response.read(64)
                    if re.fullmatch(rb"[0-9a-f]{48}", answer):
                        return {"proof": answer.decode("ascii")}
            except (OSError, HTTPException):
                pass
            time.sleep(0.2)
        return {}
    if action == "tunnel_write":
        return write_remote_files(home, options)
    if action == "tunnel_reconnect":
        config = home / "config.toml"
        capability, context = native_codex(home)
        try:
            digest = config_digest(config)
            restart_native(context, capability["reason"])
            warning = "" if config_digest(config) == digest else "Codex 已重载，但配置又有变化，请再次重新连接。"
            return {"state": "reloaded", "warning": warning, "codex_restart": capability}
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            return {"state": "unconfirmed", "error": str(exc)[:1500], "codex_restart": capability}
    raise ValueError("无效的远程接入操作。")

def control():
    options = json.loads(INPUT)
    home = pathlib.Path(os.environ.get("CODEX_HOME") or pathlib.Path.home() / ".codex").expanduser()
    config = home / "config.toml"
    if str(options.get("action", "")).startswith("tunnel_"):
        try:
            return tunnel_control(home, options)
        except ValueError as exc:
            return {"error": str(exc)[:1000]}
        except OSError as exc:
            return {"error": "无法读写远程 Codex 配置（" + type(exc).__name__ + "）。"}
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
            restart_native(native_context, capability["reason"])
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
            reconnect = {"state": "unconfirmed", "error": RECONNECT_UNCONFIRMED}
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
'''.replace("DEFAULT_PORT", repr(DEFAULT_PORT)).replace("RESULT_PREFIX", repr(RESULT_PREFIX)).replace("SSH_LABEL_BATCH", str(SSH_LABEL_BATCH))
