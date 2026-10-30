"""Durable grants and aggregate statistics. Never stores model credentials or content."""
from contextlib import contextmanager
from datetime import datetime, timedelta
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time

from .sharing_auth import SharingError
from .sharing_pricing import MIN_BUDGET_USD, budget_nanos, reference_cost

CLIENT_HEADER = "X-Inspector-Client"
MIN_TTL_SECONDS = 86400
COUNTERS = ("input_tokens", "output_tokens", "total_tokens", "cached_input_tokens", "responses",
            "missing_responses", "unpriced_responses", "cost_nanos", "sent_bytes", "received_bytes", "requests")


class ExitMachineMismatch(SharingError):
    pass


class QuotaMachineMismatch(SharingError):
    pass


def protect(value, decrypt=False):
    if os.name != "nt":
        return value  # Private-file fallback for isolated non-Windows test fixtures.
    class Blob(ctypes.Structure):
        _fields_ = [("size", ctypes.c_ulong), ("data", ctypes.POINTER(ctypes.c_ubyte))]
    source = ctypes.create_string_buffer(value)
    incoming = Blob(len(value), ctypes.cast(source, ctypes.POINTER(ctypes.c_ubyte)))
    outgoing = Blob()
    library = ctypes.WinDLL("crypt32", use_last_error=True)
    function = library.CryptUnprotectData if decrypt else library.CryptProtectData
    if not function(ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing)):
        raise OSError("无法读取本机共享凭据。" if decrypt else "无法保护本机共享凭据。")
    try:
        return ctypes.string_at(outgoing.data, outgoing.size)
    finally:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree(ctypes.cast(outgoing.data, ctypes.c_void_p))


def expiration(value, *, since=None):
    if value is None or value == "":
        return None
    since = time.time() if since is None else since
    if type(value) not in {int, float} or not since + MIN_TTL_SECONDS <= value <= 253402214400:
        raise SharingError("期限至少为一天，也可选择无期限。")
    return float(value)


def week_start(now=None):
    current = datetime.fromtimestamp(time.time() if now is None else now)
    return (current.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=current.weekday())).timestamp()


def day_key(now=None):
    return datetime.fromtimestamp(time.time() if now is None else now).strftime("%Y-%m-%d")


