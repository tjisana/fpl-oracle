"""Video classification for the autopilot ledger (`kind`).

v0 is a title regex (the one used in the 2026-11-06 timing analysis), kept
behind the `Classifier` Protocol so the Haiku classifier (ROADMAP M4) drops
in unchanged.

Decision order (first match wins), see `RegexClassifier.classify`:

1. `final`      "final thoughts/decisions", "locked"
2. `selection`  "team selection/reveal"
3. `review`     review / recap / reaction / post-mortem / hindsight
4. `live`       live / stream / Q&A / premiere
5. `show`       podcast / pod / show / episode
6. `other`      title does not match the relevance regex at all
7. `transfers`  relevant, mentions "transfer"
8. `plan`       any other relevant title (gw N, gameweek, captain, wildcard,
                free hit)

Final and selection are checked BEFORE the negative kinds on purpose: a
combined "GW10 review + GW11 team selection" title must stay a selection
video. The price is that a negative word in a selection title (e.g. "Team
Selection LIVE") is still treated as selection.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Literal, Protocol

VideoKind = Literal["selection", "final", "transfers", "plan", "review", "live", "show", "other"]

# The relevance gate from the 2026-11-06 analysis (case-insensitive).
RELEVANT_RE = re.compile(
    r"gw ?\d|gameweek|team (selection|reveal)|final (thoughts|decisions)|captain"
    r"|transfer|wildcard|free hit|locked",
    re.IGNORECASE,
)
_FINAL_RE = re.compile(r"final (thoughts|decisions)|\blocked\b", re.IGNORECASE)
_SELECTION_RE = re.compile(r"team (selection|reveal)", re.IGNORECASE)
_REVIEW_RE = re.compile(
    r"\breview\b|\brecap\b|\breaction\b|post[- ]?mortem|\bhindsight\b", re.IGNORECASE
)
_LIVE_RE = re.compile(r"\blive\b|livestream|\bstream\b|\bq ?& ?a\b|\bpremiere\b", re.IGNORECASE)
_SHOW_RE = re.compile(r"\bpodcast\b|\bpod\b|\bshow\b|\bepisode\b|\bep\.? ?\d+\b", re.IGNORECASE)
_TRANSFERS_RE = re.compile(r"transfer", re.IGNORECASE)

# Share of the budget priority a kind earns (creator weight x multiplier).
KIND_MULTIPLIER: Mapping[VideoKind, float] = {
    "selection": 1.0,
    "final": 1.0,
    "transfers": 0.6,
    "plan": 0.6,
    "other": 0.3,
    "review": 0.0,
    "live": 0.0,
    "show": 0.0,
}

# Kinds that count toward "this creator has a relevant video" in the
# readiness report. `other` is harvested opportunistically but is not
# evidence that the creator has published their picks.
RELEVANT_KINDS: frozenset[VideoKind] = frozenset({"selection", "final", "transfers", "plan"})

# Kinds that earn the full-limit budget in the final-window reserve rule.
FINAL_CLASS_KINDS: frozenset[VideoKind] = frozenset({"selection", "final"})


def priority_multiplier(kind: VideoKind) -> float:
    return KIND_MULTIPLIER[kind]


def is_skipped_kind(kind: VideoKind) -> bool:
    """review / live / show: never harvested (multiplier 0)."""
    return KIND_MULTIPLIER[kind] == 0.0


class Classifier(Protocol):
    def classify(self, title: str) -> VideoKind: ...


class RegexClassifier:
    """Title-only v0 classifier. See the module docstring for the order."""

    def classify(self, title: str) -> VideoKind:
        if _FINAL_RE.search(title):
            return "final"
        if _SELECTION_RE.search(title):
            return "selection"
        if _REVIEW_RE.search(title):
            return "review"
        if _LIVE_RE.search(title):
            return "live"
        if _SHOW_RE.search(title):
            return "show"
        if not RELEVANT_RE.search(title):
            return "other"
        if _TRANSFERS_RE.search(title):
            return "transfers"
        return "plan"
