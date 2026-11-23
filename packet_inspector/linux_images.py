"""Image configuration and synchronous staging for connected SSH and WSL Codex."""
from __future__ import annotations

import base64
from collections import OrderedDict
from dataclasses import dataclass
import hashlib
from html import escape
import json
from pathlib import Path, PurePosixPath
import re
import threading

import tomlkit

from .analyzer import JsonBytes, decode_body, encode
from .image_config import TOOL_SERVER, remove_legacy_image_setting
from .image_isolation import ImageIsolationFailure, decode_inline_image
from .linux_image_control import LINUX_IMAGE_DATA_DIR
from .providers import _get, _put
from .request_context import IMAGE_PATH_ATTRIBUTE


SETTINGS_BYTES = 512 * 1024
MAX_STAGE_BYTES = 64 * 1024 * 1024
NATIVE_NAMES = {"view_image", "functions.view_image"}
IMAGE_NAMES = re.compile(rb"/(i-[0-9a-f]{30}\.(?:png|jpg|webp|gif))\b")
MCP_TOOL = ["mcp_servers", TOOL_SERVER]


def linux_data_directories(values):
    if not isinstance(values, dict) or len(values) > 80:
        raise ValueError("Linux 数据目录设置无效。")
    result = {}
    for key, value in values.items():
        if not isinstance(key, str) or not 0 < len(key) <= 256 or not isinstance(value, str) or len(value) > 1024:
            raise ValueError("Linux 数据目录设置无效。")
        path = value.strip()
        if path and (not path.startswith(("/", "~/")) or "\\" in path or any(ord(char) < 32 for char in path)):
            raise ValueError("数据目录须为 Linux 绝对路径或以 ~/ 开头。")
        if path and path != LINUX_IMAGE_DATA_DIR:
            result[key] = path
    return result


@dataclass(frozen=True)
class LinuxImageTarget:
    directory: str
    send: object

    def store(self, entries, referenced):
        return self.send("image_store", data_dir=str(PurePosixPath(self.directory).parent),
                         entries=entries, referenced=referenced)


def probe_image_directory(send, data_dir):
    value = send("image_probe", data_dir=data_dir).get("directory", "")
    if not isinstance(value, str) or any(ord(char) < 32 for char in value):
        raise ValueError("Linux 未确认图片目录。")
    path = PurePosixPath(value)
    if not path.is_absolute() or path.name != "images":
        raise ValueError("Linux 未确认图片目录。")
    return str(path)


def linux_image_edits(raw, previous, mode, send):
    """Return a config edit and the owned fields needed for exact restoration."""
    document = tomlkit.parse((raw or b"").decode("utf-8-sig"))
    original = document.unwrap()
    for field in previous:
        present, value = _get(document, field["keys"])
        if present and value == field["applied"]:
            _put(document, field["keys"], field["before"], field["present"])
            for keys in reversed(field.get("added_tables", [])):
                _, table = _get(document, keys)
                if isinstance(table, dict) and not table and not table.as_string().strip():
                    _put(document, keys, None, False)

    fields = []

    def change(keys, value):
        present, before = _get(document, keys)
        if present and before == value:
            return
        fields.append({"keys": keys, "present": present, "before": before, "applied": value,
                       "added_tables": [keys[:i] for i in range(1, len(keys)) if not _get(document, keys[:i])[0]]})
        _put(document, keys, value)

    if mode != "native":
        if mode == "optimized" and _get(document, MCP_TOOL)[0]:
            raise ValueError("该设备已有独立图片工具配置，请先确认现有配置。")
        source = (Path(__file__).with_name("isolated_image_tool.py").read_text(encoding="utf-8")
                  if mode == "optimized" else "")
        registration = send("image_tool", mode=mode, source=source)
        if mode == "optimized":
            if not registration.get("command"):
                raise ValueError("Linux 未确认图片工具安装。")
            change(MCP_TOOL, registration)
        elif _get(document, MCP_TOOL)[0]:
            change([*MCP_TOOL, "enabled"], False)
        # Configurations from older cores may have the removed tools.view_image key.
        # Only our features and MCP keys are owned and restored here.
        remove_legacy_image_setting(document)
        change(["features", "view_image"], False)
        for name, profile in document.get("profiles", {}).items():
            if "view_image" in profile.get("features", {}):
                change(["profiles", name, "features", "view_image"], False)
    uninstall = not _get(document, MCP_TOOL)[0] and any(field["keys"] == MCP_TOOL for field in previous)
    if document.unwrap() == original:
        return None, fields, uninstall
    bom = b"\xef\xbb\xbf" if (raw or b"").startswith(b"\xef\xbb\xbf") else b""
    return bom + tomlkit.dumps(document).encode("utf-8"), fields, uninstall


