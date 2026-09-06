"""Parses --set KEY=VALUE overrides from the command line."""

from util import strip_quotes


def parse_kv(line):
    if "=" not in line:
        raise ValueError(f"expected KEY=VALUE, got {line!r}")
    key, value = line.split("=", 1)
    return key.strip(), strip_quotes(value.strip())


def overrides(argv):
    out = {}
    it = iter(argv)
    for arg in it:
        if arg == "--set":
            k, v = parse_kv(next(it))
            out[k] = v
    return out
