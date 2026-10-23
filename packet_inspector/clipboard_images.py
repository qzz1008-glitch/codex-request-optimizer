"""Keep relay-owned copies of Codex clipboard images in one temporary folder."""
from pathlib import Path
import re
import tempfile
import threading
import time
import uuid

from .locking import file_lock
from .request_context import IMAGE_PATH_ATTRIBUTE, local_image_path

CLIPBOARD_NAME = re.compile(r"codex-clipboard-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\.(?:png|jpe?g|webp|gif)", re.I)
STAGING_NAME = re.compile(r"\.clipboard-[0-9a-f]{32}\.new")


class ClipboardImageCache:
    def __init__(self, *, root=None, max_file_bytes=50 * 1024 * 1024,
                 max_bytes=256 * 1024 * 1024, ttl=7 * 24 * 3600):
        self.root = Path(root or tempfile.gettempdir()).resolve()
        self.directory = self.root / "CodexRequestOptimizer"
        self.max_file_bytes, self.max_bytes, self.ttl = max_file_bytes, max_bytes, ttl
        self.lock = threading.RLock()

    def destination(self, value):
        value = local_image_path(value)
        if not value:
            return None
        source = Path(value)
        if not CLIPBOARD_NAME.fullmatch(source.name) or source.parent.resolve() not in (self.root, self.directory):
            return None
        return self.directory / source.name

    @staticmethod
    def _regular(path):
        return not path.is_symlink() and not path.is_junction() and path.is_file()

    def _prune(self, protected):
        total, now = 0, time.time()
        for path in self.directory.iterdir():
            staging = STAGING_NAME.fullmatch(path.name)
            if not (staging or CLIPBOARD_NAME.fullmatch(path.name)) or not self._regular(path):
                continue
            info = path.stat()
            if path not in protected and now - info.st_mtime > (3600 if staging else self.ttl):
                path.unlink()
            else:
                total += info.st_size
        return total

    def prepare(self, paths):
        candidates = {}
        for value in paths[:32]:
            target = self.destination(value)
            if target:
                candidates[local_image_path(value)] = target
        protected, copied = set(candidates.values()), {}
        for value, target in candidates.items():
            staging = None
            try:
                source = Path(value)
                if self.directory.is_symlink() or self.directory.is_junction():
                    continue
                if not self._regular(source) and not self._regular(target):
                    continue
                self.directory.mkdir(parents=True, exist_ok=True)
                with self.lock, file_lock(self.directory / ".clipboard.lock"):
                    source_info = source.stat() if self._regular(source) else None
                    target_info = target.stat() if self._regular(target) else None
                    fresh = (source_info is None or source == target or
                             (source_info.st_size == target_info.st_size and source_info.st_mtime_ns <= target_info.st_mtime_ns)) if target_info else False
                    if fresh and 0 < target_info.st_size <= self.max_file_bytes:
                        target.touch()
                        self._prune(protected)
                        copied[value] = str(target)
                        continue
                    if target.is_symlink() or target.is_junction() or source_info is None or source == target:
                        continue
                    size = source_info.st_size
                    total = self._prune(protected) - (target_info.st_size if target_info else 0)
                    if not 0 < size <= self.max_file_bytes or total + size > self.max_bytes:
                        continue
                    with source.open("rb") as stream:
                        payload = stream.read(self.max_file_bytes + 1)
                    if not 0 < len(payload) <= self.max_file_bytes or total + len(payload) > self.max_bytes:
                        continue
                    staging = self.directory / (".clipboard-" + uuid.uuid4().hex + ".new")
                    with staging.open("xb") as stream:
                        stream.write(payload)
                    staging.replace(target)
                    copied[value] = str(target)
            except OSError:
                # A missing original or unwritable cache must not break an otherwise valid request.
                continue
            finally:
                if staging is not None:
                    try:
                        staging.unlink(missing_ok=True)
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
