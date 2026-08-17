"""Unit tests for shared-channel video attribution: the pure
`videos_for_creator` function in `fpl_oracle.ingest.run_ingest`, plus
registry-level invariants over the attribution fields on `Creator`."""

from __future__ import annotations

from datetime import UTC, datetime

from fpl_oracle.ingest.run_ingest import eligible_creators, videos_for_creator
from fpl_oracle.ingest.youtube_client import VideoInfo
from fpl_oracle.roster.models import Creator, Tier
from fpl_oracle.roster.registry import REGISTRY

_CHANNEL_ID = "UCshared"


def _video(video_id: str, title: str) -> VideoInfo:
    return VideoInfo(
        video_id=video_id,
        channel_id=_CHANNEL_ID,
        title=title,
        published_at=datetime(2026, 8, 1, tzinfo=UTC),
        description="",
    )


def _creator(creator_id: str, **kwargs) -> Creator:
    return Creator(
        creator_id=creator_id,
        name=creator_id,
        youtube_hint=creator_id,
        tier=Tier.SECONDARY,
        channel_id=_CHANNEL_ID,
        **kwargs,
    )


class TestVideosForCreatorSoleOwner:
    def test_sole_owner_gets_all_videos_unfiltered(self) -> None:
        creator = _creator("solo")
        videos = [_video("v1", "My GW1 Team"), _video("v2", "Random title")]
        assert videos_for_creator(creator, [], videos) == videos


class TestVideosForCreatorTitleFilter:
    def test_filter_match_goes_to_the_persona(self) -> None:
        salah = _creator("fpl-salah", title_filter="FPL Salah")
        matthew = _creator("fpl-matthew", title_filter="FPL Matthew")
        videos = [_video("v1", "FPL Salah's GW1 Team Reveal")]

        assert videos_for_creator(salah, [matthew], videos) == videos
        assert videos_for_creator(matthew, [salah], videos) == []

    def test_case_insensitive_match(self) -> None:
        salah = _creator("fpl-salah", title_filter="FPL Salah")
        videos = [_video("v1", "fpl salah gw1 reveal")]
        assert videos_for_creator(salah, [], videos) == videos

    def test_title_matching_two_filters_is_skipped_not_given_to_either(self) -> None:
        salah = _creator("fpl-salah", title_filter="FPL Salah")
        matthew = _creator("fpl-matthew", title_filter="FPL Matthew")
        videos = [_video("v1", "FPL Salah & FPL Matthew: GW1 Special")]

        assert videos_for_creator(salah, [matthew], videos) == []
        assert videos_for_creator(matthew, [salah], videos) == []

    def test_unmatched_video_on_filtered_channel_with_no_primary_is_skipped(self) -> None:
        salah = _creator("fpl-salah", title_filter="FPL Salah")
        matthew = _creator("fpl-matthew", title_filter="FPL Matthew")
        videos = [_video("v1", "Some unrelated GW1 chat")]

        assert videos_for_creator(salah, [matthew], videos) == []
        assert videos_for_creator(matthew, [salah], videos) == []


class TestVideosForCreatorChannelPrimary:
    def test_primary_gets_unmatched_videos(self) -> None:
        primary = _creator("primary", channel_primary=True)
        persona = _creator("persona", title_filter="Persona Name")
        videos = [
            _video("v1", "Persona Name's episode"),
            _video("v2", "Regular joint episode"),
        ]

        assert videos_for_creator(primary, [persona], videos) == [videos[1]]

    def test_primary_gets_everything_when_no_co_creator_filters(self) -> None:
        primary = _creator("primary", channel_primary=True)
        co_host = _creator("co-host")  # no title_filter, no channel_primary
        videos = [_video("v1", "Episode 1"), _video("v2", "Episode 2")]

        assert videos_for_creator(primary, [co_host], videos) == videos

    def test_primary_does_not_get_video_matching_multiple_filters(self) -> None:
        # A video ambiguous between two personas goes to nobody, not to
        # the primary either.
        primary = _creator("primary", channel_primary=True)
        salah = _creator("fpl-salah", title_filter="FPL Salah")
        matthew = _creator("fpl-matthew", title_filter="FPL Matthew")
        videos = [_video("v1", "FPL Salah & FPL Matthew: GW1 Special")]

        assert videos_for_creator(primary, [salah, matthew], videos) == []

    def test_case_insensitive_claim_check(self) -> None:
        primary = _creator("primary", channel_primary=True)
        persona = _creator("persona", title_filter="Persona Name")
        videos = [_video("v1", "persona name's episode")]

        assert videos_for_creator(primary, [persona], videos) == []


class TestEligibleCreatorsSharedChannel:
    def test_channel_primary_is_eligible(self) -> None:
        primary = _creator("primary", channel_primary=True)
        co_host = _creator("co-host")
        result = eligible_creators([primary, co_host])
        assert primary in result
        assert co_host not in result

    def test_title_filter_creator_is_eligible(self) -> None:
        persona = _creator("persona", title_filter="Persona Name")
        co_host = _creator("co-host")
        result = eligible_creators([persona, co_host])
        assert persona in result
        assert co_host not in result

    def test_plain_co_host_with_neither_is_excluded(self) -> None:
        primary = _creator("primary", channel_primary=True)
        co_host_a = _creator("co-host-a")
        co_host_b = _creator("co-host-b")
        result = eligible_creators([primary, co_host_a, co_host_b])
        assert result == [primary]

    def test_sole_owner_channel_still_eligible(self) -> None:
        solo = Creator(
            creator_id="solo",
            name="Solo",
            youtube_hint="Solo",
            tier=Tier.SECONDARY,
            channel_id="UConly",
        )
        assert eligible_creators([solo]) == [solo]


class TestRegistryAttributionInvariants:
    """Sanity checks on the real registry's shared-channel setup, not just
    the pure function in isolation."""

    def test_fpl_wire_channel_has_exactly_one_primary(self) -> None:
        fpl_wire_channel_id = "UCtIPFexB6PLKNNl0XH3SKKw"
        primaries = [
            c for c in REGISTRY if c.channel_id == fpl_wire_channel_id and c.channel_primary
        ]
        assert len(primaries) == 1
        assert primaries[0].creator_id == "pras"

    def test_every_ffh_persona_has_a_title_filter(self) -> None:
        ffh_persona_ids = {"fpl-salah", "fpl-matthew", "big-man-bakar"}
        personas = [c for c in REGISTRY if c.creator_id in ffh_persona_ids]
        assert len(personas) == len(ffh_persona_ids)
        for persona in personas:
            assert persona.title_filter is not None
            assert persona.title_filter != ""

    def test_no_channel_has_two_channel_primary_creators(self) -> None:
        primary_channel_ids = [c.channel_id for c in REGISTRY if c.channel_primary]
        assert len(primary_channel_ids) == len(set(primary_channel_ids))
