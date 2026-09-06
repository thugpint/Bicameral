"""Loads KEY=VALUE config files."""

from util import strip_quotes


def parse_kv(line):
    if "=" not in line:
        raise ValueError(f"expected KEY=VALUE, got {line!r}")
    key, value = line.split("=", 1)
    return key.strip(), strip_quotes(value.strip())


def load(lines):
    out = {}
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        k, v = parse_kv(line)
        out[k] = v
    return out
