"""The real creator registry — `seed_roster.py` promoted with resolved
YouTube channel IDs and FPL-team-ID research results.

This supersedes an earlier registry pass that got corrected by the
project owner on two counts (see PLAN.md / session notes for the full
story):

1. The roster itself was researched from scratch instead of using the
   owner's own curated list. `seed_roster.py` is now the owner's
   canonical 20 — this file just resolves it.
2. FPL-ID verification was checking candidate entries against creators'
   CHANNEL/BRAND names ("FPL Harry") instead of their REAL names, which
   silently threw away potentially-correct IDs. This pass finds each
   creator's real name FIRST (BBC Sport, Fantasy Football Scout's Pro
   Pundits page, Companies House, LinkedIn, self-disclosed emails/bios),
   then checks every candidate — including everything rejected last
   time — against that.

Result: real names are now solid for most of tier 1, and a `Verification.
DOCUMENTED` tier was added for creators a reputable third party (BBC,
FFS) has publicly attributed specific finishes to under their real name,
even without a pullable ID. But **0 of 20 still have an API-verified ID**
— every fresh candidate this pass also failed live verification, in a
few cases because a years-old article's entry ID has since come to
belong to a completely different, unrelated manager (see notes below;
worth remembering as a standing risk, not just a one-off). Institutional
brands (Fantasy Football Scout, Fantasy Football Hub) were dropped
entirely per the owner's instruction — no person, no track record, no
Tier.CORE claim. Three "creators" (FPL Salah, FPL Matthew, Big Man
Bakar) turned out to be recurring on-camera personas whose content is
actually produced on Fantasy Football Hub's own channel, not a personal
one — flagged below; ingestion will need title-based filtering, not a
per-creator channel_id, if picked up later.

A caution surfaced this pass, worth keeping in mind generally: at least
one web-search/fetch tool call returned a plausible-looking table of FPL
entry IDs that turned out to be entirely fabricated (every ID in it
resolved to an unrelated manager on the live API). Every ID anywhere in
this file was checked against the live API directly — but the lesson is
to never trust a numeric FPL ID from search/fetch summarization without
that live check, even when it's presented with citations.
"""

from fpl_oracle.roster.models import ClaimedFinish, Creator, Tier, Verification
from fpl_oracle.roster.weights import weight_for

_BBC_FPL_EXPERTS = "https://feeds.bbci.co.uk/sport/articles/c4gj7pl3p82o"
_FFS_PRO_PUNDITS = "https://www.fantasyfootballscout.co.uk/the-ffs-pro-pundits"
_ANDY_SEASON_REVIEW = "https://www.youtube.com/watch?v=UW85Hel20QE"

_ANDY_CLAIMS = [
    ClaimedFinish(
        description="588th overall, 2025/26 season (stated in his own season-review video; "
        "only corroborated at search-snippet level, not a direct read of the video itself "
        "— worth a human spot-check of the primary source)",
        rank=588,
        source_url=_ANDY_SEASON_REVIEW,
        count=1,
    )
]
_PRAS_CLAIMS = [
    ClaimedFinish(
        description="five top-10k finishes (of 13 straight top-100k seasons)",
        rank=10_000,
        source_url=_BBC_FPL_EXPERTS,
        count=5,
    ),
    ClaimedFinish(
        description="nine top-25k finishes (of 13 straight top-100k seasons)",
        rank=25_000,
        source_url=_BBC_FPL_EXPERTS,
        count=9,
    ),
]
_HOLLY_CLAIMS = [
    ClaimedFinish(
        description="two top-10k finishes over 10 years playing FPL",
        rank=10_000,
        source_url=_BBC_FPL_EXPERTS,
        count=2,
    )
]
_HEISENBERG_CLAIMS = [
    ClaimedFinish(
        description="best-ever finish 836th in the world",
        rank=836,
        source_url=_BBC_FPL_EXPERTS,
        count=1,
    ),
    ClaimedFinish(
        description="one of seven top-30k finishes",
        rank=30_000,
        source_url=_BBC_FPL_EXPERTS,
        count=7,
    ),
]
_GIANNI_CLAIMS = [
    ClaimedFinish(
        description="top-100k in 11 of the past 14 seasons (outside top-100k only 3 times)",
        rank=100_000,
        source_url=_BBC_FPL_EXPERTS,
        count=11,
    ),
    ClaimedFinish(
        description="eight top-50k finishes",
        rank=50_000,
        source_url=_BBC_FPL_EXPERTS,
        count=8,
    ),
]
_FOCAL_CLAIMS = [
    ClaimedFinish(
        description="briefly reached world #1 in 2021/22 — scored conservatively as an elite "
        "(not literal rank-1) finish, since the claim is he briefly touched #1 mid-season, "
        "not that he finished the season there",
        rank=500,
        source_url=_FFS_PRO_PUNDITS,
        count=1,
    ),
    ClaimedFinish(
        description='finished ~3,400th ("3.4k") in 2024/25',
        rank=3_400,
        source_url=_FFS_PRO_PUNDITS,
        count=1,
    ),
]

