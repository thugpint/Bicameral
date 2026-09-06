"""Learned routing: which of the two chosen models should execute a given step.

A Thompson-sampling bandit keyed on (step kind, model). Each arm holds a Beta
posterior over "this model's edits for this kind of step get accepted and pass
verification". The architect's suggested role is folded in as a prior pseudo-
success so a fresh install follows the architect until evidence says otherwise.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from .store import Store


@dataclass
class Choice:
    model: str
    role: str
    reason: str


class Router:
    SUGGESTION_PRIOR = 3.0  # pseudo-successes granted to the architect's suggested role (fresh install follows it ~80%)

    def __init__(self, store: Store, learning: bool = True, rng: random.Random | None = None):
        self.store = store
        self.learning = learning
        self.rng = rng or random.Random()

    def choose(self, kind: str, candidates: dict[str, str], suggested_role: str, pinned: bool = False) -> Choice:
        """candidates maps role -> model id (e.g. {"architect": "...", "editor": "..."}).

        A pinned step goes to the suggested role, no exploration: the user said who does it.
        """
        if suggested_role not in candidates:
            suggested_role = "editor" if "editor" in candidates else next(iter(candidates))
        if len(set(candidates.values())) == 1:
            role = suggested_role
            return Choice(candidates[role], role, "single model configured")
        if pinned:
            return Choice(candidates[suggested_role], suggested_role, "pinned by the architect")
        if not self.learning:
            return Choice(candidates[suggested_role], suggested_role, "architect suggestion (learning off)")

        best: Choice | None = None
        best_sample = -1.0
        details: list[str] = []
        for role, model in candidates.items():
            succ, fail = self.store.routing_stats(kind, model)
            alpha = 1.0 + succ + (self.SUGGESTION_PRIOR if role == suggested_role else 0.0)
            beta = 1.0 + fail
            sample = self.rng.betavariate(alpha, beta)
            details.append(f"{role}={succ}/{succ + fail}")
            if sample > best_sample:
                best_sample = sample
                best = Choice(model, role, "")
        assert best is not None
        agree = "follows" if best.role == suggested_role else "overrides"
        best.reason = f"bandit {agree} architect suggestion ({', '.join(details)})"
        return best

    def record(self, kind: str, model: str, success: bool) -> None:
        self.store.record_routing(kind, model, success)
