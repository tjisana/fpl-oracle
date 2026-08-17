"""Seed creator roster — drafted 2026-08-15 from conversation research.

Tier 1 = core weighted vote (YouTube-active, strong verified pedigree).
Tier 2 = sentiment radar / diversity picks (ingested, lower or provisional weight).

`fpl_team_id` and `channel_id` are unresolved (None) until Phase 1 verifies them:
 - fpl_team_id: from video descriptions, X bios, or the FFPundit creators
   mini-league (https://www.fantasyfootballpundit.com/fpl-content-creators-league-table/)
 - channel_id: via YouTube Data API search, confirmed by eye.
Verified pedigree notes below came from public sources (BBC/FFS profiles,
season-review videos) and should be re-verified from entry/{id}/history/
once team IDs are resolved.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class SeedCreator:
    name: str
    tier: int
    youtube_hint: str  # search string to locate the channel
    notes: str = ""
    channel_id: str | None = None
    fpl_team_id: int | None = None


SEED_ROSTER: list[SeedCreator] = [
    # ---- Tier 1: core weighted vote ----
    SeedCreator(
        "Let's Talk FPL (Andy)",
        1,
        "Lets Talk FPL",
        "Huge channel AND elite rank — finished 588th overall in 2025/26",
    ),
    SeedCreator("FPL Harry", 1, "FPL Harry"),
    SeedCreator("FPL Raptor", 1, "FPL Raptor"),
    SeedCreator(
        "Pras / The FPL Wire",
        1,
        "FPL Wire Pras",
        "13 straight top-100k seasons, five top-10k finishes",
    ),
    SeedCreator("FPL Mate", 1, "FPL Mate"),
    SeedCreator("FPL Focal", 1, "FPL Focal"),
    SeedCreator(
        "Holly Shand", 1, "Holly Shand FPL", "Two top-10k finishes; BBC/Premier League pundit"
    ),
    SeedCreator(
        "FPL BlackBox (Mark Sutherns)",
        1,
        "FPL BlackBox",
        "Fantasy Football Scout founder; multiple top-10k finishes",
    ),
    SeedCreator("Zophar", 1, "Zophar FPL", "FPL Wire co-host"),
    SeedCreator("Lateriser", 1, "Lateriser FPL"),
    SeedCreator("Gianni Buttice", 1, "Gianni Buttice FPL", "BBC FPL expert"),
    SeedCreator("FPL Heisenberg", 1, "FPL Heisenberg", "BBC FPL expert"),
    # ---- Tier 2: sentiment radar / diversity ----
    SeedCreator("FPL Family", 2, "FPL Family", "Casual-manager pulse"),
    SeedCreator("FPL Salah", 2, "FPL Salah"),
    SeedCreator("FPL Hints", 2, "FPL Hints"),
    SeedCreator("FPL Matthew", 2, "FPL Matthew"),
    SeedCreator("Big Man Bakar", 2, "Big Man Bakar FPL"),
    SeedCreator("FPL Dylan", 2, "FPL Dylan", "Smaller, differential-leaning"),
    SeedCreator(
        "Planet FPL (Suj & James)", 2, "Planet FPL", "Podcast-style, longer-form reasoning"
    ),
    SeedCreator("FPLtips", 2, "FPLtips", "Long-running tactical channel"),
]
