"""Mechanical (no fuzzy matching, no LLM) verification that an extracted
rank-claim quote is genuinely present in the transcript it's attributed
to.

Per PLAN.md's QUOTE-ANCHORING RULE, this check exists specifically to
kill fabricated or misread claims by construction: an LLM extracting a
quote can hallucinate or paraphrase, but a plain substring check either
finds the exact words in the transcript or it doesn't. Any fuzziness
here (rapidfuzz, embedding similarity, "close enough") would let a
paraphrased or partially-hallucinated quote through, which defeats the
whole point of the rule — the owner needs to be able to trust that
`quote_verified=True` means "this exact sentence is really in the
transcript", not "something similar is probably in there somewhere".
Interpretation (is this the creator speaking, not a guest? is it
specific enough to admit as evidence?) is left entirely to the human
reviewer in `claims_review.md`; this function only ever answers the
narrower, purely mechanical question of whether the quote is really
there.
"""

from __future__ import annotations

import re

_WHITESPACE_RE = re.compile(r"\s+")


def _collapse_whitespace(text: str) -> str:
    """Collapse every run of whitespace — including newlines, since
    transcript segments are stored as separate lines and wrap
    arbitrarily — to a single space, and strip the ends."""
    return _WHITESPACE_RE.sub(" ", text).strip()


def quote_in_transcript(quote: str, transcript_text: str) -> bool:
    """True iff `quote` is an exact substring of `transcript_text` after
    collapsing whitespace runs to single spaces on both sides.

    Case-sensitive and substring-exact by design (see module docstring)
    — a quote-anchoring check that tolerated near-misses wouldn't anchor
    anything. An empty or whitespace-only quote never verifies, even
    though it's trivially a substring of any text — that emptiness would
    silently defeat the whole check rather than genuinely anchoring a
    claim.
    """
    collapsed_quote = _collapse_whitespace(quote)
    if not collapsed_quote:
        return False
    return collapsed_quote in _collapse_whitespace(transcript_text)
