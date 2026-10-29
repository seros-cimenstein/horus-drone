"""Untrusted text (detector/grounder labels, region-watch names, map names): always data, never
instructions. Standard library only, so every tool module can import it."""

from __future__ import annotations

import json
from typing import Any

# Bound on untrusted strings HORUS echoes (labels, names).
MAX_TEXT_LENGTH = 64


def quote_label(label: Any) -> str:
    """JSON-quote an untrusted label or name (bounded) so it is always data, never instruction."""
    return json.dumps(str(label)[:MAX_TEXT_LENGTH], ensure_ascii=False)
