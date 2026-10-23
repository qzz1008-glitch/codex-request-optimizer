"""Move inline user images out of the upstream main request, never into its text."""
import base64
import binascii
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
import uuid

from .analyzer import JsonBytes, decode_body, encode
from .overflow import encode_changed_info
from .locking import file_lock

MAX_IMAGE_BYTES = 50 * 1024 * 1024
MAX_ATTACHMENTS_BYTES = 256 * 1024 * 1024
ATTACHMENT_TTL = 7 * 24 * 3600
MIME_EXTENSIONS = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}


def resolve_image_directory(data_dir, value=""):
    if not isinstance(value, str) or "\x00" in value:
        raise ValueError("图片保存路径必须是绝对文件夹路径。")
    if not value.strip():
        return (Path(data_dir) / "image-attachments").resolve()
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
        self.lock_path = Path(directory) / "image-attachments.lock"
        self.lock = threading.RLock()

    def _store(self, image_url, protected):
        if not isinstance(image_url, str):
            raise ImageIsolationFailure("image_reference_unsupported", "图片缺少可保存的内嵌内容；请提供本地图片文件。")
        match = re.fullmatch(r"data:(image/(?:png|jpeg|webp|gif));base64,([A-Za-z0-9+/=\r\n]+)", image_url)
        if not match:
            raise ImageIsolationFailure("image_reference_unsupported", "图片隔离只接受内嵌图片；远程 URL 和文件 ID 未转发。")
        if len(match[2]) > (MAX_IMAGE_BYTES + 2) // 3 * 4 + 4096:
            raise ImageIsolationFailure("image_too_large", "图片超过独立解析的 50 MiB 限制。", 413)
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
        name = hashlib.sha256(payload).hexdigest() + MIME_EXTENSIONS[match[1]]
        with self.lock, file_lock(self.lock_path):
            self.directory.mkdir(parents=True, exist_ok=True)
            target = self.directory / name
            if target.is_symlink():
                raise ImageIsolationFailure("image_attachment_conflict", "本地附件路径异常。")
            if target.exists():
                target.touch()
                protected.add(target)
                return target.resolve()
            total = 0
            for path in self.directory.iterdir():
                if path.suffix not in MIME_EXTENSIONS.values() or not re.fullmatch(r"[a-f0-9]{64}\.(png|jpg|webp|gif)", path.name):
                    continue
                if path.is_symlink() or not path.is_file():
                    continue
                info = path.stat()
                if path not in protected and time.time() - info.st_mtime > ATTACHMENT_TTL:
                    path.unlink()
                else:
                    total += info.st_size
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
        replacements, protected = [], set()
        index = None
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
                if not isinstance(item, dict) or item.get("role") != "user" or not isinstance(item.get("content"), list):
                    continue
                for j, part in enumerate(item["content"]):
                    if not isinstance(part, dict) or part.get("type") != "input_image":
                        continue
                    if part.get("file_id"):
                        raise ImageIsolationFailure("image_reference_unsupported", "图片文件 ID 无法转换成本地路径，原图未转发。")
                    image_path = self._store(part.get("image_url"), protected)
                    text = str(image_path)
                    index = index or JsonBytes(decoded)
                    a, b = index.spans[f"/input/{i}/content/{j}"]
                    replacements.append((a, b, encode({"type": "input_text", "text": text})))
                    images_removed += 1
                    # Mark only the local parsed copy, to check for unsupported image locations.
                    item["content"][j] = {"type": "input_text", "text": text}
        def has_image(value):
            if isinstance(value, dict):
                return value.get("type") == "input_image" or any(has_image(v) for v in value.values())
            return isinstance(value, list) and any(has_image(v) for v in value)
        if has_image(inputs):
            raise ImageIsolationFailure("image_location_unsupported", "当前图片位置不能安全替换，原图未转发。")
        if not replacements:
            return None
        changed = decoded
        for start, end, replacement in sorted(replacements, reverse=True):
            changed = changed[:start] + replacement + changed[end:]
        prepared = encode_changed_info(changed, headers, path, compression, zstd_level,
                                       auto_zstd_level=auto_zstd_level, budget_bytes=budget_bytes)
        return prepared.body, prepared.encoding, {"image_isolation_applied": True, "image_isolation_removed": images_removed,
                                "relay_zstd_level": prepared.zstd_level,
                                "native_image_tools_hidden": hidden_tools,
                                "image_paths": sorted(str(path.resolve()) for path in protected),
                                "image_isolation_decoded_before": len(decoded),
                                "image_isolation_decoded_after": len(changed)}
