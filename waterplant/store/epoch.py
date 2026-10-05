"""Numeric helpers layered on top of the string key/value store."""

from __future__ import annotations

from .store import Store


def load_float(store: Store, key: str) -> tuple[float, bool]:
    """Read a float value, reporting whether it was present and parseable."""

    raw, present = store.get(key)
    if not present:
        return 0.0, False
    try:
        return float(raw), True
    except ValueError:
        return 0.0, False


def save_float(store: Store, key: str, value: float) -> None:
    """Persist a float with a stable four decimal text form."""

    store.put(key, f"{value:.4f}")
