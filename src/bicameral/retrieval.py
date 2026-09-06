"""Dependency-free BM25 retrieval over short text records."""

from __future__ import annotations

import math
import re
from collections import Counter

_TOKEN = re.compile(r"[a-z0-9_]+")
_STOP = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "is", "it", "this", "that", "with", "as",
    "be", "by", "at", "from", "so", "if", "are", "was", "not", "but", "into", "than", "then", "its", "when",
}


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if len(t) > 1 and t not in _STOP]


class BM25:
    def __init__(self, docs: list[str], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.docs = [tokenize(d) for d in docs]
        self.n = len(self.docs)
        self.avgdl = (sum(len(d) for d in self.docs) / self.n) if self.n else 0.0
        self.tf = [Counter(d) for d in self.docs]
        df: Counter[str] = Counter()
        for d in self.docs:
            df.update(set(d))
        self.idf = {t: math.log(1 + (self.n - n_t + 0.5) / (n_t + 0.5)) for t, n_t in df.items()}

    def score(self, query: str, idx: int) -> float:
        q = tokenize(query)
        if not q or not self.docs:
            return 0.0
        tf, dl = self.tf[idx], len(self.docs[idx])
        s = 0.0
        for t in q:
            if t not in tf:
                continue
            f = tf[t]
            s += self.idf[t] * (f * (self.k1 + 1)) / (f + self.k1 * (1 - self.b + self.b * dl / (self.avgdl or 1)))
        return s

    def top(self, query: str, k: int = 5, min_score: float = 0.0) -> list[tuple[int, float]]:
        scored = [(i, self.score(query, i)) for i in range(self.n)]
        scored = [(i, s) for i, s in scored if s > min_score]
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:k]
