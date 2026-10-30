"""API reference prices, not a conversion of a ChatGPT subscription balance."""
from decimal import Decimal, InvalidOperation
import re

# USD / million tokens: ordinary input, output, cache read, 30m write, 12h write.
# https://developers.openai.com/api/docs/pricing (2026-10-30, Standard)
RATES = {
    "gpt-6-astra": (10, 50, 1, 12.5, 25),
    "gpt-6.1-sol": (2, 10, .10, 2.5, 5),
    "gpt-6.1-terra": (1, 5, .05, 1.25, 2.5),
    "gpt-6-sol": (2, 10, .20, 2.5, 5),
    "gpt-6-luna": (.10, .50, .01, .125, .25),
    "gpt-5.6-sol": (4, 20, .40, 5, 10),
    "gpt-5.6-terra": (2, 12, .20, 2.5, 5),
    "gpt-5.6-luna": (.20, 1.20, .02, .25, .50),
    "gpt-5.4-mini": (.75, 4.5, .075, None, None),
}


def budget_nanos(value):
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0 or amount > 1_000_000:
            raise ValueError()
        return int(amount * 1_000_000_000)
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("额度应为 0 到 1,000,000 美元，0 表示不限。") from None


def reference_cost(values):
    model = values.get("model", "")
    model = re.sub(r"-\d{4}-\d{2}-\d{2}$", "", model) if isinstance(model, str) else ""
    rates = RATES.get(model)
    if rates is None:
        return None
    counts = [values.get(key, 0) for key in ("input_tokens", "output_tokens", "cached_input_tokens",
              "cache_write_30m_tokens", "cache_write_12h_tokens")]
    if any(type(value) is not int or value < 0 for value in counts):
        return None
    incoming, output, cached, write30, write12 = counts
    if cached + write30 + write12 > incoming or values.get("cache_write_unknown"):
        return None
    ordinary = incoming - cached - write30 - write12
    if (write30 and rates[3] is None) or (write12 and rates[4] is None):
        return None
    tier = values.get("service_tier") or "default"
    multiplier = {"default": 1, "standard": 1, "priority": 2, "fast": 2,
                  "ultrafast": 6, "flex": .5, "batch": .5}.get(tier)
    if multiplier is None:
        return None
    rate = [Decimal(str(value or 0)) for value in rates]
    input_factor = 2 if incoming > 272_000 else 1
    output_factor = Decimal("1.5") if incoming > 272_000 else 1
    cost = ((ordinary * rate[0] + cached * rate[2] + write30 * rate[3] + write12 * rate[4]) * input_factor
            + output * rate[1] * output_factor)
    # One dollar = 1e9 nanodollars; rates are per 1e6 tokens.
    return int(cost * Decimal(str(multiplier)) * 1000)
