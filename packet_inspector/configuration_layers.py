"""TOML table overlays shared by connection planning and identity checks."""
from copy import deepcopy


def merge_layers(*layers):
    """Overlay tables recursively; scalar values and arrays replace earlier ones.

    Unknown provider fields participate without a version-specific whitelist.
    Neither the input documents nor nested credential/header tables are mutated.
    """
    result = {}
    for layer in layers:
        for key, value in layer.items():
            if isinstance(value, dict) and isinstance(result.get(key), dict):
                result[key] = merge_layers(result[key], value)
            else:
                result[key] = deepcopy(value)
    return result
