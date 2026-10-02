"""Independent, order-insensitive random streams derived from one world seed.

The layout sampler keeps ``random.Random(seed)``; every other category (appearance, fauna,
environment, clutter, events, tasks) draws from its own hashed stream so adding or changing
one category never shifts the layout, routes or occluders of an existing seed.
"""

from __future__ import annotations
import hashlib
import json
import random


def substream(seed: int, *labels: str) -> random.Random:
    """A stream keyed by the JSON array ``[seed, *labels]`` (unambiguous for any label text)."""
    if not all(isinstance(label, str) for label in labels):
        raise TypeError("stream labels must be strings")
    key = json.dumps([int(seed), *labels], ensure_ascii=True, separators=(",", ":"))
    digest = hashlib.sha256(key.encode("ascii")).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))
