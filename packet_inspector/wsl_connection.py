"""Explicit, reversible connection of running local WSL Codex installations.

No listeners, tunnels, WSL settings or Linux tools are installed. Linux SQLite
indexes are queried inside WSL, so their WAL/locking semantics stay native.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import threading
import time
from urllib.parse import urlsplit

from .config_link import ConfigLink
from .providers import ProviderManager


PROBE_SCRIPT = r'''
import json,sys,urllib.request
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
try:
    with opener.open("http://127.0.0.1:"+str(int(sys.argv[1]))+"/api/wsl-probe",timeout=2) as response:
        data=json.loads(response.read(4096))
except Exception as exc:
    data={"error":type(exc).__name__}
print(json.dumps(data))
'''

# Only requested IDs and display metadata leave Linux; never auth or messages.
LABEL_SCRIPT = r'''
import json,sqlite3,sys
from contextlib import closing
from pathlib import Path
request=json.load(sys.stdin)
home=Path(request["home"])
wanted=set(request["ids"][:300])
rows,edges,index=[],[],{}
def revision(path):
    value=path.stem.removeprefix("state_")
    return int(value) if value.isdigit() else -1
for path in sorted(home.glob("state_*.sqlite"),key=revision,reverse=True):
    if not wanted: break
    try:
        with closing(sqlite3.connect(path.as_uri()+"?mode=ro",uri=True,timeout=.2)) as db:
            db.execute("PRAGMA query_only=ON")
            columns={row[1] for row in db.execute("PRAGMA table_info(threads)")}
            if "id" not in columns: continue
            times=(["nullif(updated_at_ms,0)/1000.0"] if "updated_at_ms" in columns else [])
            times += ["updated_at"] if "updated_at" in columns else []
            updated="coalesce("+",".join(times+["NULL"])+")" if times else "NULL"
            fields=["id"]+["substr("+key+",1,"+str(size)+")" if key in columns else "NULL"
                for key,size in (("title",512),("name",512),("agent_nickname",128),("source",8192))]+[updated]
            ids=sorted(wanted); placeholders=",".join("?" for _ in ids)
            current=db.execute("SELECT "+",".join(fields)+" FROM threads WHERE id IN ("+placeholders+")",ids).fetchall()
            try:
                edges += db.execute("SELECT child_thread_id,min(parent_thread_id) FROM thread_spawn_edges WHERE child_thread_id IN ("+
                    placeholders+") GROUP BY child_thread_id HAVING count(DISTINCT parent_thread_id)=1",ids).fetchall()
            except sqlite3.OperationalError: pass
            rows += current
            wanted.difference_update(row[0] for row in current)
    except (OSError,sqlite3.Error): continue
try:
    with (home/"session_index.jsonl").open("rb") as stream:
        stream.seek(0,2); offset=max(0,stream.tell()-1024*1024); stream.seek(offset)
        lines=stream.read(1024*1024).split(b"\n")
    for line in reversed(lines[1:-1] if offset else lines[:-1]):
        try:
            item=json.loads(line); key=item.get("id"); title=item.get("thread_name")
            if key in request["ids"] and key not in index and isinstance(title,str) and title:
                index[key]=title[:512]
        except (ValueError,TypeError,AttributeError): pass
except OSError: pass
print(json.dumps({"rows":rows,"edges":edges,"index":index},ensure_ascii=True))
'''


class WslError(ValueError):
    pass


def decode_output(value):
    if value.startswith((b"\xff\xfe", b"\xfe\xff")):
        return value.decode("utf-16", errors="replace")
    return value.decode("utf-16-le" if b"\0" in value[:128] else "utf-8", errors="replace")


def validate_target(distribution, home):
    if (not isinstance(distribution, str) or not re.fullmatch(r"[\w .-]{1,128}", distribution)
            or distribution in {".", ".."}):
        raise WslError("invalid_distribution")
    if (not isinstance(home, str) or not home.startswith("/") or home.startswith("//")
            or ".." in home.split("/") or "\\" in home or any(ord(c) < 32 for c in home)
            or len(home) > 2048 or home == "/"):
        raise WslError("invalid_codex_home")
    return distribution, str(PurePosixPath(home))


class LocalWsl:
    def __init__(self):
        self.executable = shutil.which("wsl.exe") if os.name == "nt" else None

    def run(self, arguments, *, data=None, timeout=5):
        if not self.executable:
            raise WslError("wsl_unavailable")
        try:
            result = subprocess.run([self.executable, *arguments], input=data, capture_output=True,
                                    timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired:
            raise WslError("wsl_timeout") from None
        except OSError:
            raise WslError("wsl_unavailable") from None
        if result.returncode:
            # Do not log shell profiles or arbitrary subprocess output.
            raise WslError("wsl_command_failed")
        if len(result.stdout) > 4 * 1024 * 1024:
            raise WslError("wsl_output_too_large")
        return decode_output(result.stdout)

    def running(self):
        if not self.executable:
            return []
        text = self.run(["--list", "--running", "--quiet"])
        names = [line.strip().lstrip("\ufeff") for line in text.splitlines() if line.strip()]
        return [name for name in names if re.fullmatch(r"[\w .-]{1,128}", name)
                and not name.lower().startswith(("docker", "rancher", "podman"))][:8]

    def home(self, distribution):
        text = self.run(["-d", distribution, "--exec", "sh", "-lc",
                         'printf "\\nCPI_CODEX_HOME=%s\\n" "${CODEX_HOME:-$HOME/.codex}"'])
        homes = [line.removeprefix("CPI_CODEX_HOME=") for line in text.splitlines()
                 if line.startswith("CPI_CODEX_HOME=")]
        if len(homes) != 1:
            raise WslError("codex_home_not_found")
        return validate_target(distribution, homes[0])[1]

    def config(self, distribution, home):
        validate_target(distribution, home)
        for host in ("wsl.localhost", "wsl$"):
            path = Path("\\\\" + host + "\\" + distribution).joinpath(*PurePosixPath(home).parts[1:], "config.toml")
            if path.is_file():
                return path
        return None

    def probe(self, distribution, port, marker):
        try:
            data = json.loads(self.run(["-d", distribution, "--exec", "python3", "-c", PROBE_SCRIPT, str(port)]))
        except WslError:
            raise WslError("wsl_python_probe_failed") from None
        except (ValueError, TypeError):
            raise WslError("relay_unreachable") from None
        if isinstance(data, dict) and data.get("error"):
            raise WslError("relay_unreachable")
        if not isinstance(data, dict) or data.get("instance") != marker:
            raise WslError("relay_instance_mismatch")

    def labels(self, distribution, home, ids):
        body = json.dumps({"home": home, "ids": ids}).encode("utf-8")
        return json.loads(self.run(["-d", distribution, "--exec", "python3", "-c", LABEL_SCRIPT], data=body))


class WslConnection:
    def __init__(self, data_dir, backend=None):
        self.directory = Path(data_dir) / "wsl-connections"
        self.backend = backend or LocalWsl()
        self.result = {"targets": [], "warnings": [], "changed": False, "reload_required": False}
        self._lock = threading.RLock()
        self._running_at, self._running = 0, []
        self._labels = {}
        self.metadata_error = ""
        self._metadata_retry = 0

    def _distributions(self, *, refresh=False):
        if refresh or time.monotonic() - self._running_at > 15:
            self._running = self.backend.running()
            self._running_at = time.monotonic()
        return self._running

    def _targets(self):
        for path in sorted(self.directory.glob("*/target.json")):
            try:
                value = json.loads(path.read_text(encoding="utf8"))
                validate_target(value["distribution"], value["home"])
                yield path.parent, value
            except (OSError, ValueError, KeyError, TypeError):
                continue

    def apply(self, connected, endpoint, upstream, routes, publish, *, retry_limit=None):
        with self._lock:
            result = {"targets": [], "warnings": [], "changed": False, "reload_required": False}
            self.result = result
            try:
                running = self._distributions(refresh=True)
            except WslError as exc:
                # WSL's Windows stub is present even when WSL is not installed.
                if list(self._targets()):
                    result["warnings"].append({"distribution": "WSL", "reason": str(exc)})
                return result
            candidates = []
            if connected:
                for distribution in running:
                    try:
                        home = self.backend.home(distribution)
                        config = self.backend.config(distribution, home)
                        if config is None:
                            continue
                        identity = hashlib.sha256((distribution + "\n" + home).encode()).hexdigest()[:24]
                        candidates.append((self.directory / identity, {"distribution": distribution, "home": home}, config))
                    except (WslError, OSError) as exc:
                        result["warnings"].append({"distribution": distribution, "reason": str(exc) if isinstance(exc, WslError) else type(exc).__name__})
            else:
                for directory, target in self._targets():
                    if not (directory / "link.json").is_file():
                        continue
                    if target["distribution"] not in running:
                        result["warnings"].append({"distribution": target["distribution"], "reason": "restore_pending"})
                        continue
                    candidates.append((directory, target, Path(target["config"])))
            for directory, target, config in candidates:
                try:
                    link = ConfigLink(config, directory / "link.json")
                    if connected:
                        own = urlsplit(endpoint)
                        marker = hashlib.sha256(endpoint.encode()).hexdigest()
                        # Verify this exact Windows instance before any Linux write.
                        self.backend.probe(target["distribution"], own.port, marker)
                        directory.mkdir(parents=True, exist_ok=True)
                        target = {**target, "config": str(config)}
                        staging = directory / "target.new"
                        staging.write_text(json.dumps(target), encoding="utf8")
                        staging.replace(directory / "target.json")
                        def publish_routes(updated):
                            routes.clear()
                            routes.update(updated)
                            publish(updated)
                        update = ProviderManager(link, endpoint, upstream).sync(routes, publish_routes, retry_limit=retry_limit)
                    else:
                        if not config.is_file() or any(not Path(name).is_file() for name in link._state().get("files", {})):
                            raise WslError("restore_pending")
                        update = link.restore() if link._state() else {"reload_required": False}
                    result["targets"].append({**target, "active": connected, "changed": bool(update.get("reload_required"))})
                    result["changed"] |= bool(update.get("reload_required"))
                    result["reload_required"] |= bool(update.get("reload_required"))
                    result["warnings"].extend({"distribution": target["distribution"], "reason": item["reason"]}
                                              for item in update.get("warnings", []))
                except (OSError, ValueError) as exc:
                    result["warnings"].append({"distribution": target["distribution"], "reason": str(exc) if isinstance(exc, WslError) else type(exc).__name__})
            self._labels.clear()
            return result

    def labels(self, ids):
        """Bounded, cached, read-only display metadata; never runs on forwarding."""
        ids = sorted({value for value in ids if isinstance(value, str) and re.fullmatch(r"[a-fA-F0-9-]{36}", value)})[:300]
        if not ids:
            return {}
        from .session_summary import subagent_source, subagent_title
        found = {}
        with self._lock:
            if time.monotonic() < self._metadata_retry:
                return found
            targets = list(self._targets())
            if not targets:
                return found
            try:
                running = self._distributions()
                for directory, target in targets:
                    if target["distribution"] not in running:
                        continue
                    key = str(directory)
                    cached = self._labels.get(key)
                    if not cached or time.monotonic() - cached[0] > 15 or not set(ids) <= cached[1]:
                        requested = sorted(set(ids) | (cached[1] if cached else set()))
                        if len(requested) > 300:
                            requested = ids
                        raw = self.backend.labels(target["distribution"], target["home"], requested)
                        labels, parents = {}, {}
                        for child, parent in raw.get("edges", []):
                            if child in requested and isinstance(parent, str) and re.fullmatch(r"[a-fA-F0-9-]{36}", parent) and child != parent:
                                parents.setdefault(child, set()).add(parent)
                        for thread, title, name, nickname, source, updated in raw.get("rows", []):
                            if thread not in requested:
                                continue
                            subagent = subagent_source(source)
                            spawn = subagent.get("thread_spawn", {}) if subagent else {}
                            parent = spawn.get("parent_thread_id") if isinstance(spawn, dict) else None
                            if isinstance(parent, str) and re.fullmatch(r"[a-fA-F0-9-]{36}", parent) and parent != thread:
                                parents.setdefault(thread, set()).add(parent)
                            labels[thread] = {"title": name if isinstance(name, str) and name.strip() else title,
                                              "updated_at": updated, "fallback_title": subagent_title(source, name, nickname),
                                              **({"session_kind": "subagent"} if subagent else {})}
                        for thread, title in raw.get("index", {}).items():
                            if thread in requested and isinstance(title, str) and not labels.get(thread, {}).get("title"):
                                labels.setdefault(thread, {})["title"] = title[:512]
                        for thread, values in parents.items():
                            if len(values) == 1:
                                labels.setdefault(thread, {}).update(parent_thread_id=next(iter(values)), session_kind="subagent")
                        cached = (time.monotonic(), set(requested), labels)
                        self._labels[key] = cached
                    for thread, label in cached[2].items():
                        if thread in ids:
                            found.setdefault(thread, label)
                self.metadata_error = ""
            except (OSError, ValueError, TypeError, KeyError) as exc:
                self.metadata_error = str(exc) if isinstance(exc, WslError) else type(exc).__name__
                self._metadata_retry = time.monotonic() + 15
        return found
