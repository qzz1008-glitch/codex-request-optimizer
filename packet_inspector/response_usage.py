"""Validated response usage shared by capture and presentation adapters."""

TOKEN_FIELDS = ("input_tokens", "output_tokens", "total_tokens")


def token_values(usage):
    if not isinstance(usage, dict) or not all(type(usage.get(key)) is int and usage[key] >= 0
                                             for key in TOKEN_FIELDS):
        return None
    return {key: usage[key] for key in TOKEN_FIELDS}


def packet_usage(packet):
    if packet.get("status") == 200 and packet.get("state") in {"streaming", "complete"}:
        return token_values(packet.get("upstream_usage"))
    return None
