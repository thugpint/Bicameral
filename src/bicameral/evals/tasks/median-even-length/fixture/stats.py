"""Tiny statistics helpers."""


def mean(xs):
    if not xs:
        raise ValueError("mean of empty list")
    return sum(xs) / len(xs)


def median(xs):
    if not xs:
        raise ValueError("median of empty list")
    s = sorted(xs)
    return s[len(s) // 2]
