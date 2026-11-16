"""Resolve clipboard references into the single shared image attachment cache."""
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
import uuid
import weakref

from .locking import file_lock
from .request_context import IMAGE_PATH_ATTRIBUTE, local_image_path

CLIPBOARD_NAME = re.compile(r"codex-clipboard-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\.(?:png|jpe?g|webp|gif)", re.I)
ATTACHMENT_NAME = re.compile(r"(?:[a-f0-9]{64}|[a-f0-9]{32}|i-[a-f0-9]{30}|i-[a-f0-9]{1,28}-[1-9][0-9]*)\.(?:png|jpe?g|webp|gif)", re.I)
STAGING_NAME = re.compile(r"(?:\.clipboard-)?[0-9a-f]{32}\.new")
MAX_ALIASES = 4096
MAX_ALIAS_BYTES = 4 * 1024 * 1024
_LOCKS = weakref.WeakValueDictionary()
_LOCKS_GUARD = threading.Lock()


class ClipboardImageCache:
    def __init__(self, directory, store, *, root=None, legacy_directories=(),
                 max_file_bytes=50 * 1024 * 1024, ttl=7 * 24 * 3600):
        self.root = Path(root or tempfile.gettempdir()).resolve()
        self.directory, self.store = Path(directory).resolve(), store
        self.previous_directories = {Path(path).resolve() for path in legacy_directories}
        self.aliases_path = self.directory / ".clipboard-paths.json"
        with _LOCKS_GUARD:
            self.lock = _LOCKS.setdefault(os.path.normcase(str(self.directory)), threading.RLock())
        self.max_file_bytes, self.ttl = max_file_bytes, ttl

    @staticmethod
    def _regular(path):
        return not path.is_symlink() and not path.is_junction() and path.is_file()

    def _eligible(self, path):
        parent = path.parent.resolve()
        if parent == self.directory or parent in self.previous_directories:
            return bool(ATTACHMENT_NAME.fullmatch(path.name))
        return self.is_clipboard_source(path)

    def is_clipboard_source(self, path):
        return path.parent.resolve() == self.root and bool(CLIPBOARD_NAME.fullmatch(path.name))

    def _retained(self, path):
        parent = path.parent.resolve()
        if parent in {self.directory, *self.previous_directories}:
            return path if self._regular(path) else None
        return None

    def _cached(self, record):
        name = record.get("name", "")
        if not isinstance(name, str) or not (ATTACHMENT_NAME.fullmatch(name) or CLIPBOARD_NAME.fullmatch(name)):
            return None
        path = Path(record["path"]) if isinstance(record.get("path"), str) else self.directory / name
        parents = {self.directory, *self.previous_directories}
        return path if path.name == name and path.parent.resolve() in parents else None

    def _load(self):
        try:
            if self.aliases_path.stat().st_size <= MAX_ALIAS_BYTES:
                result = json.loads(self.aliases_path.read_text(encoding="utf8"))
                if isinstance(result, dict):
                    return {key: value for key, value in result.items()
                            if isinstance(value, dict) and isinstance(value.get("used"), (int, float))
                            and self._cached(value) is not None}
        except (OSError, ValueError):
            pass
        return {}

    def _save(self, aliases):
        now = time.time()
        ordered = sorted(((key, value) for key, value in aliases.items()
                          if now - value["used"] <= self.ttl), key=lambda row: row[1]["used"], reverse=True)
        retained, size = {}, 2
        for key, value in ordered[:MAX_ALIASES]:
            size += len(json.dumps({key: value}, ensure_ascii=False).encode("utf8")) + 2
            if size > MAX_ALIAS_BYTES:
                break
            retained[key] = value
        staging = self.directory / (uuid.uuid4().hex + ".new")
        try:
            staging.write_text(json.dumps(retained, ensure_ascii=False), encoding="utf8")
            staging.replace(self.aliases_path)
        finally:
            staging.unlink(missing_ok=True)

    def retained_paths(self, paths):
        """Protect existing context paths before an inline image can trigger pruning."""
        if not paths:
            return set()
        aliases, retained = self._load(), set()
        for value in paths[:32]:
            value = local_image_path(value)
            if not value:
                continue
            try:
                path = Path(value)
                if not self._eligible(path):
                    continue
                key = os.path.normcase(str(path.resolve()))
                for target in (self._retained(path), self._cached(aliases.get(key, {}))):
                    if target is not None and self._regular(target):
                        retained.add(target)
            except OSError:
                continue
        return retained

    def prepare(self, paths, *, protected=None, bindings=None):
        candidates = {}
        for value in paths[:32]:
            value = local_image_path(value)
            if value and self._eligible(Path(value)):
                candidates[value] = Path(value)
        if not candidates or self.directory.is_symlink() or self.directory.is_junction():
            return {}
        protected = protected if protected is not None else set()
        pinned = set(protected)
        bindings = bindings or {}
        copied = {}
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            with self.lock, file_lock(self.directory / ".clipboard-paths.lock"):
                aliases = self._load()
                # Pin all retained references before a store call can prune them.
                for value, path in candidates.items():
                    key = os.path.normcase(str(path.resolve()))
                    record = aliases.get(key, {})
                    bound = self._retained(Path(bindings[value])) if value in bindings else None
                    cached = bound or self._cached(record) or self._retained(path)
                    if cached is not None and self._regular(cached):
                        if cached.parent.resolve() == self.directory:
                            cached.touch()
                        pinned.add(cached)
                for value, path in candidates.items():
                    try:
                        key = os.path.normcase(str(path.resolve()))
                        record = aliases.get(key, {})
                        cached = self._cached(record)
                        bound = self._retained(Path(bindings[value])) if value in bindings else None
                        retained = bound or self._retained(path)
                        direct = path.parent.resolve() in {self.directory, *self.previous_directories}
                        source = path if self._regular(path) else None
                        info = source.stat() if source else None
                        fresh = (source is None or direct or
                                 (record.get("size") == info.st_size and record.get("mtime") == info.st_mtime_ns))
                        if retained is not None:
                            cached, fresh = retained, True
                        if fresh and cached is not None and self._regular(cached) and 0 < cached.stat().st_size <= self.max_file_bytes:
                            target = cached
                        else:
                            if source is None:
                                continue
                            info = source.stat()
                            if not 0 < info.st_size <= self.max_file_bytes:
                                continue
                            with source.open("rb") as stream:
                                payload = stream.read(self.max_file_bytes + 1)
                            if not 0 < len(payload) <= self.max_file_bytes:
                                continue
                            extension = ".jpg" if source.suffix.lower() == ".jpeg" else source.suffix.lower()
                            target = self.store(payload, extension, pinned)
                            if target is None:
                                continue
                        target.touch()
                        protected.add(target)
                        pinned.add(target)
                        copied[value] = str(target)
                        if not direct:
                            aliases[key] = {"name": target.name, "path": str(target), "used": time.time(),
                                            "size": info.st_size if info else record.get("size"),
                                            "mtime": info.st_mtime_ns if info else record.get("mtime")}
                    except OSError:
                        # Path-only references can still use their original if copying fails.
                        continue
                self._save(aliases)
        except OSError:
            pass
        return copied


def rewrite_clipboard_text(text, copied):
    original = local_image_path(text)
    if original in copied:
        start = len(text) - len(text.lstrip())
        end = len(text.rstrip())
        return text[:start] + copied[original] + text[end:]

    def replace(match):
        target = copied.get(local_image_path(match[1]))
        if target is None:
            return match[0]
        start, end = match.span(1)
        return match[0][:start - match.start()] + target + match[0][end - match.start():]

    return IMAGE_PATH_ATTRIBUTE.sub(replace, text)
