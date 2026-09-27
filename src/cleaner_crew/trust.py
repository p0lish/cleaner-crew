"""Per-category trust levels earned from how humans actually treated the crew's MRs.

A category's level is computed from its recent closed MRs (merged *without* human changes
counts as accepted), then capped by the `max_level` a human set in policy.yml:

    fewer than min_samples  -> draft
    rate >= promote_at      -> ready
    rate <  demote_below    -> shadow   (stays there until a human bumps trust_since)
    otherwise               -> draft

History lives on the code host (MR labels + commit trailers), so CI and local runners agree.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import Category, MrRecord
from .policy import Policy, TrustLevel


@dataclass
class TrustStatus:
    category: Category
    level: TrustLevel
    earned: TrustLevel
    samples: int
    rate: float | None
    reason: str


def compute(policy: Policy, history: list[MrRecord]) -> dict[Category, TrustStatus]:
    t = policy.trust
    out = {}
    for cat in Category:
        rule = policy.rule(cat)
        recs = sorted((r for r in history if r.category is cat
                       and (rule.trust_since is None
                            or r.closed_at[:10] >= rule.trust_since.isoformat())),
                      key=lambda r: r.closed_at, reverse=True)[: t.window]
        n = len(recs)
        rate = sum(r.accepted for r in recs) / n if n else None
        if n < t.min_samples:
            earned, why = TrustLevel.DRAFT, f"{n}/{t.min_samples} samples so far"
        elif rate >= t.promote_at:
            earned, why = TrustLevel.READY, f"{rate:.0%} accepted over last {n}"
        elif rate < t.demote_below:
            earned, why = TrustLevel.SHADOW, (f"only {rate:.0%} accepted over last {n}; "
                                              "bump trust_since in policy.yml to retry")
        else:
            earned, why = TrustLevel.DRAFT, f"{rate:.0%} accepted over last {n}"
        level = earned.cap(rule.max_level)
        if level is not earned:
            why += f" (capped at {rule.max_level.value} by policy)"
        out[cat] = TrustStatus(cat, level, earned, n, rate, why)
    return out


def fallback(policy: Policy) -> dict[Category, TrustStatus]:
    """Used when MR history can't be read: never more than draft."""
    return {c: TrustStatus(c, TrustLevel.DRAFT.cap(policy.rule(c).max_level), TrustLevel.DRAFT,
                           0, None, "MR history unavailable") for c in Category}
