"""Read-only, bounded sharing accounting. Never retain content or identifiers."""
from .response_usage import token_values
from .usage_trigger import UsageStream


def empty_usage():
    return {key: 0 for key in ("input_tokens", "output_tokens", "total_tokens", "cached_input_tokens",
                               "cached_responses", "responses", "missing_responses")}


class SharedUsageStream(UsageStream):
    """Observe terminal usage before forwarding it; do not delay or rewrite output."""
    def __init__(self, response, record):
        super().__init__(response, record, rewrite=False)
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
        try:
            self.record(values)
            self.metered = True
        except Exception:
            pass  # Accounting must never break a model stream.
        return False
