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

RESOLVED 2026-08-19 — 19 of 20 creators now have API-VERIFIED history.
The breakthrough was a technique, not a search: `entry/{id}/` returns a
manager's private leagues, and `leagues-classic/{lid}/standings/` returns
`league.admin_entry` — the creator who CREATED their own branded league
("youtube.com/FPLRaptor" -> its admin IS FPL Raptor). Seeded from one
self-disclosed ID, this snowballs across the creator ecosystem with no
name-guessing. Every ID here was pulled and confirmed against the LIVE
API, never from a web page (see the fabrication caution below).

The histories independently reproduce the claim ledger recorded earlier
from unrelated third-party sources — the strongest evidence the IDs are
right: Andy's 588 (plus his 1294 and 9975), Heisenberg's 836 (BBC),
Focal's 3405 (FFS), Gianni's "11 of 14 top-100k" and "eight top-50k"
(both exact), Holly's "two top-10k", Harry's "seven top-15k / five
consecutive top-10k", and Big Man Bakar's extraordinary "world #4"
claim, which this file previously flagged as unverifiable: TRUE, 2014/15,
rank 4.

Weights now come from `compute_weight()` on real multi-season history —
the API tier the system was designed around but had never been able to
exercise. This is recency-weighted (last 3 seasons dominate), unlike the
claim tiers' best-ever + consistency scoring, so it re-ranks the roster
substantially. Two corrections it forced:

- FPL BlackBox: the seed's "multiple top-10k finishes" was RIGHT and the
  FFS reading was wrong. Entry 252 (Mark Sutherns) has finishes of 42,
  115, 268, 382, 1367, 2044, 2061, 2217, 2392, 3285 across 20 seasons.
  Separately, the harvest sweep extracted a "1.1 million" rank from a
  BlackBox season-review video and attributed it to Mark — it is his
  CO-HOST Az Phillips (entry 246, 2025/26 = 1,148,135). A shared-channel
  speaker-attribution miss the quote-anchoring rule cannot catch, since
  the quote was genuinely spoken; only the API data disambiguated it.
- FPL Harry: the FFS-article attribution of entry 1320 is WRONG (that is
  Zinedine Bashir). 3054 is correct, and it also confirms the real name
  "Harry Daniels" this file had held only as an unverified guess.

The claim-tier evidence (`documented_finishes` / `self_claimed_finishes`)
is deliberately RETAINED below even where API history supersedes it: it
documents provenance, and it now serves as corroboration of the IDs.

FPL Dylan is the one creator with no discoverable entry — not in any
creator league reached by the crawl, no entry link in his descriptions.
He remains UNVERIFIED at the default weight.

CORRECTING AN EARLIER THEORY IN THIS FILE: a previous pass concluded FPL
entry IDs are assigned per-season and are "not a persistent per-person
identifier". The data gathered here refutes that — every ID below returns
one manager's whole career under a single ID (252 goes back to 2006/07,
20 seasons; 41 covers 16). IDs are persistent per account. The old
observation that stale article IDs resolve to strangers is better
explained by those sources simply being WRONG (one such table was found
to be fabricated outright, and the FFS attribution of entry 1320 to FPL
Harry is demonstrably a different person) than by ID recycling.

Consequence: these IDs do NOT need re-verifying every August. Re-pull the
histories to pick up a new season, and re-check an ID only if its
`entry/{id}/` name stops matching the creator.

Institutional brands (Fantasy Football Scout, Fantasy Football Hub) were
dropped entirely per the owner's instruction — no person, no track
record, no Tier.CORE claim. Three "creators" (FPL Salah, FPL Matthew,
Big Man Bakar) turned out to be recurring on-camera personas whose
content is actually produced on Fantasy Football Hub's own channel, not
a personal one — flagged below; ingestion will need title-based
filtering, not a per-creator channel_id, if picked up later.

