"""Published draft boards from FPL Draft creators, for the 2026/27 season.

Draft-format creators are a small field — most "FPL draft" content is classic
FPL managers sketching a provisional squad, which is a different thing
entirely. These are the two channels that cover the actual draft game.

Every entry below was read off the creator's own published top-20 video and is
recorded with the video id so it can be re-checked. Ranks that could not be
attributed unambiguously from the transcript are OMITTED rather than guessed:
a partial list is handled correctly by the blend in `board.py` (a player absent
from a list simply gets no vote from that creator), whereas a wrong rank is a
silent distortion. This mirrors the project's standing rule that an unresolved
name never enters the data.

Names are matched against the live draft player DB at load time; anything that
fails to resolve is reported rather than dropped silently.
"""

from __future__ import annotations

from pydantic import BaseModel


class ExpertBoard(BaseModel):
    """One creator's published draft ranking."""

    creator: str
    source_url: str
    complete: bool  # False when the transcript did not yield every rank
    ranks: dict[str, int]  # player name as published -> their draft rank


# Dom Croft's top 20. The full list was recapped on screen at the end of the
# video, so every rank here is directly attributable except #18, which was not
# stated in the audio and is therefore omitted.
FPL_DRAFT_ZONE = ExpertBoard(
    creator="FPL Draft Zone",
    source_url="https://www.youtube.com/watch?v=W2sUovur47A",
    complete=False,
    ranks={
        "Haaland": 1,
        "B.Fernandes": 2,
        "Isak": 3,
        # Full name required: the pool holds both Cole Palmer (MID) and Alex
        # Palmer (GK), and a bare "Palmer" is correctly refused as ambiguous.
        "Cole Palmer": 4,
        "João Pedro": 5,
        "Semenyo": 6,
        "Saka": 7,
        "Gabriel": 8,
        "Thiago": 9,
        "Watkins": 10,
        "Rice": 11,
        "Gibbs-White": 12,
        "Morgan Rogers": 13,
        "Sesko": 14,
        "Mbeumo": 15,
        "Havertz": 16,
        "Szoboszlai": 17,
        # 18 not stated on air
        "Bruno Guimarães": 19,
        "Calvert-Lewin": 20,
    },
)

# Mitch's top 20, delivered as a countdown. The transcript interleaves his own
# ranks with "global median pick" (ADP) figures for the same players, so most
# positions cannot be separated from the ADP mentioned in the same breath.
# Only the unambiguous reveals are recorded.
DRAFT_FC = ExpertBoard(
    creator="Draft FC",
    source_url="https://www.youtube.com/watch?v=0Igypp5nEuI",
    complete=False,
    ranks={
        "João Pedro": 4,
        "Saka": 6,
        "Watkins": 8,
        "Semenyo": 13,
        "Cunha": 16,
        "Gyökeres": 18,
        "Wirtz": 19,
        "Cherki": 20,
    },
)

ALL_BOARDS: list[ExpertBoard] = [FPL_DRAFT_ZONE, DRAFT_FC]
