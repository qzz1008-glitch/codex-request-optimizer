"""Read-only, bounded sharing accounting. Never retain content or identifiers."""
from .response_usage import token_values
from .usage_trigger import UsageStream


def empty_usage():
    return {key: 0 for key in ("input_tokens", "output_tokens", "total_tokens", "cached_input_tokens",
                               "cached_responses", "responses", "missing_responses")}


class SharedUsageStream(UsageStream):
    """Observe terminal usage before forwarding it; do not delay or rewrite output."""
    def __init__(self, response, record, *, model="", service_tier=None):
        super().__init__(response, record, rewrite=False)
        self.model, self.service_tier = model, service_tier
        self.metered = False
        encoding = response.headers.get("Content-Encoding", "identity").strip().lower()
        self.enabled = encoding in {"", "identity"} and self.mode in {
            "", "text/event-stream", "application/json"}

    def _rewrite(self, value, event=False):
        if self.metered or not isinstance(value, dict):
            return False
        if event and value.get("type") not in {
                "response.completed", "response.failed", "response.incomplete", "response.cancelled"}:
            return False
        response = value.get("response") if event else value
        if not isinstance(response, dict):
            return False
        usage = response.get("usage")
        values = token_values(usage)
        if values is None:
            return False
        # Cached input is already included in input_tokens; never add it twice.
        details = usage.get("input_tokens_details")
        cached = details.get("cached_tokens") if isinstance(details, dict) else None
        if type(cached) is int and 0 <= cached <= values["input_tokens"]:
            values["cached_input_tokens"] = cached
        if isinstance(response.get("model"), str):
            values["model"] = response["model"]
        elif self.model:
            values["model"] = self.model
        if isinstance(response.get("service_tier"), str):
            values["service_tier"] = response["service_tier"]
        elif self.service_tier:
            values["service_tier"] = self.service_tier
        if isinstance(details, dict):
            writes = details.get("cache_write_tokens", 0)
            split = details.get("cache_write_tokens_details", {})
            split = split if isinstance(split, dict) else {}
            for source, target in (("ttl_30m_tokens", "cache_write_30m_tokens"),
                                   ("ttl_12h_tokens", "cache_write_12h_tokens")):
                count = split.get(source, 0)
                if type(count) is int and count >= 0:
                    if count:
                        values[target] = count
                else:
                    values["cache_write_unknown"] = True
            if (type(writes) is not int or writes < 0 or
                    writes != values.get("cache_write_30m_tokens", 0) + values.get("cache_write_12h_tokens", 0)):
                values["cache_write_unknown"] = True
        try:
            self.record(values)
            self.metered = True
        except Exception:
            pass  # Accounting must never break a model stream.
        return False