_FFH_CHANNEL_ID = "UCcqEr3DfrRwtoF2a1yW8qgQ"
_FFH_SHARED_NOTE = (
    "Not a dedicated personal channel — his 'Team Reveal' content is produced and hosted on "
    "Fantasy Football Hub's own channel under his persona name; his claimed personal channel "
    "exists but is empty (0-1 videos). If this creator is picked up for transcript ingestion "
    "later, it needs title-substring filtering against the FFH channel, not a 1:1 channel_id."
)

REGISTRY: list[Creator] = [
    # ---- Tier 1 candidates: real person, track-record-eligible ----
    Creator(
        creator_id="lets-talk-fpl",
        name="Let's Talk FPL (Andy)",
        youtube_hint="Lets Talk FPL",
        tier=Tier.CORE,
        channel_id="UCxeOc7eFxq37yW_Nc-69deA",
        channel_title="Let's Talk FPL",
        subscriber_count=501_000,
        real_name="Andy Mears",
        verification=Verification.DOCUMENTED,
        documented_finishes=_ANDY_CLAIMS,
        weight=weight_for(Verification.DOCUMENTED, documented_finishes=_ANDY_CLAIMS),
        notes=(
            "Real name Andy Mears — corroborated by 3 independent sources (a podcast interview "
            "title, his solo.to link page, and his personal YouTube handle @andymears501). No "
            "FPL entry ID found/verified despite dedicated searching (entry 40, the one prior "
            "candidate, is confirmed unrelated — Korn Supatrabutra). Documented tier rests on "
            "his own season-review video's claim; see the caveat on that claim above."
        ),
    ),
    Creator(
        creator_id="fpl-harry",
        name="FPL Harry",
        youtube_hint="FPL Harry",
        tier=Tier.SECONDARY,
        channel_id="UCcPWnCj5AKC19HaySZjb25g",
        channel_title="FPL Harry",
        subscriber_count=233_000,
        real_name=None,
        verification=Verification.UNVERIFIED,
        weight=weight_for(Verification.UNVERIFIED),
        notes=(
            "NEAR MISS, worth a human follow-up: real name unconfirmed (a business email "
            "suggested 'Harry Daniels' but that's unverified — could just be an agency "
            "contact), BUT Fantasy Football Scout's own recurring gameweek-recap article "
            "series explicitly attributes entry 1320 to 'FPL Harry' by name in a specific, "
            "checkable sentence (fantasyfootballscout.co.uk/2026/03/02/how-fpl-harry-mark-"
            "sutherns-more-did-in-gameweek-28) — not a generic guess. However entry 1320's "
            "real name (Zinedine Bashir) doesn't corroborate, and its history (92k-579k across "
            "5 seasons since 2021/22) sits far below his self-reported 'seven top-15k, five "
            "consecutive top-10k' claims — though that account only goes back 5 seasons while "
            "he claims 9 years playing, so it may simply be a newer/secondary account rather "
            "than proof of a wrong ID. Left unverified pending a real-name confirmation or a "
            "direct answer from the creator — deliberately not accepting on a third-party "
            "attribution alone without name corroboration."
        ),
    ),
    Creator(
        creator_id="fpl-raptor",
        name="FPL Raptor",
        youtube_hint="FPL Raptor",
        tier=Tier.SECONDARY,
        channel_id="UC54QLWzsMifTRjNQ02z5pCw",
        channel_title="FPL Raptor",
        subscriber_count=178_000,
        real_name="Ross Dowsett",
        verification=Verification.UNVERIFIED,
        weight=weight_for(Verification.UNVERIFIED),
        notes=(
            "Real name Ross Dowsett, confirmed independently (his own X account @ross_dowsett, "
            "a 2021 FFS interview, a Fantasy Football Fix contributor bio). No FPL ID found "
            "under that name; the two previously-rejected candidates (18675 = a spam account, "
            "746 = unrelated manager 'A. Almarzooqi') remain correctly rejected."
        ),
    ),
    Creator(
        creator_id="pras",
        name="Pras / The FPL Wire",
        youtube_hint="FPL Wire Pras",
        tier=Tier.CORE,
        channel_id="UCtIPFexB6PLKNNl0XH3SKKw",
        channel_title="The FPL Wire - Fantasy Premier League",
        subscriber_count=44_000,
        real_name="Prasun Singhal",
        verification=Verification.DOCUMENTED,
        documented_finishes=_PRAS_CLAIMS,
        weight=weight_for(Verification.DOCUMENTED, documented_finishes=_PRAS_CLAIMS),
        notes=(
            "Real name Prasun Singhal, well corroborated (FFS 'Meet the Manager' feature, an "
            "official FPL/Premier League Facebook video caption, an FFH team-reveal article, "
            "BBC). No FPL ID found under that name despite checking his FFS interview article "
            "and his co-hosts' shared channel for an embedded entry link."
        ),
    ),
    Creator(
        creator_id="fpl-mate",
        name="FPL Mate",
        youtube_hint="FPL Mate",
        tier=Tier.SECONDARY,
        channel_id="UCweDAlFm2LnVcOqaFU4_AGA",
        channel_title="FPL Mate",
        subscriber_count=256_000,
        real_name="Dan (surname unconfirmed)",
        verification=Verification.UNVERIFIED,
        weight=weight_for(Verification.UNVERIFIED),
        notes=(
            "First name 'Dan' confirmed (his own channel's business-inquiry email is "
            "dan@fplmate.co.uk, consistent across his socials). Surname 'Bradley' appears on "
            "fantasyfootballbible.co.uk, but that site is unreliable (flagged this project — "
            "elsewhere it serves what looks like templated/fabricated per-manager content) and "
            "no independent source corroborates it (no Companies House or LinkedIn match "
            "found). Both previously-tried candidates (7386 = 'Mohammed Al-Otaibi', 1266 = "
            "'Bradley Killick') fail on first name alone regardless of the surname question."
        ),
    ),
    Creator(
        creator_id="fpl-focal",
        name="FPL Focal",
        youtube_hint="FPL Focal",
        tier=Tier.CORE,
        channel_id="UC72QokPHXQ9r98ROfNZmaDw",
        channel_title="FPL Focal",
        subscriber_count=253_000,
        real_name="Oscar (surname not publicly disclosed)",
        verification=Verification.DOCUMENTED,
        documented_finishes=_FOCAL_CLAIMS,
        weight=weight_for(Verification.DOCUMENTED, documented_finishes=_FOCAL_CLAIMS),
        notes=(
            "Only a first name is publicly disclosed — he appears to run a deliberately "
            "faceless/surname-anonymous brand (his talent agency's bio page was unreachable). "
            "Did not attempt a first-name-only FPL API match (too ambiguous — many 'Oscar's "
            "exist). Documented tier rests on his own FFS Pro Pundits bio instead."
        ),
    ),
    Creator(
        creator_id="holly-shand",
        name="Holly Shand",
        youtube_hint="Holly Shand FPL",
        tier=Tier.CORE,
        channel_id="UCeVXtaZ6PM7YbdBVrjlWI_g",
        channel_title="Holly Shand FPL",
        subscriber_count=41_200,
        real_name="Holly Shand",
        verification=Verification.DOCUMENTED,
        documented_finishes=_HOLLY_CLAIMS,
        weight=weight_for(Verification.DOCUMENTED, documented_finishes=_HOLLY_CLAIMS),
        notes=(
            "Real/public name confirmed consistent across Premier League, Sky Sports News, "
            "The Athletic, LinkedIn, and BBC. No FPL ID found — the one previously-rejected "
            "candidate (118, 'Mamisch O') remains correctly rejected (also implausibly old "
            "for a modern creator). Documented tier rests on BBC's own reporting."
        ),
    ),
    Creator(
        creator_id="fpl-blackbox",
        name="FPL BlackBox (Mark Sutherns)",
        youtube_hint="FPL BlackBox",
        tier=Tier.SECONDARY,
        channel_id="UCGJ8-xqhOLwyJNuPMsVoQWQ",
        channel_title="FPL BlackBox",
        subscriber_count=37_900,
        real_name="Mark Sutherns",
        verification=Verification.UNVERIFIED,
        weight=weight_for(Verification.UNVERIFIED),
        notes=(
            "Real name Mark Sutherns solidly confirmed (Companies House officer record 'Mark "
            "John Sutherns' as a founding FFS director, LinkedIn, FFS's own site naming him as "
            "co-host). No FPL ID found — previously-rejected 795 ('Finnian Anyanwu') and 5289 "
            "('Chandan Gupta') remain correctly rejected. FLAGGING A DISCREPANCY for your "
            "spot-check: the seed roster's note says 'multiple top-10k finishes', but the only "
            "specific stat this pass could independently find (FFS's Pro Pundits page) says he "
            "'reached top 100,000 for the first time this season' as of a 2025/26 gameweek — "
            "which reads as weaker/more ambiguous than 'multiple top-10k finishes', not "
            "confirming it. Left unverified rather than scoring off an unconfirmed claim; "
            "if you have a source for the stronger claim, this should move to DOCUMENTED."
        ),
    ),
    Creator(
        creator_id="zophar",
        name="Zophar",
        youtube_hint="Zophar FPL",
        tier=Tier.SECONDARY,
        channel_id="UCtIPFexB6PLKNNl0XH3SKKw",
        channel_title="The FPL Wire - Fantasy Premier League",
        subscriber_count=44_000,
        channel_match_flagged=False,
        real_name="Utkarsh Dalmia",
        verification=Verification.UNVERIFIED,
        weight=weight_for(Verification.UNVERIFIED),
        notes=(
            "Shares The FPL Wire's channel with Pras and Lateriser (co-hosts) — the resolver's "
            "fuzzy-match flag on this is a false positive, overridden here since it's a known, "
            "confirmed shared channel. Real name Utkarsh Dalmia, well corroborated (FFS 'Meet "
            "the Manager #20' feature, Premier League's own articles, X posts). A candidate ID "
            "(554) surfaced from a 2020 FFS article but is REJECTED — resolves to an unrelated "
            "Irish manager, 'Nigel Dowler'. Self-reports 'Top10k x7' in the channel description, "
            "but that's a self-authored tagline, not third-party documentation, so left "
            "UNVERIFIED rather than promoted to DOCUMENTED — flagging this as a judgment call "
            "in case you'd rather treat strong self-reported claims the way Andy's own "
            "season-review video was treated (see his entry above)."
        ),
    ),
    Creator(
        creator_id="lateriser",
        name="Lateriser",
        youtube_hint="Lateriser FPL",
        tier=Tier.SECONDARY,
        channel_id="UCtIPFexB6PLKNNl0XH3SKKw",
        channel_title="The FPL Wire - Fantasy Premier League",
        subscriber_count=44_000,
        channel_match_flagged=False,
        real_name="Pranil Sheth",
        verification=Verification.UNVERIFIED,
        weight=weight_for(Verification.UNVERIFIED),
        notes=(
            "Shares The FPL Wire's channel (see Zophar's note on the false-positive flag "
            "override). Real name Pranil Sheth — this resolves an ambiguity from the prior "
            "pass, which had only a single, unconfirmed source (an FFS profile page's "
            "structured-data link) tying him to a candidate ID. This pass independently "
            "corroborated 'Pranil Sheth' via 4+ unrelated sources (an FFS interview feature, a "
            "LinkedIn interview, a Sportskeeda author page, official Premier League articles, "
            "and his own Instagram bio, which even echoes the channel's exact self-reported "
            "stats). BUT both candidate IDs tried — 55085 (the old one, real name 'Kjartan "
            "Rørstadbotten') and 76913 (a new one, from a link embedded in the same FFS "
            "article that names him, real name 'SI GENDUT') — are REJECTED; neither matches "
            "'Pranil Sheth'. Self-reports 'Top 200 x3' in the channel description and his own "
            "Instagram bio (repeated consistently), but per the same judgment call noted on "
            "Zophar, left UNVERIFIED rather than promoted to DOCUMENTED since it's self-report, "
            "not third-party."
        ),
    ),
    Creator(
        creator_id="gianni-buttice",
        name="Gianni Buttice",
        youtube_hint="Gianni Buttice FPL",
        tier=Tier.CORE,
        channel_id="UCwoe4hkwLnjl7TAgcoeQgpA",
        channel_title="Gianni Butticè FPL",
        subscriber_count=46_200,
        real_name="Gianni Buttice",
        verification=Verification.DOCUMENTED,
        documented_finishes=_GIANNI_CLAIMS,
        weight=weight_for(Verification.DOCUMENTED, documented_finishes=_GIANNI_CLAIMS),
        notes=(
            "Treating 'Gianni Buttice' as his real name at medium-high confidence — it's used "
            "consistently as his published-author identity (his book 'FPL Diary') and on "
            "Premier League video credits, but no independent legal-name record (Companies "
            "House etc.) was found to fully confirm it isn't a pen name. No FPL ID found — the "
            "previously-rejected candidate (7685, 'Bob van Bilsen') remains correctly rejected. "
            "Found a self-published mini-league join code in his own video description "
            "(0ftdit) as a good-faith ownership signal, but it can't be resolved to a numeric "
            "ID without an authenticated FPL session. Documented tier rests on BBC's reporting."
        ),
    ),
    Creator(
        creator_id="fpl-heisenberg",
        name="FPL Heisenberg",
        youtube_hint="FPL Heisenberg",
        tier=Tier.CORE,
        channel_id="UCpEUTF5nW1uF7dfXiE387dA",
        channel_title="FPL Heisenberg",
        subscriber_count=227,
        real_name="Wes Prickett",
        verification=Verification.DOCUMENTED,
        documented_finishes=_HEISENBERG_CLAIMS,
        weight=weight_for(Verification.DOCUMENTED, documented_finishes=_HEISENBERG_CLAIMS),
        notes=(
            "Real name Wes Prickett confirmed (LinkedIn, BBC). Small personal channel (227 "
            "subs) despite the reputation — likely better known via BBC/Patreon appearances "
            "than his own channel. No FPL ID found (the one previously-checked URL, entry "
            "5665538, 404s on the live API). Documented tier rests on BBC's own reporting."
        ),
    ),
    # ---- Tier 2: sentiment radar / diversity picks (default weight regardless) ----
    Creator(
        creator_id="fpl-family",
        name="FPL Family",
        youtube_hint="FPL Family",
        tier=Tier.SECONDARY,
        channel_id="UCDG_EqOaaO1SSxEMZwfrSkg",
        channel_title="FPL Family",
        subscriber_count=28_800,
        real_name="Sam Bonfield",
        verification=Verification.UNVERIFIED,
        weight=weight_for(Verification.UNVERIFIED),
        notes=(
            "Not deep-verified this pass — tier 2 by design (sentiment/diversity pick), so "
            "default weight applies regardless of verification status. real_name is one half "
            "of the Lee & Sam duo: FFS's own Pro Pundits page independently names 'Sam "
            "Bonfield' as 'One half of FPL Family team', consistent with earlier research "
            "finding Sam writes for FFS."
        ),
    ),
    Creator(
        creator_id="fpl-salah",
        name="FPL Salah",
        youtube_hint="FPL Salah",
        tier=Tier.SECONDARY,
        channel_id=_FFH_CHANNEL_ID,
        channel_title="Fantasy Football Hub (shared — see notes)",
        subscriber_count=109_000,
        channel_match_flagged=True,
        real_name=None,
        verification=Verification.UNVERIFIED,
        weight=weight_for(Verification.UNVERIFIED),
        notes=_FFH_SHARED_NOTE
        + " Possibly 'Abdul Rehman' per an X bio, but not independently confirmed — left "
        "real_name unset rather than record an unconfirmed guess.",
    ),
    Creator(
        creator_id="fpl-hints",
        name="FPL Hints",
        youtube_hint="FPL Hints",
        tier=Tier.SECONDARY,
        channel_id="UCRaakOLYqsR9VcpylmmfuJQ",
        channel_title="FPL Hints",
        subscriber_count=2_400,
        verification=Verification.UNVERIFIED,
        weight=weight_for(Verification.UNVERIFIED),
        notes="Not deep-verified — tier 2 by design, default weight applies.",
    ),
    Creator(
        creator_id="fpl-matthew",
        name="FPL Matthew",
        youtube_hint="FPL Matthew",
        tier=Tier.SECONDARY,
        channel_id=_FFH_CHANNEL_ID,
        channel_title="Fantasy Football Hub (shared — see notes)",
        subscriber_count=109_000,
        channel_match_flagged=True,
        verification=Verification.UNVERIFIED,
        weight=weight_for(Verification.UNVERIFIED),
        notes=_FFH_SHARED_NOTE
        + " (A plain search hint for 'FPL Matthew' also false-positive-matches 'FPL Mate' — "
        "a completely unrelated, much bigger channel; don't reuse that match.)",
    ),
    Creator(
        creator_id="big-man-bakar",
        name="Big Man Bakar",
        youtube_hint="Big Man Bakar FPL",
        tier=Tier.SECONDARY,
        channel_id=_FFH_CHANNEL_ID,
        channel_title="Fantasy Football Hub (shared — see notes)",
        subscriber_count=109_000,
        channel_match_flagged=True,
        real_name="AbuBakar Siddiq (per X/Instagram bios)",
        verification=Verification.UNVERIFIED,
        weight=weight_for(Verification.UNVERIFIED),
        notes=_FFH_SHARED_NOTE
        + " Self-reports 'former FPL world #4' across socials — an extraordinary claim with "
        "no independent verification found; not scored on it (tier 2 default applies anyway).",
    ),
    Creator(
        creator_id="fpl-dylan",
        name="FPL Dylan",
        youtube_hint="FPL Dylan",
        tier=Tier.SECONDARY,
        channel_id="UCwt39viL_ZHxF1Ggk-_CrDw",
        channel_title="FPL Dylan",
        subscriber_count=39_600,
        verification=Verification.UNVERIFIED,
        weight=weight_for(Verification.UNVERIFIED),
        notes="Not deep-verified — tier 2 by design, default weight applies.",
    ),
    Creator(
        creator_id="planet-fpl",
        name="Planet FPL (Suj & James)",
        youtube_hint="Planet FPL",
        tier=Tier.SECONDARY,
        channel_id="UC8043oOKTB4uP8Nq15Kz6bg",
        channel_title="Planet FPL",
        subscriber_count=23_300,
        verification=Verification.UNVERIFIED,
        weight=weight_for(Verification.UNVERIFIED),
        notes="Not deep-verified — tier 2 by design, default weight applies.",
    ),
    Creator(
        creator_id="fpltips",
        name="FPLtips",
        youtube_hint="FPLtips",
        tier=Tier.SECONDARY,
        channel_id="UCVPb_jLxwaoYd-Dm7aSWQKQ",
        channel_title="FPLtips",
        subscriber_count=220_000,
        verification=Verification.UNVERIFIED,
        weight=weight_for(Verification.UNVERIFIED),
        notes="Not deep-verified — tier 2 by design, default weight applies.",
    ),
]
