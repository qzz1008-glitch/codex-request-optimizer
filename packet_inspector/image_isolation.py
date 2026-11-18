"""Replace inline request images with paths in the upstream main request."""
import base64
import binascii
import hashlib
from itertools import chain
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
import uuid

from .analyzer import JsonBytes, decode_body, encode
from .clipboard_images import ATTACHMENT_NAME, STAGING_NAME, ClipboardImageCache, rewrite_clipboard_text
from .overflow import encode_changed_info
from .locking import file_lock
from .request_context import IMAGE_PATH_ATTRIBUTE, local_image_path, request_image_paths, temporary_request_kind


def is_analysis_request(raw, headers):
    """Recognize our independent tool's explicit prompt, without provider identity."""
    try:
        encoding = headers.get("Content-Encoding", headers.get("content-encoding", "identity"))
        return temporary_request_kind(json.loads(decode_body(raw, encoding))) == "image_analysis"
    except Exception:
        return False  # Forwarding/preparation owns malformed-body handling.

MAX_IMAGE_BYTES = 50 * 1024 * 1024
MAX_ATTACHMENTS_BYTES = 256 * 1024 * 1024
ATTACHMENT_TTL = 7 * 24 * 3600
CLEANUP_INTERVAL = 600
MIME_EXTENSIONS = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}


def resolve_image_directory(data_dir, value=""):
    if not isinstance(value, str) or "\x00" in value:
        raise ValueError("图片保存路径必须是绝对文件夹路径。")
    if not value.strip():
        return (Path(data_dir) / "images").resolve()
    try:
        directory = Path(os.path.expandvars(value.strip())).expanduser()
        if not directory.is_absolute():
            raise ValueError("图片保存路径必须是绝对文件夹路径。")
        return directory.resolve()
    except (OSError, RuntimeError):
        raise ValueError("无法访问图片保存路径，请检查文件夹路径。") from None


def check_image_directory(directory):
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(prefix=".codex-image-check-", dir=directory):
            pass
    except OSError:
        raise ValueError("图片保存路径必须是可写的文件夹。") from None


class ImageIsolationFailure(Exception):
    def __init__(self, code, message, status=422):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


