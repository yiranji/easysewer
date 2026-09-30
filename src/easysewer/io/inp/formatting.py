"""Shared positional formatting without dependencies on feature codecs."""


def optional_tail(values, defaults=None):
    """Materialize only interior defaults needed to reach later fields."""
    values = list(values)
    defaults = defaults or ["0"] * len(values)
    while values and values[-1] is None:
        values.pop()
    return tuple(defaults[index] if value is None else value for index, value in enumerate(values))