A caution surfaced this pass, worth keeping in mind generally: at least
one web-search/fetch tool call returned a plausible-looking table of FPL
entry IDs that turned out to be entirely fabricated (every ID in it
resolved to an unrelated manager on the live API). Every ID anywhere in
this file was checked against the live API directly — but the lesson is
to never trust a numeric FPL ID from search/fetch summarization without
that live check, even when it's presented with citations.
"""

from fpl_oracle.roster.models import ClaimedFinish, Creator, PastFinish, Tier, Verification
from fpl_oracle.roster.weights import weight_for

_BBC_FPL_EXPERTS = "https://feeds.bbci.co.uk/sport/articles/c4gj7pl3p82o"
_FFS_PRO_PUNDITS = "https://www.fantasyfootballscout.co.uk/the-ffs-pro-pundits"
_ANDY_SEASON_REVIEW = "https://www.youtube.com/watch?v=UW85Hel20QE"
_RAPTOR_SEASON_GUIDE = "https://www.youtube.com/watch?v=c1MyHESd5pA&t=111"
_HARRY_SEASON_REVIEW = "https://www.youtube.com/watch?v=lNnB4-KxC_A&t=259"


def _past(spec: str) -> list[PastFinish]:
    """Parse a compact "season:rank season:rank" string into PastFinish rows.

    The histories below were pulled LIVE from `entry/{id}/history/` on
    2026-08-19 (see each creator's `fpl_team_id`) — they are API facts, not
    claims. Kept in this compact form so ~300 season rows stay readable.
    A rank of "-" means the API returned no rank for that season (it can
    return null/0 — e.g. a season the manager did not play). Recorded
    rather than dropped, so a re-pull round-trips exactly; `compute_weight`
    skips them instead of scoring them as a worst-possible finish.
    """
    return [
        PastFinish(season_name=season, rank=None if rank == "-" else int(rank))
        for season, rank in (tok.split(":") for tok in spec.split())
    ]


_API_HISTORY: dict[str, list[PastFinish]] = {
    "lets-talk-fpl": _past(
        "2010/11:20698 2011/12:10463 2012/13:15557 2013/14:104345 2014/15:8492 "
        "2015/16:92554 2016/17:4935 2017/18:21436 2018/19:12731 2019/20:162555 "
        "2020/21:1294 2021/22:9975 2022/23:35711 2023/24:44067 2024/25:77007 "
        "2025/26:588"
    ),
    "fpl-harry": _past(
        "2016/17:10979 2017/18:752900 2018/19:13845 2019/20:125929 2020/21:3819 "
        "2021/22:1345 2022/23:510 2023/24:5401 2024/25:6470 2025/26:26743"
    ),
    "fpl-raptor": _past(
        "2020/21:129753 2021/22:35078 2022/23:10198 2023/24:29860 2024/25:12592 2025/26:530214"
    ),
    "pras": _past(
        "2010/11:548037 2011/12:136098 2012/13:6378 2013/14:56696 2014/15:8682 "
        "2015/16:90972 2016/17:75129 2017/18:22677 2018/19:4252 2019/20:32282 "
        "2020/21:11549 2021/22:4841 2022/23:37971 2023/24:21656 2024/25:4184 "
        "2025/26:192790"
    ),
    "fpl-mate": _past(
        "2012/13:1333371 2013/14:1822004 2015/16:210654 2016/17:462777 "
        "2017/18:4423610 2018/19:1377380 2019/20:62690 2020/21:119546 2021/22:243 "
        "2022/23:34863 2023/24:126994 2024/25:355412 2025/26:123422"
    ),
    "fpl-focal": _past(
        "2011/12:529142 2012/13:270849 2013/14:22327 2014/15:43767 2015/16:89065 "
        "2016/17:155308 2017/18:41632 2018/19:104353 2019/20:80619 2020/21:53719 "
        "2021/22:17391 2022/23:- 2023/24:49772 2024/25:3405 2025/26:25669"
    ),
    "holly-shand": _past(
        "2014/15:170951 2015/16:5707 2016/17:88806 2017/18:282648 2018/19:6720 "
        "2019/20:67905 2020/21:35354 2021/22:93929 2022/23:156378 2023/24:222633 "
        "2024/25:77007 2025/26:240001"
    ),
    "fpl-blackbox": _past(
        "2006/07:2217 2007/08:2392 2008/09:268 2009/10:29460 2010/11:3285 2011/12:382 "
        "2012/13:2044 2013/14:192837 2014/15:42 2015/16:28329 2016/17:115 "
        "2017/18:11458 2018/19:1367 2019/20:177309 2020/21:2061 2021/22:156321 "
        "2022/23:145833 2023/24:12945 2024/25:76358 2025/26:11980"
    ),
    "zophar": _past(
        "2009/10:4142 2010/11:17 2011/12:7627 2012/13:1792 2013/14:3749 2014/15:1951 "
        "2015/16:3229 2016/17:30179 2017/18:16075 2018/19:25294 2019/20:52916 "
        "2020/21:15625 2021/22:14668 2022/23:78749 2023/24:6799 2024/25:284767 "
        "2025/26:140701"
    ),
    "lateriser": _past(
        "2008/09:632836 2009/10:2084714 2010/11:2403966 2011/12:132668 2012/13:189 "
        "2013/14:2212 2015/16:77 2016/17:49211 2017/18:8621 2018/19:12202 2019/20:30 "
        "2020/21:678991 2021/22:1047 2022/23:117524 2023/24:173729 2024/25:435772 "
        "2025/26:835102"
    ),
    "gianni-buttice": _past(
        "2009/10:135956 2010/11:342944 2011/12:46453 2012/13:15283 2013/14:68213 "
        "2014/15:18795 2015/16:57186 2016/17:20793 2017/18:20774 2018/19:385976 "
        "2019/20:25622 2020/21:393344 2021/22:16235 2022/23:84832 2023/24:47328 "
        "2024/25:560294 2025/26:142459"
    ),
    "fpl-heisenberg": _past(
        "2013/14:202551 2014/15:20643 2015/16:31659 2016/17:9239 2017/18:836 "
        "2018/19:18760 2019/20:25529 2020/21:7619 2021/22:105292 2022/23:112819 "
        "2023/24:180809 2024/25:358569 2025/26:40323"
    ),
    "fpl-family": _past(
        "2007/08:60606 2008/09:1732 2009/10:4439 2010/11:83121 2011/12:8036 "
        "2012/13:3510 2013/14:24822 2014/15:11993 2015/16:70717 2016/17:73956 "
        "2017/18:204457 2018/19:68818 2019/20:66286 2020/21:69166 2021/22:115082 "
        "2022/23:48301 2023/24:83883 2024/25:119594 2025/26:183491"
    ),
    "fpl-salah": _past(
        "2007/08:221345 2008/09:36391 2009/10:604 2010/11:952 2011/12:47454 "
        "2012/13:20509 2013/14:11877 2014/15:859 2015/16:29602 2016/17:138935 "
        "2017/18:12302 2018/19:709 2019/20:19682 2020/21:4991 2021/22:3231 "
        "2022/23:103038 2023/24:48272 2024/25:70089 2025/26:425078"
    ),
    "fpl-hints": _past(
        "2007/08:754930 2010/11:82141 2011/12:110 2012/13:5038 2013/14:34913 "
        "2014/15:37515 2015/16:261424 2016/17:43184 2017/18:67977 2018/19:604565 "
        "2019/20:284540 2020/21:4918 2021/22:4759 2022/23:113326 2023/24:622907 "
        "2024/25:482412 2025/26:196475"
    ),
    "fpl-matthew": _past(
        "2009/10:15535 2010/11:434 2011/12:9076 2012/13:436 2013/14:1032 2014/15:1565 "
        "2015/16:5272 2016/17:367 2017/18:11009 2018/19:9864 2019/20:71107 "
        "2020/21:29128 2021/22:23021 2022/23:58817 2023/24:181584 2024/25:13608 "
        "2025/26:19166"
    ),
    "big-man-bakar": _past(
        "2010/11:363660 2011/12:71500 2012/13:20351 2013/14:93165 2014/15:4 "
        "2015/16:176402 2016/17:1157 2017/18:101917 2018/19:66896 2019/20:68757 "
        "2020/21:111827 2021/22:3735 2022/23:37086 2023/24:14594 2024/25:33564 "
        "2025/26:839"
    ),
    "planet-fpl": _past(
        "2014/15:504436 2015/16:335222 2016/17:21479 2017/18:197611 2018/19:164873 "
        "2019/20:149826 2020/21:343289 2021/22:82927 2022/23:224364 2023/24:113903 "
        "2024/25:51340 2025/26:32962"
    ),
    "fpltips": _past(
        "2013/14:69644 2014/15:7498 2015/16:5076 2016/17:1881 2017/18:28103 "
        "2018/19:68706 2019/20:75570 2020/21:5895 2021/22:92680 2022/23:61691 "
        "2023/24:68475 2024/25:3834 2025/26:7242"
    ),
}


_HARRY_SELF_CLAIMS = [
    ClaimedFinish(
        description="five consecutive top-10k finishes (the five seasons preceding 2025/26), "
        "stated in his own season-review video at 4:19 — exact phrase: 'i finished in the top "
        "10k every single season for the past five years before this one'. Quote verified as an "
        "exact substring of the stored transcript by the harvest sweep (roster.claim_verify). "
        "CONSISTENCY (not corroboration): the Phase 1 research pass separately recorded him "
        "self-reporting 'seven top-15k, five consecutive top-10k' elsewhere. Both trace back "
        "to Harry himself, so this is the same self-report restated in two places, NOT "
        "third-party verification — it shows he has not inflated the number recently, and "
        "nothing more. No third party has attributed any finish to him.",
        rank=10_000,
        source_url=_HARRY_SEASON_REVIEW,
        count=5,
    ),
    ClaimedFinish(
        description="~26,000th overall, 2025/26 season — exact phrase: 'five top 10k finishes "
        "and now a 26k, which is still great'. CREDIBILITY NOTE (argues FOR the tier, not "
        "against): he volunteers his bad seasons unprompted in the same video — a 750k worst "
        "season (2017/18) and a first season 'just outside the top 10K' (2016/17) — and is "
        "careful to say 8 more points would have made 23k, i.e. that this was NOT a top-25k "
        "finish. Precision against his own interest, the opposite of self-report inflation.",
        rank=26_000,
        source_url=_HARRY_SEASON_REVIEW,
        count=1,
    ),
]

_RAPTOR_SELF_CLAIMS = [
    ClaimedFinish(
        description="four consecutive top-35k finishes (the four seasons preceding 2025/26), "
        "stated in his own end-of-season video at 1:51 — exact phrase: 'the previous four "
        "seasons, as you can see above my head, i finished inside the top 35k'. Quote "
        "verified as an exact substring of the stored transcript by the harvest sweep "
        "(roster.claim_verify). INTERPRETATION CAVEAT the tier doesn't price in: he is "
        "reading off an on-screen graphic, so the transcript corroborates that he SAID it, "
        "not the ranks displayed; and 'inside the top 35k' is a band, so 35,000 is the "
        "conservative representative value, not an exact rank.",
        rank=35_000,
        source_url=_RAPTOR_SEASON_GUIDE,
        count=4,
    ),
]

_ANDY_SELF_CLAIMS = [
    ClaimedFinish(
        description="588th overall, 2025/26 season (stated in his own season-review video; "
        "corroborated 2026-08-17 against the video's own auto-generated transcript via "
        "ingest.transcripts — exact phrase: 'my final rank and where i finished for the "
        "2025/26 season is 588th in the world')",
        rank=588,
        source_url=_ANDY_SEASON_REVIEW,
        count=1,
    ),
    ClaimedFinish(
        description="four further top-10k finishes across his 16 seasons (five total per "
        "his own recap, minus the 588 already claimed above to avoid double-counting; "
        "previous best 1,294, another at 9,975; one more just outside at ~10,400 NOT "
        "counted). Same season-review transcript as above. Honesty note the claim tier "
        "doesn't price in: his most recent pre-588 run was declining (35k, 44k, 77k) — "
        "claim-based weights score best + consistency with no recency term, same as "
        "every other claim-tier creator.",
        rank=10_000,
        source_url=_ANDY_SEASON_REVIEW,
        count=4,
    ),
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
        fpl_team_id=41,
        verification=Verification.API,
        self_claimed_finishes=_ANDY_SELF_CLAIMS,
        past_finishes=_API_HISTORY["lets-talk-fpl"],
        weight=weight_for(Verification.API, past_finishes=_API_HISTORY["lets-talk-fpl"]),
        notes=(
            "Real name Andy Mears — corroborated by 3 independent sources (a podcast interview "
            "title, his solo.to link page, and his personal YouTube handle @andymears501). No "
            "FPL entry ID found/verified despite dedicated searching (entry 40, the one prior "
            "candidate, is confirmed unrelated — Korn Supatrabutra). SELF_CLAIMED tier (not "
            "DOCUMENTED): the 588th-overall claim comes from his own season-review video, not "
            "a third party, so it's scored on the steeper SELF_CLAIMED_SHRINK to account for "
            "self-report selection bias; the claim is otherwise elite and specific enough to "
            "keep him at Tier.CORE. See the sourcing caveat on the claim itself above."
        ),
    ),
    Creator(
        creator_id="fpl-harry",
        name="FPL Harry",
        youtube_hint="FPL Harry",
        tier=Tier.CORE,
        channel_id="UCcPWnCj5AKC19HaySZjb25g",
        channel_title="FPL Harry",
        subscriber_count=233_000,
        real_name="Harry Daniels",
        fpl_team_id=3054,
        verification=Verification.API,
        self_claimed_finishes=_HARRY_SELF_CLAIMS,
        past_finishes=_API_HISTORY["fpl-harry"],
        weight=weight_for(Verification.API, past_finishes=_API_HISTORY["fpl-harry"]),
        notes=(
            "Promoted UNVERIFIED -> SELF_CLAIMED by the harvest sweep (owner-approved "
            "2026-08-19); his own season-review video claims five consecutive top-10k "
            "finishes plus a ~26k 2025/26. Entry-ID research below is unchanged and still "
            "unresolved. NEAR MISS, worth a human follow-up: real name unconfirmed (a business email "
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
        tier=Tier.CORE,
        channel_id="UC54QLWzsMifTRjNQ02z5pCw",
        channel_title="FPL Raptor",
        subscriber_count=178_000,
        real_name="Ross Dowsett",
        fpl_team_id=199,
        verification=Verification.API,
        self_claimed_finishes=_RAPTOR_SELF_CLAIMS,
        past_finishes=_API_HISTORY["fpl-raptor"],
        weight=weight_for(Verification.API, past_finishes=_API_HISTORY["fpl-raptor"]),
        notes=(
            "Real name Ross Dowsett, confirmed independently (his own X account @ross_dowsett, "
            "a 2021 FFS interview, a Fantasy Football Fix contributor bio). No FPL ID found "
            "under that name; the two previously-rejected candidates (18675 = a spam account, "
            "746 = unrelated manager 'A. Almarzooqi') remain correctly rejected. Promoted "
            "UNVERIFIED -> SELF_CLAIMED by the harvest sweep (owner-approved 2026-08-19): his "
            "own end-of-season video claims four straight top-35k finishes."
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
        fpl_team_id=3315,
        verification=Verification.API,
        documented_finishes=_PRAS_CLAIMS,
        past_finishes=_API_HISTORY["pras"],
        weight=weight_for(Verification.API, past_finishes=_API_HISTORY["pras"]),
        channel_primary=True,
        notes=(
            "Real name Prasun Singhal, well corroborated (FFS 'Meet the Manager' feature, an "
            "official FPL/Premier League Facebook video caption, an FFH team-reveal article, "
            "BBC). No FPL ID found under that name despite checking his FFS interview article "
            "and his co-hosts' shared channel for an embedded entry link. `channel_primary` on "
            "The FPL Wire: episodes are joint discussions among Pras, Zophar, and Lateriser, "
            "not attributable to one co-host by title, so they're ingested once under Pras's "
            "weight rather than skipped or triple-counted. A blended pod weight across all "
            "three co-hosts is a v2 idea, not implemented here."
        ),
    ),
    Creator(
        creator_id="fpl-mate",
        name="FPL Mate",
        youtube_hint="FPL Mate",
        tier=Tier.CORE,
        channel_id="UCweDAlFm2LnVcOqaFU4_AGA",
        channel_title="FPL Mate",
        subscriber_count=256_000,
        real_name="Dan (surname unconfirmed)",
        fpl_team_id=120,
        verification=Verification.API,
        past_finishes=_API_HISTORY["fpl-mate"],
        weight=weight_for(Verification.API, past_finishes=_API_HISTORY["fpl-mate"]),
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
        fpl_team_id=298,
        verification=Verification.API,
        documented_finishes=_FOCAL_CLAIMS,
        past_finishes=_API_HISTORY["fpl-focal"],
        weight=weight_for(Verification.API, past_finishes=_API_HISTORY["fpl-focal"]),
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
        fpl_team_id=70063,
        verification=Verification.API,
        documented_finishes=_HOLLY_CLAIMS,
        past_finishes=_API_HISTORY["holly-shand"],
        weight=weight_for(Verification.API, past_finishes=_API_HISTORY["holly-shand"]),
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
        tier=Tier.CORE,
        channel_id="UCGJ8-xqhOLwyJNuPMsVoQWQ",
        channel_title="FPL BlackBox",
        subscriber_count=37_900,
        real_name="Mark Sutherns",
        fpl_team_id=252,
        verification=Verification.API,
        past_finishes=_API_HISTORY["fpl-blackbox"],
        weight=weight_for(Verification.API, past_finishes=_API_HISTORY["fpl-blackbox"]),
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
        tier=Tier.CORE,
        channel_id="UCtIPFexB6PLKNNl0XH3SKKw",
        channel_title="The FPL Wire - Fantasy Premier League",
        subscriber_count=44_000,
        channel_match_flagged=False,
        real_name="Utkarsh Dalmia",
        fpl_team_id=2177,
        verification=Verification.API,
        past_finishes=_API_HISTORY["zophar"],
        weight=weight_for(Verification.API, past_finishes=_API_HISTORY["zophar"]),
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
            "season-review video was treated (see his entry above). Not independently "
            "ingested for transcripts — The FPL Wire's episodes are joint discussions with no "
            "reliable per-co-host title split, so content flows in via the shared channel's "
            "`channel_primary` (Pras) instead."
        ),
    ),
    Creator(
        creator_id="lateriser",
        name="Lateriser",
        youtube_hint="Lateriser FPL",
        tier=Tier.CORE,
        channel_id="UCtIPFexB6PLKNNl0XH3SKKw",
        channel_title="The FPL Wire - Fantasy Premier League",
        subscriber_count=44_000,
        channel_match_flagged=False,
        real_name="Pranil Sheth",
        fpl_team_id=6816,
        verification=Verification.API,
        past_finishes=_API_HISTORY["lateriser"],
        weight=weight_for(Verification.API, past_finishes=_API_HISTORY["lateriser"]),
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
            "not third-party. Not independently ingested for transcripts — The FPL Wire's "
            "episodes are joint discussions with no reliable per-co-host title split, so "
            "content flows in via the shared channel's `channel_primary` (Pras) instead."
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
        fpl_team_id=2375,
        verification=Verification.API,
        documented_finishes=_GIANNI_CLAIMS,
        past_finishes=_API_HISTORY["gianni-buttice"],
        weight=weight_for(Verification.API, past_finishes=_API_HISTORY["gianni-buttice"]),
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
        fpl_team_id=16070,
        verification=Verification.API,
        documented_finishes=_HEISENBERG_CLAIMS,
        past_finishes=_API_HISTORY["fpl-heisenberg"],
        weight=weight_for(Verification.API, past_finishes=_API_HISTORY["fpl-heisenberg"]),
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
        tier=Tier.CORE,
        channel_id="UCDG_EqOaaO1SSxEMZwfrSkg",
        channel_title="FPL Family",
        subscriber_count=28_800,
        real_name="Lee Bonfield",
        fpl_team_id=2913,
        verification=Verification.API,
        past_finishes=_API_HISTORY["fpl-family"],
        weight=weight_for(Verification.API, past_finishes=_API_HISTORY["fpl-family"]),
        notes=(
            "DUO — the weight is LEE Bonfield's verified history (entry 2913), not the channel's combined output. Sam's own entry (2977) was checked and is weaker (0.198 vs Lee's 0.264); the better of the two was taken, per the owner. Picks spoken by Sam therefore carry Lee's weight — a known approximation. Not deep-verified this pass — tier 2 by design (sentiment/diversity pick), so "
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
        tier=Tier.CORE,
        channel_id=_FFH_CHANNEL_ID,
        channel_title="Fantasy Football Hub (shared — see notes)",
        subscriber_count=109_000,
        channel_match_flagged=True,
        real_name="Abdul Rehman",
        fpl_team_id=70,
        verification=Verification.API,
        past_finishes=_API_HISTORY["fpl-salah"],
        weight=weight_for(Verification.API, past_finishes=_API_HISTORY["fpl-salah"]),
        title_filter="FPL Salah",
        notes=_FFH_SHARED_NOTE
        + " Possibly 'Abdul Rehman' per an X bio, but not independently confirmed — left "
        "real_name unset rather than record an unconfirmed guess. `title_filter='FPL Salah'` "
        "attributes FFH videos to him — his persona name is distinctive enough not to "
        "false-positive against his co-personas' names.",
    ),
    Creator(
        creator_id="fpl-hints",
        name="FPL Hints",
        youtube_hint="FPL Hints",
        tier=Tier.CORE,
        channel_id="UCRaakOLYqsR9VcpylmmfuJQ",
        channel_title="FPL Hints",
        subscriber_count=2_400,
        fpl_team_id=405,
        verification=Verification.API,
        past_finishes=_API_HISTORY["fpl-hints"],
        weight=weight_for(Verification.API, past_finishes=_API_HISTORY["fpl-hints"]),
        notes="Seeded as a tier-2 diversity pick; now API-verified (see fpl_team_id) and weighted on real history like everyone else.",
    ),
    Creator(
        creator_id="fpl-matthew",
        name="FPL Matthew",
        youtube_hint="FPL Matthew",
        tier=Tier.CORE,
        channel_id=_FFH_CHANNEL_ID,
        channel_title="Fantasy Football Hub (shared — see notes)",
        subscriber_count=109_000,
        channel_match_flagged=True,
        fpl_team_id=18124,
        verification=Verification.API,
        past_finishes=_API_HISTORY["fpl-matthew"],
        weight=weight_for(Verification.API, past_finishes=_API_HISTORY["fpl-matthew"]),
        title_filter="FPL Matthew",
        notes=_FFH_SHARED_NOTE
        + " (A plain search hint for 'FPL Matthew' also false-positive-matches 'FPL Mate' — "
        "a completely unrelated, much bigger channel; don't reuse that match.) "
        "`title_filter='FPL Matthew'` is scoped to FFH-channel video titles only, so it's "
        "safe here despite the FPL Mate collision risk in a plain search/description context.",
    ),
    Creator(
        creator_id="big-man-bakar",
        name="Big Man Bakar",
        youtube_hint="Big Man Bakar FPL",
        tier=Tier.CORE,
        channel_id=_FFH_CHANNEL_ID,
        channel_title="Fantasy Football Hub (shared — see notes)",
        subscriber_count=109_000,
        channel_match_flagged=True,
        real_name="AbuBakar Siddiq (per X/Instagram bios)",
        fpl_team_id=5133,
        verification=Verification.API,
        past_finishes=_API_HISTORY["big-man-bakar"],
        weight=weight_for(Verification.API, past_finishes=_API_HISTORY["big-man-bakar"]),
        title_filter="Bakar",
        notes=_FFH_SHARED_NOTE
        + " Self-reports 'former FPL world #4' across socials — an extraordinary claim with "
        "no independent verification found; not scored on it (tier 2 default applies anyway). "
        "`title_filter='Bakar'` (not the full 'Big Man Bakar') — his own display name is "
        "'Big Man Bakar' but titles more reliably carry just 'Bakar' as the persona tag; "
        "doesn't collide with 'FPL Salah' or 'FPL Matthew'.",
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
        notes="Seeded as a tier-2 diversity pick; now API-verified (see fpl_team_id) and weighted on real history like everyone else.",
    ),
    Creator(
        creator_id="planet-fpl",
        name="Planet FPL (Suj & James)",
        youtube_hint="Planet FPL",
        tier=Tier.CORE,
        channel_id="UC8043oOKTB4uP8Nq15Kz6bg",
        channel_title="Planet FPL",
        subscriber_count=23_300,
        real_name="James Linden",
        fpl_team_id=1194,
        verification=Verification.API,
        past_finishes=_API_HISTORY["planet-fpl"],
        weight=weight_for(Verification.API, past_finishes=_API_HISTORY["planet-fpl"]),
        notes="Seeded as a tier-2 diversity pick; now API-verified (see fpl_team_id) and weighted on real history like everyone else.",
    ),
    Creator(
        creator_id="fpltips",
        name="FPLtips",
        youtube_hint="FPLtips",
        tier=Tier.CORE,
        channel_id="UCVPb_jLxwaoYd-Dm7aSWQKQ",
        channel_title="FPLtips",
        subscriber_count=220_000,
        fpl_team_id=1527,
        verification=Verification.API,
        past_finishes=_API_HISTORY["fpltips"],
        weight=weight_for(Verification.API, past_finishes=_API_HISTORY["fpltips"]),
        notes="Seeded as a tier-2 diversity pick; now API-verified (see fpl_team_id) and weighted on real history like everyone else.",
    ),
]