class ImageIsolation:
    def __init__(self, directory, attachment_dir=""):
        self.directory = resolve_image_directory(directory, attachment_dir)
        self.previous_directory = (Path(directory) / "image-attachments").resolve()
        self.lock_path = Path(directory) / "image-attachments.lock"
        self.lock = threading.RLock()
        self.active_references = {}
        self.next_cleanup = 0.0
        self.last_cleanup = {"state": "not_run", "expired": 0, "reclaimed_bytes": 0}
        self.clipboard = ClipboardImageCache(self.directory, self._copy_payload,
                                            legacy_directories=[self.previous_directory],
                                            max_file_bytes=MAX_IMAGE_BYTES,
                                            ttl=ATTACHMENT_TTL)

    def _prune(self, protected=(), *, stop=None):
        """Caller holds the storage locks; only owned files in the two cache roots qualify."""
        pinned = set(protected)
        for references in self.active_references.values():
            pinned.update(references.copy())
        result = {"state": "checked", "expired": 0, "reclaimed_bytes": 0, "bytes": 0, "errors": 0}
        now = time.time()
        for directory in dict.fromkeys((self.directory, self.previous_directory)):
            if directory.is_symlink() or getattr(directory, "is_junction", lambda: False)() or not directory.is_dir():
                continue
            try:
                entries = os.scandir(directory)
            except OSError:
                result["errors"] += 1
                continue
            with entries:
                for entry in entries:
                    if stop and stop():
                        return {**result, "state": "deferred"}
                    path = Path(entry.path)
                    staging = STAGING_NAME.fullmatch(path.name)
                    if not (staging or ATTACHMENT_NAME.fullmatch(path.name)):
                        continue
                    try:
                        if not self.clipboard._regular(path):
                            continue
                        info = path.stat()
                        if path not in pinned and now - info.st_mtime > (3600 if staging else ATTACHMENT_TTL):
                            if stop and stop():
                                return {**result, "state": "deferred"}
                            try:
                                path.unlink()
                            except FileNotFoundError:
                                continue
                            except OSError:
                                result["errors"] += 1
                                result["bytes"] += info.st_size
                            else:
                                result["expired"] += 1
                                result["reclaimed_bytes"] += info.st_size
                        else:
                            result["bytes"] += info.st_size
                    except OSError:
                        result["errors"] += 1
        return result

    def maintenance(self, busy, *, force=False):
        now = time.monotonic()
        if not force and now < self.next_cleanup:
            return self.last_cleanup
        if busy.is_set() or not self.lock.acquire(blocking=False):
            return self.last_cleanup
        try:
            if busy.is_set():
                return self.last_cleanup
            if not any(path.is_dir() for path in (self.directory, self.previous_directory)):
                self.last_cleanup = {"state": "absent", "expired": 0, "reclaimed_bytes": 0, "bytes": 0}
                self.next_cleanup = now + CLEANUP_INTERVAL
                return self.last_cleanup
            with file_lock(self.lock_path, blocking=False):
                self.last_cleanup = self._prune(stop=busy.is_set)
            self.next_cleanup = now + (5 if self.last_cleanup["state"] == "deferred" else CLEANUP_INTERVAL)
        except BlockingIOError:
            self.next_cleanup = now + 5
        except OSError as error:
            self.last_cleanup = {"state": "error", "expired": 0, "reclaimed_bytes": 0,
                                 "error": type(error).__name__, "errno": error.errno}
            self.next_cleanup = now + 60
        finally:
            self.lock.release()
        if not busy.is_set():
            self.clipboard.prune()
        return self.last_cleanup

    def cache_status(self):
        return {"ttl_seconds": ATTACHMENT_TTL, "max_bytes": MAX_ATTACHMENTS_BYTES,
                "cleanup_interval_seconds": CLEANUP_INTERVAL, **self.last_cleanup}

    def refresh_references(self, paths):
        if not paths:
            return set()
        with self.lock, file_lock(self.lock_path):
            references = self.clipboard.retained_paths(paths)
            for path in references:
                try:
                    os.utime(path, None)
                except OSError:
                    continue
            return references

    def observe_request(self, raw, headers):
        """Independent image analysis also refreshes retained sources, with capture disabled too."""
        try:
            coding = next((v for k, v in headers.items() if k.lower() == "content-encoding"), "identity")
            body = json.loads(decode_body(raw, coding))
            if isinstance(body, dict):
                self.refresh_references(request_image_paths(body))
        except Exception:
            pass  # Optional usage observation must not change the forwarding outcome.

    def _copy_payload(self, payload, extension, protected):
        try:
            return self._store_payload(payload, extension, protected)
        except ImageIsolationFailure as error:
            if error.code != "image_attachment_storage_full":
                raise
            return None

    def _store(self, image_url, protected):
        if not isinstance(image_url, str):
            raise ImageIsolationFailure("image_reference_unsupported", "图片缺少可保存的内嵌内容；请提供本地图片文件。")
        match = re.fullmatch(r"data:(image/(?:png|jpeg|webp|gif));base64,([A-Za-z0-9+/=\r\n]+)", image_url)
        if not match:
            raise ImageIsolationFailure("image_reference_unsupported", "图片隔离只接受内嵌图片；远程 URL 和文件 ID 未转发。")
        if len(match[2]) > (MAX_IMAGE_BYTES + 2) // 3 * 4 + 4096:
            raise ImageIsolationFailure("image_too_large", f"图片超过独立解析的 {MAX_IMAGE_BYTES / 1000000:g} MB 限制。", 413)
        try:
            payload = base64.b64decode(match[2].replace("\r", "").replace("\n", ""), validate=True)
        except (ValueError, binascii.Error):
            raise ImageIsolationFailure("image_invalid_data", "图片编码无效，未上传原始图片。") from None
        if not payload or len(payload) > MAX_IMAGE_BYTES:
            raise ImageIsolationFailure("image_too_large", "图片为空或超过独立解析的大小限制。", 413)
        signature_ok = {"image/png": payload.startswith(b"\x89PNG\r\n\x1a\n"),
                        "image/jpeg": payload.startswith(b"\xff\xd8\xff"),
                        "image/gif": payload.startswith((b"GIF87a", b"GIF89a")),
                        "image/webp": payload[:4] == b"RIFF" and payload[8:12] == b"WEBP"}
        if not signature_ok[match[1]]:
            raise ImageIsolationFailure("image_invalid_data", "图片格式与声明不一致，未上传原始图片。")
        return self._store_payload(payload, MIME_EXTENSIONS[match[1]], protected)

    def _store_payload(self, payload, extension, protected):
        digest = hashlib.sha256(payload).hexdigest()
        stem = "i-" + digest[:30]
        with self.lock, file_lock(self.lock_path):
            if self.previous_directory != self.directory and self.previous_directory.is_dir():
                # Existing contexts must keep their original paths and cache prefixes.
                candidates = chain((self.previous_directory / (stem + extension),
                                    self.previous_directory / (digest + extension)),
                                   self.previous_directory.glob("i-" + digest[:24] + "*" + extension))
                for existing in candidates:
                    if self.clipboard._regular(existing) and existing.stat().st_size == len(payload):
                        with existing.open("rb") as stream:
                            if hashlib.file_digest(stream, "sha256").hexdigest() == digest:
                                existing.touch()
                                protected.add(existing)
                                return existing.resolve()
            self.directory.mkdir(parents=True, exist_ok=True)
            sequence = 0
            while True:
                suffix = "-" + str(sequence) if sequence else ""
                target = self.directory / (stem[:32 - len(suffix)] + suffix + extension)
                if (target.is_symlink() or getattr(target, "is_junction", lambda: False)()
                        or target.exists() and not target.is_file()):
                    raise ImageIsolationFailure("image_attachment_conflict", "本地附件路径异常。")
                if not target.exists():
                    break
                if target.stat().st_size == len(payload):
                    with target.open("rb") as stream:
                        same = hashlib.file_digest(stream, "sha256").hexdigest() == digest
                    if same:
                        target.touch()
                        protected.add(target)
                        return target.resolve()
                # A short-name collision must never substitute another image.
                sequence += 1
            total = self._prune(protected)["bytes"]
            if total + len(payload) > MAX_ATTACHMENTS_BYTES:
                raise ImageIsolationFailure("image_attachment_storage_full", "图片附件缓存已满，未上传原始图片。", 507)
            staging = self.directory / (uuid.uuid4().hex + ".new")
            try:
                staging.write_bytes(payload)
                staging.replace(target)
            finally:
                staging.unlink(missing_ok=True)
            protected.add(target)
            return target.resolve()

    def prepare(self, raw, headers, path, compression, *, zstd_level=3,
                auto_zstd_level=False, budget_bytes=None):
        rewritten = self.rewrite(raw, headers)
        if rewritten is None:
            return None
        changed, meta = rewritten
        prepared = encode_changed_info(changed, headers, path, compression, zstd_level,
                                       auto_zstd_level=auto_zstd_level, budget_bytes=budget_bytes)
        return prepared.body, prepared.encoding, dict(meta, relay_zstd_level=prepared.zstd_level)

    def rewrite(self, raw, headers):
        """Return the rewritten plaintext and metrics before transport encoding."""
        protected = set()
        with self.lock:
            self.active_references[id(protected)] = protected
        try:
            return self._rewrite(raw, headers, protected=protected)
        finally:
            with self.lock:
                self.active_references.pop(id(protected))

    def _rewrite(self, raw, headers, *, protected):
        coding = next((v for k, v in headers.items() if k.lower() == "content-encoding"), "identity")
        try:
            decoded = decode_body(raw, coding)
            body = json.loads(decoded)
        except Exception:
            raise ImageIsolationFailure("image_request_invalid", "无法检查图片请求，原请求未上传。", 400) from None
        if not isinstance(body, dict):
            raise ImageIsolationFailure("image_request_invalid", "图片隔离需要 Responses JSON 请求。", 400)
        if body.get("previous_response_id") or body.get("conversation"):
            raise ImageIsolationFailure("image_server_context_unsupported", "服务器续接上下文可能已含图片；请使用完整上下文的新请求。")
        inputs = body.get("input", [])
        replacements, image_paths = [], set()
        image_references, bindings, texts = request_image_paths(body), {}, []
        with self.lock:
            protected.update(self.refresh_references(image_references))
        index = None

        def rewrite_text(text, pointer):
            nonlocal index
            if not isinstance(text, str):
                return
            changed = rewrite_clipboard_text(text, copied)
            if changed != text:
                index = index or JsonBytes(decoded)
                a, b = index.spans[pointer]
                replacements.append((a, b, encode(changed)))

        hidden_tools = 0
        def filter_tools(tools):
            nonlocal hidden_tools
            if not isinstance(tools, list):
                return tools
            filtered = []
            for tool in tools:
                if not isinstance(tool, dict):
                    filtered.append(tool)
                    continue
                function = tool.get("function", {})
                name = tool.get("name", function.get("name") if isinstance(function, dict) else "")
                if name in {"view_image", "functions.view_image"}:
                    hidden_tools += 1
                    continue
                updated = dict(tool)
                if "tools" in updated:
                    updated["tools"] = filter_tools(updated["tools"])
                    if updated.get("type") == "namespace" and not updated["tools"]:
                        continue
                filtered.append(updated)
            return filtered
        catalogs = [("/tools", body["tools"])] if isinstance(body.get("tools"), list) else []
        if isinstance(inputs, list):
            catalogs.extend((f"/input/{i}/tools", item["tools"]) for i, item in enumerate(inputs)
                if isinstance(item, dict) and item.get("type") in {"additional_tools", "tool_definition"}
                and isinstance(item.get("tools"), list))
        for pointer, tools in catalogs:
            filtered = filter_tools(tools)
            if filtered != tools:
                index = index or JsonBytes(decoded)
                a, b = index.spans[pointer]
                replacements.append((a, b, encode(filtered)))
        choice = body.get("tool_choice")
        if hidden_tools and isinstance(choice, dict) and choice.get("name") in {"view_image", "functions.view_image"}:
            raise ImageIsolationFailure("image_native_tool_forced", "当前请求强制调用原生看图工具，请重新加载 Codex 配置。")
        images_removed = 0
        if isinstance(inputs, list):
            for i, item in enumerate(inputs):
                if not isinstance(item, dict):
                    continue
                if item.get("role") == "user":
                    field = "content"
                elif item.get("type") in {"function_call_output", "custom_tool_call_output"}:
                    field = "output"
                else:
                    continue
                pointer = f"/input/{i}/{field}"
                parts = item.get(field)
                if isinstance(parts, str):
                    if field == "content":
                        texts.append((parts, pointer))
                    continue
                if not isinstance(parts, list):
                    continue
                for j, part in enumerate(parts):
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") != "input_image":
                        if field == "content":
                            texts.append((part.get("text"), f"{pointer}/{j}/text"))
                        continue
                    if part.get("file_id"):
                        raise ImageIsolationFailure("image_reference_unsupported", "图片文件 ID 无法转换成本地路径，原图未转发。")
                    image_path = self._store(part.get("image_url"), protected)
                    image_paths.add(image_path)
                    if field == "content" and j and isinstance(parts[j - 1], dict):
                        preceding = parts[j - 1].get("text")
                        tags = list(IMAGE_PATH_ATTRIBUTE.finditer(preceding)) if isinstance(preceding, str) else []
                        if len(tags) == 1:
                            source = local_image_path(tags[0][1])
                            if source and self.clipboard.is_clipboard_source(Path(source)):
                                bindings[source] = str(image_path)
                    text = str(image_path)
                    index = index or JsonBytes(decoded)
                    a, b = index.spans[f"{pointer}/{j}"]
                    replacements.append((a, b, encode({"type": "input_text", "text": text})))
                    images_removed += 1
                    # Mark only the local parsed copy, to check for unsupported image locations.
                    parts[j] = {"type": "input_text", "text": text}
        def has_image(value):
            if isinstance(value, dict):
                return value.get("type") == "input_image" or any(has_image(v) for v in value.values())
            return isinstance(value, list) and any(has_image(v) for v in value)
        if has_image(inputs):
            raise ImageIsolationFailure("image_location_unsupported", "当前图片位置不能安全替换，原图未转发。")
        copied = self.clipboard.prepare(image_references, protected=protected, bindings=bindings)
        image_paths.update(Path(target) for target in copied.values())
        for text, pointer in texts:
            rewrite_text(text, pointer)
        if not replacements:
            return None
        changed = decoded
        for start, end, replacement in sorted(replacements, reverse=True):
            changed = changed[:start] + replacement + changed[end:]
        return changed, {"image_isolation_applied": True, "image_isolation_removed": images_removed,
                         "native_image_tools_hidden": hidden_tools,
                         "image_paths": sorted(str(path.resolve()) for path in image_paths),
                         "image_path_rewrites": {source: target for source, target in copied.items() if source != target},
                         "image_isolation_decoded_before": len(decoded),
                         "image_isolation_decoded_after": len(changed)}
