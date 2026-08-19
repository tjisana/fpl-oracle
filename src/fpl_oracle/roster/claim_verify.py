"""Mechanical (no fuzzy matching, no LLM) verification that an extracted
rank-claim quote is genuinely present in the transcript it's attributed
to.

Per PLAN.md's QUOTE-ANCHORING RULE, this check exists specifically to
kill fabricated or misread claims by construction: an LLM extracting a
quote can hallucinate or paraphrase, but a plain substring check either
finds the exact words in the transcript or it doesn't. Any *semantic*
fuzziness here (rapidfuzz, embedding similarity, "close enough") would
let a paraphrased or partially-hallucinated quote through, which defeats
the whole point of the rule — the owner needs to be able to trust that
`quote_verified=True` means "these exact words are really in the
transcript", not "something similar is probably in there somewhere".
Interpretation (is this the creator speaking, not a guest? is it
specific enough to admit as evidence?) is left entirely to the human
reviewer in `claims_review.md`; this function only ever answers the
narrower, purely mechanical question of whether the quote is really
there.

Whitespace collapsing and case-folding are both applied before the
substring check, and both stay firmly on the mechanical side of that
line — deterministic, content-blind normalizations, not judgment calls
about meaning. Case-folding in particular is necessary in practice, not
just cosmetic: auto-generated YouTube transcripts are near-uniformly
lowercase, while an extraction LLM asked for a "verbatim quote" tends to
sentence-case it. Staying case-sensitive would flip genuinely verbatim
quotes to UNVERIFIED for a reason that has nothing to do with whether
the words are really there — forcing the owner back to rereading
transcripts, the exact failure this design exists to avoid. Punctuation
is deliberately NOT normalized here; that stays the extraction prompt's
job (main session, `extract/`), not this check's.
"""

from __future__ import annotations

import re

_WHITESPACE_RE = re.compile(r"\s+")


def collapse_whitespace(text: str) -> str:
    """Collapse every run of whitespace — including newlines, since
    transcript segments are stored as separate lines and wrap
    arbitrarily — to a single space, and strip the ends.

    Public because `claim_extract.locate_quote_timestamp` builds its
    quote-position index with this exact normalization: its
    "verifies implies locates" invariant depends on this function
    staying in lockstep with `quote_in_transcript`."""
    return _WHITESPACE_RE.sub(" ", text).strip()


def quote_in_transcript(quote: str, transcript_text: str) -> bool:
    """True iff `quote` is an exact substring of `transcript_text` after
    (a) collapsing whitespace runs to single spaces on both sides and
    (b) case-folding both sides (see module docstring for why both of
    these — and only these — normalizations are mechanical enough to
    belong in a quote-ANCHORING check).

    Substring-exact otherwise — no fuzzy/approximate matching, no
    punctuation normalization. An empty or whitespace-only quote never
    verifies, even though it's trivially a substring of any text — that
    emptiness would silently defeat the whole check rather than
    genuinely anchoring a claim.
    """
    collapsed_quote = collapse_whitespace(quote).casefold()
    if not collapsed_quote:
        return False
    return collapsed_quote in collapse_whitespace(transcript_text).casefold()