class LinuxImageTransfer:
    def __init__(self):
        self._known = OrderedDict()
        self._lock = threading.Lock()

    def _stage(self, target, scope, payloads, referenced):
        directory = target.directory
        with self._lock:
            known = {name for name in payloads if (scope, directory, name) in self._known}

        def manifest(names, include):
            return {name: {"size": len(payloads[name]), **(
                {"data": base64.b64encode(payloads[name]).decode("ascii")} if name in include else {})}
                    for name in names}

        def store(entries):
            result = target.store(entries, referenced)
            if result.get("directory") != directory or not isinstance(result.get("confirmed"), list):
                raise ValueError("Linux 未确认图片上传结果。")
            return {name for name in result["confirmed"] if isinstance(name, str) and name in entries}

        confirmed = store(manifest(payloads, payloads.keys() - known))
        missing = payloads.keys() - confirmed
        if missing and store(manifest(missing, missing)) != missing:
            raise ValueError("Linux 未确认所有图片已写入。")
        with self._lock:
            for name in payloads:
                key = scope, directory, name
                self._known[key] = None
                self._known.move_to_end(key)
            while len(self._known) > 4096:
                self._known.popitem(last=False)

    def rewrite(self, raw, headers, target, scope):
        encoding = headers.get("Content-Encoding", headers.get("content-encoding", "identity"))
        decoded = decode_body(raw, encoding)
        if b'"input_image"' not in decoded:
            return None
        request = json.loads(decoded)
        if not isinstance(request, dict) or not isinstance(request.get("input"), list):
            return None
        inputs = request["input"]
        catalogs = [request.get("tools"), *(item.get("tools") for item in inputs if isinstance(item, dict))]
        if any(native_image_advertised(catalog) for catalog in catalogs):
            return None  # Existing Codex process still advertises the native reader.
        if native_image_advertised([request.get("tool_choice")]):
            return None
        readers = {item.get("call_id") for item in inputs if isinstance(item, dict)
                   and item.get("name") in NATIVE_NAMES and isinstance(item.get("call_id"), str)}
        payloads, attachments, texts = {}, {}, {}
        directory = PurePosixPath(target.directory)
        for number, item in enumerate(inputs):
            if not isinstance(item, dict):
                continue
            if item.get("role") == "user":
                field = "content"
            elif (item.get("type") in {"function_call_output", "custom_tool_call_output"}
                  and item.get("call_id") not in readers):
                field = "output"
            else:
                continue
            parts = item.get(field)
            if not isinstance(parts, list):
                continue
            for index, part in enumerate(parts):
                if not isinstance(part, dict) or part.get("type") != "input_image" or part.get("file_id"):
                    continue
                try:
                    payload, extension = decode_inline_image(part.get("image_url"))
                except ImageIsolationFailure:
                    continue
                name = "i-" + hashlib.sha256(payload).hexdigest()[:30] + extension
                payloads.setdefault(name, payload)
                pointer = f"/input/{number}/{field}"
                path = str(directory / name)
                attachments[f"{pointer}/{index}"] = path
                if field == "content" and index and isinstance(parts[index - 1], dict):
                    previous = parts[index - 1].get("text")
                    if isinstance(previous, str):
                        tags = list(IMAGE_PATH_ATTRIBUTE.finditer(previous))
                        if len(tags) == 1:
                            a, b = tags[0].span(1)
                            texts[f"{pointer}/{index - 1}/text"] = previous[:a] + escape(path, quote=True) + previous[b:]
        if not attachments:
            return None
        if sum(map(len, payloads.values())) > MAX_STAGE_BYTES:
            raise ValueError("此请求的图片超过单次 Linux 上传上限。")
        spans = JsonBytes(decoded).spans
        values = {**texts, **{pointer: {"type": "input_text", "text": path} for pointer, path in attachments.items()}}
        replacements = sorted((spans[pointer][0], spans[pointer][1], encode(value))
                              for pointer, value in values.items())
        pieces, cursor = [], 0
        for start, end, replacement in replacements:
            pieces.extend((decoded[cursor:start], replacement))
            cursor = end
        changed = b"".join((*pieces, decoded[cursor:]))
        self._stage(target, scope, payloads, sorted({value.decode() for value in IMAGE_NAMES.findall(decoded)}))
        return changed, {"image_isolation_applied": True, "image_isolation_removed": len(attachments),
                         "image_paths": sorted(set(attachments.values())),
                         "image_isolation_decoded_before": len(decoded), "image_isolation_decoded_after": len(changed)}


def native_image_advertised(tools):
    return isinstance(tools, list) and any(isinstance(tool, dict) and (
        tool.get("name") in NATIVE_NAMES
        or isinstance(tool.get("function"), dict) and tool["function"].get("name") in NATIVE_NAMES
        or native_image_advertised(tool.get("tools"))) for tool in tools)
