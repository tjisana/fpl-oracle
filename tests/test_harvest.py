"""Unit tests for the SELF_CLAIMED harvest sweep: the pure
`is_season_review_candidate` title matcher and `HarvestManifestEntry` in
`fpl_oracle.roster.harvest`; the mechanical `quote_in_transcript`
whitespace-collapsing substring check in
`fpl_oracle.roster.claim_verify`; `verify_candidates` and
`render_claims_review` in `fpl_oracle.roster.claims`; and the
min-duration Shorts filter in `fpl_oracle.ingest.run_ingest`.

No network — every function under test here is pure.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fpl_oracle.ingest.run_ingest import MIN_DURATION_S, filter_min_duration
from fpl_oracle.ingest.transcripts import Transcript, TranscriptSegment
from fpl_oracle.ingest.youtube_client import VideoInfo
from fpl_oracle.roster.claim_verify import quote_in_transcript
from fpl_oracle.roster.claims import (
    RankClaimCandidate,
    render_claims_review,
    verify_candidates,
    video_url_at_timestamp,
)
from fpl_oracle.roster.harvest import _season_review_window_start, is_season_review_candidate


class TestIsSeasonReviewCandidatePositive:
    def test_season_review(self) -> None:
        assert is_season_review_candidate("MY SEASON REVIEW 2025/26")

    def test_my_season_in_review(self) -> None:
        assert is_season_review_candidate("My Season In Review | FPL 25/26")

    def test_how_i_finished(self) -> None:
        assert is_season_review_candidate("HOW I FINISHED THE 2025/26 SEASON")

    def test_rank_reveal(self) -> None:
        assert is_season_review_candidate("MY FINAL RANK REVEAL - FPL 25/26")

    def test_my_fpl_season(self) -> None:
        assert is_season_review_candidate("MY FPL SEASON: THE FULL STORY")

    def test_end_of_season(self) -> None:
        assert is_season_review_candidate("END OF SEASON THOUGHTS & RANK")

    def test_season_recap(self) -> None:
        assert is_season_review_candidate("2025/26 SEASON RECAP")

    def test_what_i_learned_this_season(self) -> None:
        assert is_season_review_candidate("WHAT I LEARNED THIS SEASON")

    def test_finishing_top_k(self) -> None:
        assert is_season_review_candidate("FINISHING IN THE TOP 10K THIS SEASON")

    def test_finishing_top_k_no_in_the(self) -> None:
        assert is_season_review_candidate("FINISHING TOP 10K")

    def test_case_insensitive(self) -> None:
        assert is_season_review_candidate("my season review, finally!")

    def test_season_opener_matches(self) -> None:
        # PLAN.md explicitly names "season-review / season-opener" videos
        # as harvest targets — matched deliberately, per the
        # recall-leaning design (see the function docstring): even a
        # preseason "season opener" video that turns out to have no rank
        # claim in it is a cheap false positive, filtered out at the
        # human-review stage rather than here.
        assert is_season_review_candidate("FPL SEASON OPENER - What I Learned")


class TestIsSeasonReviewCandidateNegative:
    def test_ordinary_gameweek_review_does_not_match(self) -> None:
        # A plain "review" of a single gameweek's results/transfers is
        # completely ordinary weekly content, not a season-level rank
        # claim — must NOT match on "review" alone.
        assert not is_season_review_candidate("GW38 REVIEW")

    def test_ordinary_team_selection_title_does_not_match(self) -> None:
        assert not is_season_review_candidate("My GW1 Team Reveal")

    def test_ordinary_transfer_video_does_not_match(self) -> None:
        assert not is_season_review_candidate("5 Transfers In For GW12")

    def test_wildcard_video_does_not_match(self) -> None:
        assert not is_season_review_candidate("WILDCARD TEAM REVEAL GW9")

    def test_plain_reveal_does_not_match(self) -> None:
        assert not is_season_review_candidate("Team Reveal - GW5")

    def test_empty_title_does_not_match(self) -> None:
        assert not is_season_review_candidate("")


class TestQuoteInTranscript:
    def test_exact_hit(self) -> None:
        transcript = "my final rank was 588th in the world this season"
        assert quote_in_transcript("my final rank was 588th in the world", transcript)

    def test_whitespace_mangled_hit(self) -> None:
        # Transcript segments carry arbitrary line breaks/extra spaces;
        # the quote itself might too (e.g. copied across a line wrap).
        transcript = "my final\nrank   was\n  588th in the\nworld this season"
        quote = "my final rank was   588th\nin the world"
        assert quote_in_transcript(quote, transcript)

    def test_near_miss_fails(self) -> None:
        transcript = "my final rank was 589th in the world this season"
        assert not quote_in_transcript("my final rank was 588th in the world", transcript)

    def test_paraphrase_fails(self) -> None:
        transcript = "i finished 588th overall in the world this year"
        assert not quote_in_transcript("my final rank was 588th in the world", transcript)

    def test_empty_quote_fails(self) -> None:
        assert not quote_in_transcript("", "any transcript text at all")

    def test_whitespace_only_quote_fails(self) -> None:
        assert not quote_in_transcript("   \n  ", "any transcript text at all")

    def test_case_insensitive(self) -> None:
        # Auto-generated transcripts are near-uniformly lowercase while
        # an extraction LLM tends to sentence-case a "verbatim" quote —
        # case-folding (but nothing fuzzier) keeps a genuinely verbatim
        # quote from flipping to UNVERIFIED for a reason unrelated to
        # whether the words are really there.
        assert quote_in_transcript("MY FINAL RANK", "my final rank was 588th")

    def test_mixed_case_verbatim_quote_verifies(self) -> None:
        transcript = "my final rank and where i finished for the 2025/26 season is 588th"
        quote = "My Final Rank And Where I Finished For The 2025/26 Season Is 588th"
        assert quote_in_transcript(quote, transcript)

    def test_punctuation_is_not_normalized(self) -> None:
        # Case-folding is mechanical enough to belong here; punctuation
        # normalization is deliberately NOT — that stays the extraction
        # prompt's job (see module docstring).
        transcript = "i finished 588th in the world this season"
        assert not quote_in_transcript("i finished 588th, in the world", transcript)


class TestSeasonReviewWindowStart:
    def test_returns_may_1_of_given_year(self) -> None:
        now = datetime(2026, 8, 18, tzinfo=UTC)
        assert _season_review_window_start(now) == datetime(2026, 5, 1, tzinfo=UTC)

    def test_defaults_to_current_time_when_omitted(self) -> None:
        result = _season_review_window_start()
        assert result.month == 5
        assert result.day == 1


class TestFilterMinDuration:
    def _video(self, video_id: str) -> VideoInfo:
        return VideoInfo(
            video_id=video_id,
            channel_id="UCabc",
            title=f"Video {video_id}",
            published_at=datetime(2026, 8, 1, tzinfo=UTC),
            description="",
        )

    def test_drops_short_video(self) -> None:
        videos = [self._video("short"), self._video("long")]
        durations = {"short": 45, "long": 600}
        result = filter_min_duration(videos, durations)
        assert [v.video_id for v in result] == ["long"]

    def test_keeps_video_at_exact_boundary(self) -> None:
        videos = [self._video("boundary")]
        durations = {"boundary": MIN_DURATION_S}
        assert filter_min_duration(videos, durations) == videos

    def test_missing_duration_is_kept_not_dropped(self) -> None:
        videos = [self._video("unknown")]
        result = filter_min_duration(videos, {})
        assert result == videos

    def test_custom_threshold(self) -> None:
        videos = [self._video("v1")]
        durations = {"v1": 100}
        assert filter_min_duration(videos, durations, min_duration_s=200) == []
        assert filter_min_duration(videos, durations, min_duration_s=50) == videos


class TestVideoUrlAtTimestamp:
    def test_builds_t_param(self) -> None:
        assert video_url_at_timestamp("abc123", 90) == "https://www.youtube.com/watch?v=abc123&t=90"


class TestVerifyCandidates:
    def _candidate(self, video_id: str, quote: str) -> RankClaimCandidate:
        return RankClaimCandidate(
            creator_id="andy",
            video_id=video_id,
            video_title="My Season Review",
            timestamp_s=120,
            quote=quote,
            claimed_season="2025/26",
            claimed_rank=588,
            claim_kind="overall_rank",
        )

    def _transcript(self, video_id: str, text: str) -> Transcript:
        return Transcript(
            video_id=video_id,
            language="en",
            is_generated=True,
            segments=[TranscriptSegment(text=text, start=0.0, duration=5.0)],
        )

    def test_verified_true_when_quote_present(self) -> None:
        candidate = self._candidate("v1", "588th in the world")
        transcripts = {"v1": self._transcript("v1", "i finished 588th in the world this year")}
        result = verify_candidates([candidate], transcripts)
        assert result[0].quote_verified is True

    def test_verified_false_when_quote_absent(self) -> None:
        candidate = self._candidate("v1", "588th in the world")
        transcripts = {"v1": self._transcript("v1", "i finished 589th in the world this year")}
        result = verify_candidates([candidate], transcripts)
        assert result[0].quote_verified is False

    def test_verified_false_when_transcript_missing(self) -> None:
        candidate = self._candidate("v1", "588th in the world")
        result = verify_candidates([candidate], {})
        assert result[0].quote_verified is False

    def test_original_candidate_list_untouched(self) -> None:
        candidate = self._candidate("v1", "588th in the world")
        transcripts = {"v1": self._transcript("v1", "i finished 588th in the world this year")}
        verify_candidates([candidate], transcripts)
        assert candidate.quote_verified is False


class TestRenderClaimsReview:
    def _candidate(
        self,
        creator_id: str,
        verified: bool,
        quote: str = "my final rank was 588th",
        published_at: datetime | None = None,
    ) -> RankClaimCandidate:
        return RankClaimCandidate(
            creator_id=creator_id,
            video_id="vid1",
            video_title="My Season Review",
            timestamp_s=90,
            quote=quote,
            claimed_season="2025/26",
            claimed_rank=588,
            claim_kind="overall_rank",
            quote_verified=verified,
            published_at=published_at,
        )

    def test_empty_candidates_says_so(self) -> None:
        rendered = render_claims_review([])
        assert "No candidates." in rendered

    def test_structure_has_creator_section_and_claim_fields(self) -> None:
        candidate = self._candidate("andy", verified=True)
        rendered = render_claims_review([candidate], creator_names={"andy": "Let's Talk FPL"})

        assert "## Let's Talk FPL (`andy`)" in rendered
        assert "### My Season Review" in rendered
        assert "VERIFIED" in rendered
        assert "2025/26" in rendered
        assert "588" in rendered
        assert "overall_rank" in rendered
        # URL is derived from video_id + timestamp_s (never stored) —
        # matches what video_url_at_timestamp("vid1", 90) builds.
        assert "[1:30](https://www.youtube.com/watch?v=vid1&t=90)" in rendered
        assert video_url_at_timestamp("vid1", 90) in rendered
        # Quote renders as its own blockquote line, not inline in the
        # "- Quote:" bullet.
        assert "- Quote:\n  > my final rank was 588th" in rendered
        assert "- [ ] Approve" in rendered

    def test_multiline_quote_collapsed_into_single_blockquote_line(self) -> None:
        candidate = self._candidate("andy", verified=True, quote="my final rank\nwas 588th")
        rendered = render_claims_review([candidate])
        assert "- Quote:\n  > my final rank was 588th" in rendered
        # The raw newline must not survive into the rendered file — it
        # would break the blockquote out of the list structure.
        assert "my final rank\nwas 588th" not in rendered

    def test_published_at_rendered_when_present(self) -> None:
        candidate = self._candidate(
            "andy", verified=True, published_at=datetime(2026, 5, 28, tzinfo=UTC)
        )
        rendered = render_claims_review([candidate])
        assert "- Video published: 2026-05-28" in rendered

    def test_published_at_omitted_when_absent(self) -> None:
        candidate = self._candidate("andy", verified=True, published_at=None)
        rendered = render_claims_review([candidate])
        assert "Video published" not in rendered

    def test_unverified_claim_flagged_for_manual_check(self) -> None:
        candidate = self._candidate("andy", verified=False)
        rendered = render_claims_review([candidate])
        assert "UNVERIFIED" in rendered

    def test_falls_back_to_creator_id_without_name_map(self) -> None:
        candidate = self._candidate("andy", verified=True)
        rendered = render_claims_review([candidate])
        assert "## andy (`andy`)" in rendered

    def test_multiple_creators_get_separate_sorted_sections(self) -> None:
        candidates = [
            self._candidate("zeta", verified=True),
            self._candidate("alpha", verified=True),
        ]
        rendered = render_claims_review(candidates)
        assert rendered.index("`alpha`") < rendered.index("`zeta`")

    def test_every_claim_gets_its_own_approve_checkbox(self) -> None:
        candidates = [
            self._candidate("andy", verified=True),
            self._candidate("andy", verified=False),
        ]
        rendered = render_claims_review(candidates)
        assert rendered.count("- [ ] Approve") == 2
