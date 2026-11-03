"""Roll over opted-in native image conversations before an oversized upload."""
import json
from urllib.parse import urlsplit

from .analyzer import decode_body
from .image_isolation import ImageIsolationFailure
from .request_context import request_metadata_candidates
from .transport import AUTO_ZSTD_HIGH_LEVEL, prepare_zstd

IMAGE_SESSION_BYTES = 1_000_000
IMAGE_SESSION_LIMIT = "image_session_size_limit"


def prepare_image_session(body, headers, method, path, prepared, compression):
    if (len(prepared.body) <= IMAGE_SESSION_BYTES or method != "POST"
            or not urlsplit(path).path.rstrip("/").endswith(("/responses", "/responses/compact"))):
        return prepared
    try:
        value = json.loads(decode_body(body, next((v for k, v in headers.items()
                                                 if k.lower() == "content-encoding"), "identity")))
    except (ValueError, TypeError):
        return prepared
    reusable = any(meta.get("codex_inspector_request_kind") == "image_analysis"
                   and meta.get("codex_inspector_image_session") == "native"
                   for meta in request_metadata_candidates(value, headers))
    if not reusable:
        return prepared
    if compression == "zstd" and prepared.encoding in {"identity", "", "zstd"}:
        prepared = prepare_zstd(prepared.body, prepared.encoding, AUTO_ZSTD_HIGH_LEVEL)
    if len(prepared.body) > IMAGE_SESSION_BYTES:
        raise ImageIsolationFailure(IMAGE_SESSION_LIMIT,
            IMAGE_SESSION_LIMIT + ": image conversation exceeds 1,000,000 upload bytes; start a new image conversation. Original request was not sent.", 413)
    return prepared
