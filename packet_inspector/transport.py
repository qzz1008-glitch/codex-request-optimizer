"""Optional lossless HTTP body encoding; never parses or rewrites the payload."""
from urllib.parse import urlsplit

DEFAULT_ZSTD_LEVEL = 3
MIN_ZSTD_LEVEL = 1
MAX_ZSTD_LEVEL = 17


def validate_zstd_level(value):
    if type(value) is not int or not MIN_ZSTD_LEVEL <= value <= MAX_ZSTD_LEVEL:
        raise ValueError("zstd level must be an integer from 1 to 17")
    return value


def prepare_request(body, headers, method, path, mode, zstd_level=DEFAULT_ZSTD_LEVEL):
    encoding = next((v for k, v in headers.items() if k.lower() == "content-encoding"), "identity")
    # /responses also carries modern foreground/background compaction requests.
    # Other endpoints have not been verified to accept zstd request bodies.
    eligible = (mode == "zstd" and method == "POST" and body
                and urlsplit(path).path.rstrip("/").endswith("/responses")
                and encoding.strip().lower() in {"", "identity", "zstd"})
    if eligible:
        import zstandard
        level = validate_zstd_level(zstd_level)
        decoded = body
        if encoding.strip().lower() == "zstd":
            from .analyzer import decode_body
            try:
                decoded = decode_body(body, "zstd")
            except (ValueError, zstandard.ZstdError):
                return body, encoding
        compressed = zstandard.ZstdCompressor(level=level).compress(decoded)
        if len(compressed) < len(body):
            return compressed, "zstd"
    return body, encoding or "identity"
