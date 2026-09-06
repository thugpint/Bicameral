"""String helpers."""

import re

_WS = re.compile(r"\s+")


def normalize_whitespace(text):
    return _WS.sub(" ", text).strip()


def truncate(text, limit):
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."
