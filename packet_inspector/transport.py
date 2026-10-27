"""Optional lossless HTTP body encoding; never parses or rewrites the payload."""
from urllib.parse import urlsplit
from typing import NamedTuple

DEFAULT_ZSTD_LEVEL = 3
MIN_ZSTD_LEVEL = 1
MAX_ZSTD_LEVEL = 22
AUTO_ZSTD_HIGH_LEVEL = 16


class PreparedRequest(NamedTuple):
    body: bytes
    encoding: str
    zstd_level: int | None = None


def validate_zstd_level(value):
    if type(value) is not int or not MIN_ZSTD_LEVEL <= value <= MAX_ZSTD_LEVEL:
        raise ValueError(f"zstd level must be an integer from {MIN_ZSTD_LEVEL} to {MAX_ZSTD_LEVEL}")
    return value


def select_zstd_level(encoding, zstd_level=DEFAULT_ZSTD_LEVEL, *,
                      auto_zstd_level=False, budget_bytes=None):
    if not auto_zstd_level:
        return validate_zstd_level(zstd_level)
    if type(budget_bytes) is not int or budget_bytes <= 0:
        raise ValueError("automatic compression requires a positive byte budget")
    # Accept the previous high-level selection from older callers as well.
    level = AUTO_ZSTD_HIGH_LEVEL if validate_zstd_level(zstd_level) >= AUTO_ZSTD_HIGH_LEVEL else MIN_ZSTD_LEVEL
    if encoding.strip().lower() == "zstd":
        if level == MIN_ZSTD_LEVEL:
            return None
    return level


def prepare_zstd(body, encoding, zstd_level=DEFAULT_ZSTD_LEVEL, *,
                 auto_zstd_level=False, budget_bytes=None):
    level = select_zstd_level(encoding, zstd_level,
                              auto_zstd_level=auto_zstd_level, budget_bytes=budget_bytes)
    if level is None:
        return PreparedRequest(body, encoding or "identity")
    import zstandard
    decoded = body
    if encoding.strip().lower() == "zstd":
        from .analyzer import decode_body
        try:
            decoded = decode_body(body, "zstd")
        except (ValueError, zstandard.ZstdError):
            return PreparedRequest(body, encoding)
    # The current original size takes priority over the previous session mode.
    # Native zstd stays byte-for-byte intact when no extra compression is needed.
    if auto_zstd_level and len(decoded) * 100 < budget_bytes * 90:
        return PreparedRequest(body, encoding or "identity")
    compressed = zstandard.ZstdCompressor(level=level).compress(decoded)
    if len(compressed) < len(body):
        return PreparedRequest(compressed, "zstd", level)
    return PreparedRequest(body, encoding or "identity")


def prepare_request_info(body, headers, method, path, mode, zstd_level=DEFAULT_ZSTD_LEVEL, *,
                         auto_zstd_level=False, budget_bytes=None):
    encoding = next((v for k, v in headers.items() if k.lower() == "content-encoding"), "identity")
    # /responses also carries modern foreground/background compaction requests.
    # Other endpoints have not been verified to accept zstd request bodies.
    eligible = (mode == "zstd" and method == "POST" and body
                and urlsplit(path).path.rstrip("/").endswith("/responses")
                and encoding.strip().lower() in {"", "identity", "zstd"})
    if eligible:
        return prepare_zstd(body, encoding, zstd_level,
                            auto_zstd_level=auto_zstd_level, budget_bytes=budget_bytes)
    return PreparedRequest(body, encoding or "identity")


def prepare_request(body, headers, method, path, mode, zstd_level=DEFAULT_ZSTD_LEVEL, *,
                    auto_zstd_level=False, budget_bytes=None):
    prepared = prepare_request_info(body, headers, method, path, mode, zstd_level,
                                    auto_zstd_level=auto_zstd_level, budget_bytes=budget_bytes)
    return prepared.body, prepared.encoding
