"""Model-facing sharing errors; older peer status APIs keep their flat codes."""
import io
import json

from .native_retry import PrefixResponse


LEGACY_ERRORS = {
    "sharing_unavailable": "共享已停止、到期或凭据已撤销，请联系提供方或切回本机额度。",
    "sharing_relay_unreachable": "提供方中转连接失败，请检查双方网络或切回本机额度。",
    "quota_credential_machine_mismatch": "此额度凭据已绑定其他机器，请使用独立凭据。",
}
MAX_ERROR_BYTES = 8192


def error_payload(code, message):
    return {"error": {"code": code, "message": message}}


def prepare_shared_error(response):
    """Normalize only recognized, bounded legacy errors on a borrowed route."""
    kind = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    encoding = response.headers.get("Content-Encoding", "identity").strip().lower()
    if response.status not in {403, 502, 503} or encoding not in {"", "identity"} or kind != "application/json":
        return response
    try:
        prefix = response.read(MAX_ERROR_BYTES + 1)
    except BaseException:
        response.close()
        raise
    prepared = PrefixResponse(response, prefix)
    if len(prefix) > MAX_ERROR_BYTES:
        return prepared
    try:
        value = json.loads(prefix)
    except (ValueError, UnicodeError):
        return prepared
    code = value.get("error") if isinstance(value, dict) else None
    if not isinstance(code, str) or code not in LEGACY_ERRORS:
        return prepared
    message = value.get("message")
    if not isinstance(message, str) or not message.strip():
        message = LEGACY_ERRORS[code]
    body = json.dumps(error_payload(code, message), ensure_ascii=False).encode("utf8")
    prepared.prefix.close()
    prepared.prefix = io.BytesIO(body)
    prepared.changed = True
    for key in ("Content-Length", "Content-MD5", "Digest", "ETag"):
        if key in prepared.headers:
            del prepared.headers[key]
    prepared.headers["Content-Length"] = str(len(body))
    return prepared
