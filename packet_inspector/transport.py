"""Optional lossless HTTP body encoding; never parses or rewrites the payload."""
from urllib.parse import urlsplit


def prepare_request(body, headers, method, path, mode):
    encoding = next((v for k, v in headers.items() if k.lower() == "content-encoding"), "identity")
    # /responses also carries modern foreground/background compaction requests.
    # Other endpoints have not been verified to accept zstd request bodies.
    eligible = (mode == "zstd" and method == "POST" and body
                and urlsplit(path).path.rstrip("/").endswith("/responses")
                and encoding.strip().lower() in {"", "identity"})
    if eligible:
        import zstandard
        compressed = zstandard.ZstdCompressor(level=3).compress(body)
        if len(compressed) < len(body):
            return compressed, "zstd"
    return body, encoding or "identity"