class CredentialVault:
    def __init__(self, root):
        self.path = Path(root) / "sharing-credentials.sqlite"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript("""CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS credentials (id TEXT PRIMARY KEY, kind TEXT NOT NULL,
                direction TEXT NOT NULL, digest TEXT, secret BLOB NOT NULL, data TEXT NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS grants ON credentials(digest) WHERE digest IS NOT NULL;""")
            for row in db.execute("SELECT id,data FROM credentials"):
                data = json.loads(row["data"])
                records = [data] + ([data["snapshot"]] if isinstance(data.get("snapshot"), dict) else [])
                if any(key in record for record in records for key in ("bound", "client_hash")):
                    for record in records:
                        for key in ("bound", "client_hash"):
                            record.pop(key, None)
                    self._save(db, row["id"], data)
        if os.name != "nt":
            self.path.chmod(0o600)
        self.client_id = self.secret("client-id", lambda: secrets.token_hex(32).encode()).decode()

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def secret(self, key, factory):
        with self.connection() as db:
            row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            if row:
                return protect(row[0], True)
            value = factory()
            db.execute("INSERT INTO meta VALUES (?,?)", (key, protect(value)))
            return value

    @staticmethod
    def _save(db, identifier, data):
        db.execute("UPDATE credentials SET data=? WHERE id=?", (json.dumps(data, separators=(",", ":")), identifier))

    @staticmethod
    def _roll(data):
        if data.get("weekly_reset") and data["period_started_at"] < week_start():
            data.update(period_id=secrets.token_hex(8), period_started_at=week_start(), period_cost_nanos=0)
            return True
        return False

    @staticmethod
    def _unique_label(db, kind, label, *, identifier=None, default="同事"):
        used = set()
        for row in db.execute("SELECT id,data FROM credentials WHERE kind=?", (kind,)):
            data = json.loads(row["data"])
            if row["id"] != identifier and not data.get("internal"):
                used.add(str(data.get("label", "")).strip().casefold())
        value = str(label or "").strip()[:80].strip()
        if value:
            if value.casefold() in used:
                raise SharingError("同类型凭据名称已存在，请使用其他名称。")
            return value
        if default is None:
            raise SharingError("请输入凭据名称。")
        value, suffix = default, 1
        while value.casefold() in used:
            suffix += 1
            value = f"{default} {suffix}"
        return value

    def prepare_label(self, kind, label):
        # Preflight before starting services; issue() checks again atomically.
        with self.connection() as db:
            return self._unique_label(db, kind, label)

    def issue(self, kind, label="", expires_at=None, budget=MIN_BUDGET_USD, weekly_reset=False, *, internal=False, issued_at=None):
        if kind not in {"quota", "exit"} or type(weekly_reset) is not bool:
            raise SharingError("凭据类型或刷新设置无效。")
        issued_at = time.time() if issued_at is None else issued_at
        limit = budget_nanos(budget) if kind == "quota" else 0
        expiry = expiration(expires_at, since=issued_at)
        identifier, token = secrets.token_hex(12), secrets.token_urlsafe(32)
        data = {"id": identifier, "kind": kind, "direction": "out", "label": str(label or "同事")[:80],
                "created_at": issued_at, "expires_at": expiry, "enabled": True, "internal": internal,
                "budget_nanos": limit if kind == "quota" else 0, "weekly_reset": weekly_reset if kind == "quota" else False,
                "period_id": secrets.token_hex(8), "period_started_at": time.time(), "period_cost_nanos": 0,
                "totals": {key: 0 for key in COUNTERS}, "daily": {}}
        if kind == "exit" and internal:
            data["exit_client_hash"] = hashlib.sha256(self.client_id.encode()).hexdigest()
        with self.connection() as db:
            if not internal:
                data["label"] = self._unique_label(db, kind, label)
            db.execute("INSERT INTO credentials VALUES (?,?,?,?,?,?)", (identifier, kind, "out",
                       hashlib.sha256(token.encode()).hexdigest(), protect(token.encode()), json.dumps(data)))
        return identifier, token

    def import_link(self, kind, link, label="", snapshot=None):
        # Keep one history entry per grant even if the sender's address changes.
        grant = link.rsplit("#", 1)[-1]
        digest = hashlib.sha256((kind + grant).encode()).hexdigest()
        with self.connection() as db:
            row = db.execute("SELECT id,data FROM credentials WHERE direction='in' AND digest=?", (digest,)).fetchone()
            identifier = row["id"] if row else secrets.token_hex(12)
            data = json.loads(row["data"]) if row else {"id": identifier, "kind": kind, "direction": "in", "created_at": time.time()}
            name = str(label or "").strip()[:80].strip()
            if not name and row:
                name = data.get("label", "")
            if not row or name != data.get("label"):
                name = self._unique_label(db, kind, name, identifier=identifier, default="借入凭据")
            data.update(label=name, last_seen=time.time())
            if snapshot is not None:
                data["snapshot"] = snapshot
                data["online"] = True
            db.execute("INSERT OR REPLACE INTO credentials VALUES (?,?,?,?,?,?)",
                       (identifier, kind, "in", digest, protect(link.encode()), json.dumps(data)))
            return identifier

    def get_secret(self, identifier, kind=None, direction=None):
        with self.connection() as db:
            row = db.execute("SELECT * FROM credentials WHERE id=?", (identifier,)).fetchone()
            if not row or (kind and row["kind"] != kind) or (direction and row["direction"] != direction):
                raise SharingError("凭据不存在。")
            return protect(row["secret"], True).decode()

    def authenticate(self, kind, value, client="", *, spend=False):
        # Both grant types belong to one persistent client identity. Delegated
        # exit tunnels retain that borrower's identity.
        if not isinstance(value, str) or not value.startswith("Bearer "):
            raise SharingError("凭据无效或已撤销。")
        if kind == "exit" and (not isinstance(client, str) or not re.fullmatch(r"[a-f0-9]{64}", client)):
            raise SharingError("出口凭据需要有效的机器标识，请更新使用方软件。")
        if kind == "quota" and (not isinstance(client, str) or not re.fullmatch(r"[a-f0-9]{64}", client)):
            raise SharingError("额度凭据需要有效的机器标识，请更新使用方软件。")
        digest = hashlib.sha256(value[7:].encode()).hexdigest()
        with self.connection() as db:
            row = db.execute("SELECT id,data FROM credentials WHERE kind=? AND direction='out' AND digest=?", (kind, digest)).fetchone()
            if row is None:
                raise SharingError("凭据无效或已撤销。")
            data = json.loads(row["data"])
            self._roll(data)
            if not data["enabled"] or (data["expires_at"] is not None and time.time() >= data["expires_at"]):
                raise SharingError("凭据已停用或到期。")
            if spend and data["budget_nanos"] and data["period_cost_nanos"] >= data["budget_nanos"]:
                raise SharingError("此凭据的本期额度已用完。")
            if kind in {"exit", "quota"}:
                binding = hashlib.sha256(client.encode()).hexdigest()
                key = kind + "_client_hash"
                if data.get(key) and not secrets.compare_digest(data[key], binding):
                    if kind == "exit":
                        raise ExitMachineMismatch("此出口凭据已绑定其他机器，请使用独立凭据。")
                    raise QuotaMachineMismatch("此额度凭据已绑定其他机器，请使用独立凭据。")
                data[key] = binding
            data.update(last_seen=time.time())
            if spend:
                data["totals"]["requests"] += 1
            self._save(db, row["id"], data)
            return {"credential_id": row["id"], "period_id": data["period_id"], "started_at": time.time()}

    def record(self, reservation, values=None, *, missing=False, sent=0, received=0):
        if not reservation:
            return
        increments = {key: 0 for key in COUNTERS}
        increments.update(sent_bytes=max(0, sent), received_bytes=max(0, received), missing_responses=int(missing))
        if values is not None:
            for key in ("input_tokens", "output_tokens", "total_tokens", "cached_input_tokens"):
                increments[key] = values.get(key, 0)
            cost = reference_cost(values)
            increments.update(responses=1, cost_nanos=cost or 0, unpriced_responses=int(cost is None))
        with self.connection() as db:
            row = db.execute("SELECT data FROM credentials WHERE id=?", (reservation["credential_id"],)).fetchone()
            if row is None:
                return
            data = json.loads(row[0])
            self._roll(data)
            for key, amount in increments.items():
                data["totals"][key] += amount
            if data["period_id"] == reservation["period_id"]:
                data["period_cost_nanos"] += increments["cost_nanos"]
            day = day_key()
            daily = data["daily"].setdefault(day, {key: 0 for key in COUNTERS})
            for key, amount in increments.items():
                daily[key] += amount
            # Keep a year of chart aggregates; lifetime totals never shrink.
            data["daily"] = dict(sorted(data["daily"].items())[-366:])
            self._save(db, reservation["credential_id"], data)

    @staticmethod
    def public(data):
        private = {"bound", "client_hash", "exit_client_hash", "quota_client_hash", "period_id"}
        data = {key: value for key, value in data.items() if key not in private}
        if isinstance(data.get("snapshot"), dict):
            data["snapshot"] = {key: value for key, value in data["snapshot"].items() if key not in private}
        if data.get("direction") == "out":
            start = day_key(week_start())
            data["week_cost_nanos"] = sum(v["cost_nanos"] for k, v in data["daily"].items() if k >= start)
            data["expired"] = data["expires_at"] is not None and time.time() >= data["expires_at"]
            data["exhausted"] = bool(data["budget_nanos"] and data["period_cost_nanos"] >= data["budget_nanos"])
            data["daily"] = dict(sorted(data["daily"].items())[-30:])
        return data

    def rows(self):
        with self.connection() as db:
            output = []
            for row in db.execute("SELECT id,data FROM credentials ORDER BY rowid DESC"):
                data = json.loads(row["data"])
                if data["direction"] == "out" and self._roll(data):
                    self._save(db, row["id"], data)
                output.append(self.public(data))
            return output

    def snapshot(self, reservation):
        with self.connection() as db:
            row = db.execute("SELECT data FROM credentials WHERE id=?", (reservation["credential_id"],)).fetchone()
            if not row:
                return {}
            data = json.loads(row[0])
            if self._roll(data):
                self._save(db, reservation["credential_id"], data)
            return self.public(data)

    def update(self, identifier, changes, *, reset=False, dry_run=False):
        with self.connection() as db:
            row = db.execute("SELECT data FROM credentials WHERE id=?", (identifier,)).fetchone()
            if not row:
                raise SharingError("只能修改本机借出的凭据。")
            data = json.loads(row[0])
            if data["direction"] == "in" and (reset or "label" not in changes or
                    set(changes) - {"label", "id", "action"}):
                raise SharingError("只能修改本机借出的凭据。")
            self._roll(data)
            if "label" in changes:
                name = str(changes["label"] or "").strip()[:80].strip()
                if name != data.get("label") and not data.get("internal"):
                    name = self._unique_label(db, data["kind"], name, identifier=identifier, default=None)
                data["label"] = name
            if "expires_at" in changes and changes["expires_at"] != data["expires_at"]:
                data["expires_at"] = expiration(changes["expires_at"])
            if "budget" in changes:
                limit = budget_nanos(changes["budget"], minimum=0)
                if data["kind"] == "quota" and limit != data["budget_nanos"]:
                    limit = budget_nanos(changes["budget"])
                data["budget_nanos"] = limit if data["kind"] == "quota" else 0
            for key in ("enabled", "weekly_reset"):
                if key in changes:
                    if type(changes[key]) is not bool:
                        raise SharingError("凭据设置无效。")
                    data[key] = changes[key]
            if reset:
                data.update(period_id=secrets.token_hex(8), period_started_at=time.time(), period_cost_nanos=0)
            if not dry_run:
                self._save(db, identifier, data)
            return self.public(data)

    def refresh_snapshot(self, identifier, snapshot):
        with self.connection() as db:
            row = db.execute("SELECT data FROM credentials WHERE id=? AND direction='in'", (identifier,)).fetchone()
            if row:
                data = json.loads(row[0])
                data.update(snapshot=snapshot, last_seen=time.time())
                self._save(db, identifier, data)

    def availability(self, identifier, online):
        with self.connection() as db:
            row = db.execute("SELECT data FROM credentials WHERE id=? AND direction='in'", (identifier,)).fetchone()
            if row:
                data = json.loads(row[0])
                data.update(online=online, checked_at=time.time())
                self._save(db, identifier, data)
