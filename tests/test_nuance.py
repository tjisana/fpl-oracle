"""Tests for fpl_oracle.report.nuance.

Network-free and LLM-free throughout: the two halves worth testing are
both pure — the evidence bundle handed to the model (`build_user_message`)
and the mechanical attribution check applied to what comes back
(`validate_nuance`). The API call itself is exercised only through a fake
client, to pin the two behaviours the pipeline depends on: a failure is
non-fatal, and fabricated attributions never survive.
"""

from __future__ import annotations

from datetime import UTC, datetime

import anthropic
import httpx
import pytest

from fpl_oracle.consensus.captaincy import CaptainCandidate, CaptaincyElection
from fpl_oracle.consensus.scoring import CreatorVote, PlayerScore
from fpl_oracle.extract.run_extract import ResolvedExtraction
from fpl_oracle.extract.schemas import Pick, PickAction, Provenance, VideoExtraction
from fpl_oracle.fpl.players import Player, PlayerDB, Position
from fpl_oracle.report import nuance as nuance_mod
from fpl_oracle.report.models import Concern, ConcernKind, PlayerNuance, SquadNuance
from fpl_oracle.solver.squad import Squad, SquadPlayer

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _player(pid: int, position: Position = Position.MID, price_m: float = 8.0) -> Player:
    return Player(
        player_id=pid,
        web_name=f"P{pid}",
        first_name="F",
        second_name=f"P{pid}",
        team_id=1,
        team_short="TST",
        position=position,
        now_cost=int(price_m * 10),
        status="a",
        chance_of_playing_next_round=None,
    )


def _db(players: list[Player]) -> PlayerDB:
    return PlayerDB(
        players={p.player_id: p for p in players},
        team_full_names={1: "Test FC"},
        player_metaphones={p.player_id: ("", "") for p in players},
    )


def _squad_player(pid: int, starting: bool = True) -> SquadPlayer:
    return SquadPlayer(
        player_id=pid,
        starting=starting,
        price_m=8.0,
        position=Position.MID,
        band_score=0.8,
        coefficient=1.0,
    )


def _squad(pids: list[int]) -> Squad:
    return Squad(
        players=[_squad_player(pid) for pid in pids],
        total_cost_m=100.0,
        objective=1.0,
        captain_options_included=[pids[0]],
        captain_options_required=2,
    )


def _vote(creator_id: str, action: PickAction = PickAction.SQUAD_INCLUDE, value: float = 1.0):
    return CreatorVote(
        creator_id=creator_id,
        creator_weight=0.8,
        action=action,
        conviction=4,
        value=value,
    )


def _score(pid: int, votes: list[CreatorVote]) -> PlayerScore:
    positives = [v for v in votes if v.value > 0]
    negatives = [v for v in votes if v.value < 0]
    return PlayerScore(
        player_id=pid,
        band="MID-8",
        weighted_votes=sum(v.value for v in votes),
        band_score=0.75,
        backers=len(positives),
        detractors=len(negatives),
        votes=votes,
    )


def _extraction(creator_id: str, picks: list[tuple[int, str]]) -> ResolvedExtraction:
    return ResolvedExtraction(
        extraction=VideoExtraction(
            creator_id=creator_id,
            video_id=f"vid_{creator_id}",
            video_title="My GW1 Team",
            published_at=datetime(2026, 8, 1, tzinfo=UTC),
            gameweek=1,
            picks=[
                Pick(
                    player_name_raw=f"P{pid}",
                    team_inferred=None,
                    position_inferred=None,
                    player_id=pid,
                    action=PickAction.SQUAD_INCLUDE,
                    conviction=4,
                    time_horizon=1,
                    reasoning=reason,
                    provenance=Provenance.PERSONAL,
                )
                for pid, reason in picks
            ],
        ),
        resolutions=[],
    )


def _election(pids: list[int], thin: bool = False) -> CaptaincyElection:
    return CaptaincyElection(
        candidates=[
            CaptainCandidate(player_id=pid, score=3.0 - i, voters=3 - i, share=1.0 - 0.2 * i)
            for i, pid in enumerate(pids)
        ],
        options=pids[:2],
        total_voters=4,
        contributing_creators=10,
        thin=thin,
    )


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------


class TestBuildUserMessage:
    def test_carries_reasoning_votes_and_election(self) -> None:
        db = _db([_player(1), _player(2)])
        message = nuance_mod.build_user_message(
            squad=_squad([1, 2]),
            scores={
                1: _score(1, [_vote("andy"), _vote("focal", PickAction.AVOID, -0.5)]),
                2: _score(2, [_vote("andy")]),
            },
            election=_election([1, 2]),
            extractions=[
                _extraction("andy", [(1, "nailed on penalties"), (2, "great fixtures")]),
                _extraction("focal", [(1, "rotation risk under the new manager")]),
            ],
            db=db,
            creator_names={"andy": "Andy (LTFPL)", "focal": "FPL Focal"},
        )

        assert "nailed on penalties" in message
        assert "rotation risk under the new manager" in message
        assert "andy" in message and "focal" in message
        assert "CAPTAINCY ELECTION" in message
        # The already-solved squad is stated as settled, so the model is not
        # invited to re-pick it.
        assert "do not re-pick" in message

    def test_thin_election_is_disclosed_to_the_model(self) -> None:
        db = _db([_player(1)])
        message = nuance_mod.build_user_message(
            squad=_squad([1]),
            scores={1: _score(1, [_vote("andy")])},
            election=_election([1], thin=True),
            extractions=[_extraction("andy", [(1, "captain material")])],
            db=db,
            creator_names={},
        )
        assert "THIN evidence" in message

    def test_player_with_no_votes_is_labelled_not_dropped(self) -> None:
        """A structural/price-driven pick with zero creator support is the
        most important thing to surface, not the least — it must reach the
        model rather than vanishing from the bundle."""
        db = _db([_player(1), _player(2)])
        message = nuance_mod.build_user_message(
            squad=_squad([1, 2]),
            scores={1: _score(1, [_vote("andy")])},
            election=_election([1]),
            extractions=[_extraction("andy", [(1, "solid")])],
            db=db,
            creator_names={},
        )
        assert "player_id=2" in message
        assert "no creator votes" in message

    def test_is_pure_and_deterministic(self) -> None:
        db = _db([_player(1)])

        def _render() -> str:
            return nuance_mod.build_user_message(
                squad=_squad([1]),
                scores={1: _score(1, [_vote("andy")])},
                election=_election([1]),
                extractions=[_extraction("andy", [(1, "solid")])],
                db=db,
                creator_names={},
            )

        assert _render() == _render()

    def test_repeated_picks_for_one_player_keep_all_words(self) -> None:
        """Consensus collapses a creator's squad_include + captain pick into
        one vote; both reasoning sentences are still evidence."""
        db = _db([_player(1)])
        message = nuance_mod.build_user_message(
            squad=_squad([1]),
            scores={1: _score(1, [_vote("andy")])},
            election=_election([1]),
            extractions=[
                _extraction("andy", [(1, "in my squad"), (1, "and my captain")]),
            ],
            db=db,
            creator_names={},
        )
        assert "in my squad" in message
        assert "and my captain" in message


# ---------------------------------------------------------------------------
# Mechanical attribution check
# ---------------------------------------------------------------------------


def _nuance_with(player_id: int, creator_ids: list[str]) -> SquadNuance:
    return SquadNuance(
        players=[
            PlayerNuance(
                player_id=player_id,
                summary="backed for penalties",
                concerns=[
                    Concern(
                        kind=ConcernKind.ROTATION,
                        note="might be rotated",
                        creator_ids=creator_ids,
                        severity=2,
                    )
                ],
            )
        ]
    )


class TestValidateNuance:
    def test_keeps_real_attributions(self) -> None:
        cleaned, dropped = nuance_mod.validate_nuance(
            _nuance_with(1, ["andy"]), {1: {"andy", "focal"}}
        )
        assert dropped == []
        assert cleaned.players[0].concerns[0].creator_ids == ["andy"]

    def test_drops_a_creator_who_did_not_vote_on_that_player(self) -> None:
        cleaned, dropped = nuance_mod.validate_nuance(
            _nuance_with(1, ["andy", "never_voted"]), {1: {"andy"}}
        )
        assert cleaned.players[0].concerns[0].creator_ids == ["andy"]
        assert dropped == ["1:never_voted"]

    def test_concern_with_no_valid_attribution_is_dropped_entirely(self) -> None:
        """An unattributed caveat is indistinguishable from the model's own
        opinion — the one output this pass must never produce."""
        cleaned, dropped = nuance_mod.validate_nuance(_nuance_with(1, ["ghost"]), {1: {"andy"}})
        assert cleaned.players[0].concerns == []
        assert dropped == ["1:ghost"]

    def test_player_outside_the_squad_is_dropped(self) -> None:
        cleaned, dropped = nuance_mod.validate_nuance(_nuance_with(99, ["andy"]), {1: {"andy"}})
        assert cleaned.players == []
        assert dropped == ["99:<not in squad>"]

    def test_squad_and_captaincy_notes_survive_validation(self) -> None:
        source = SquadNuance(
            players=[], captaincy_note="the armband is contested", squad_notes=["three from TST"]
        )
        cleaned, dropped = nuance_mod.validate_nuance(source, {1: {"andy"}})
        assert cleaned.captaincy_note == "the armband is contested"
        assert cleaned.squad_notes == ["three from TST"]
        assert dropped == []


class TestAllowedCreators:
    def test_maps_each_squad_player_to_its_voters(self) -> None:
        allowed = nuance_mod.allowed_creators(
            _squad([1, 2]),
            {1: _score(1, [_vote("andy"), _vote("focal")]), 2: _score(2, [_vote("andy")])},
        )
        assert allowed == {1: {"andy", "focal"}, 2: {"andy"}}

    def test_unvoted_squad_player_permits_nobody(self) -> None:
        allowed = nuance_mod.allowed_creators(_squad([1, 2]), {1: _score(1, [_vote("andy")])})
        assert allowed[2] == set()


# ---------------------------------------------------------------------------
# The call — faked, never live.
# ---------------------------------------------------------------------------


def _generate(monkeypatch, call):
    """Run generate_nuance against a fake `messages.parse`."""

    class _Messages:
        parse = staticmethod(call)

    class _Client:
        messages = _Messages()

    monkeypatch.setattr(nuance_mod, "_get_client", lambda: _Client())
    db = _db([_player(1)])
    return nuance_mod.generate_nuance(
        run_id="testrun",
        squad=_squad([1]),
        scores={1: _score(1, [_vote("andy")])},
        election=_election([1]),
        extractions=[_extraction("andy", [(1, "solid")])],
        db=db,
        creator_names={},
    )


class _FakeResponse:
    def __init__(self, parsed, stop_reason="end_turn"):
        self.parsed_output = parsed
        self.stop_reason = stop_reason
        self.content = []


def _wire_with_out_of_range_severity() -> nuance_mod._WireNuance:
    """The wire schema's `severity` is a plain `int` (no bound); the
    strict `Concern` model bounds it 1-3. This is the easiest way to make
    `_to_strict` raise `ValidationError` and drive the retry loop."""
    return nuance_mod._WireNuance(
        players=[
            nuance_mod._WirePlayerNuance(
                player_id=1,
                summary="penalties",
                concerns=[
                    nuance_mod._WireConcern(
                        kind=ConcernKind.ROTATION,
                        note="rotation risk",
                        creator_ids=["andy"],
                        severity=99,
                    )
                ],
            )
        ],
        captaincy_note=None,
        squad_notes=[],
    )


def _fake_client(monkeypatch: pytest.MonkeyPatch, call):
    class _Messages:
        parse = staticmethod(call)

    class _Client:
        messages = _Messages()

    monkeypatch.setattr(nuance_mod, "_get_client", lambda: _Client())


class TestCallModelRetry:
    """`_call_model`'s retry loop: a schema-invalid strict conversion
    should feed the validation error back to the model and try again,
    up to `MAX_VALIDATION_RETRIES` times."""

    def test_retries_with_validation_feedback_and_succeeds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bad_wire = _wire_with_out_of_range_severity()
        good_wire = nuance_mod._WireNuance(
            players=[nuance_mod._WirePlayerNuance(player_id=1, summary="penalties", concerns=[])],
            captaincy_note=None,
            squad_notes=[],
        )
        calls: list[dict] = []

        def parse(**kwargs):
            calls.append(kwargs)
            wire = bad_wire if len(calls) == 1 else good_wire
            return _FakeResponse(wire)

        _fake_client(monkeypatch, parse)

        result = nuance_mod._call_model("evidence bundle")

        assert result.players[0].concerns == []
        assert len(calls) == 2
        # the retry actually carries the validation feedback back to the model
        retry_messages = calls[1]["messages"]
        feedback = retry_messages[-1]["content"]
        assert "failed schema validation" in feedback
        assert "severity" in feedback.lower() or "1" in feedback

    def test_exhausts_retries_and_raises_nuance_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bad_wire = _wire_with_out_of_range_severity()
        calls: list[dict] = []

        def parse(**kwargs):
            calls.append(kwargs)
            return _FakeResponse(bad_wire)

        _fake_client(monkeypatch, parse)

        with pytest.raises(nuance_mod.NuanceError, match="schema-invalid"):
            nuance_mod._call_model("evidence bundle")

        # one initial attempt plus every retry, never more
        assert len(calls) == 1 + nuance_mod.MAX_VALIDATION_RETRIES


class TestGenerateNuance:
    def test_api_error_is_non_fatal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The squad is already solved when this runs — a nuance failure must
        never cost a deadline-morning squad."""

        def _boom(**_kwargs):
            request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
            raise anthropic.APIError("down", request=request, body=None)

        record = _generate(monkeypatch, _boom)
        assert record.failed is True
        assert record.failure_reason
        assert record.nuance.players == []
        assert record.run_id == "testrun"

    def test_validation_retry_exhaustion_is_non_fatal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same failure as TestCallModelRetry's exhaustion case, but through
        `generate_nuance` — confirms the caller-facing promise: exhaustion
        degrades to a `failed=True` record, it never raises out of here."""
        bad_wire = _wire_with_out_of_range_severity()
        record = _generate(monkeypatch, lambda **_k: _FakeResponse(bad_wire))
        assert record.failed is True
        assert record.failure_reason
        assert "schema-invalid" in record.failure_reason

    def test_truncation_is_non_fatal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        record = _generate(monkeypatch, lambda **_k: _FakeResponse(None, stop_reason="max_tokens"))
        assert record.failed is True

    def test_refusal_is_non_fatal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        record = _generate(monkeypatch, lambda **_k: _FakeResponse(None, stop_reason="refusal"))
        assert record.failed is True

    def test_success_records_dropped_attributions(self, monkeypatch: pytest.MonkeyPatch) -> None:
        wire = nuance_mod._WireNuance(
            players=[
                nuance_mod._WirePlayerNuance(
                    player_id=1,
                    summary="penalties",
                    concerns=[
                        nuance_mod._WireConcern(
                            kind=ConcernKind.ROTATION,
                            note="rotation risk",
                            creator_ids=["andy", "ghost"],
                            severity=2,
                        )
                    ],
                )
            ],
            captaincy_note=None,
            squad_notes=[],
        )
        record = _generate(monkeypatch, lambda **_k: _FakeResponse(wire))
        assert record.failed is False
        assert record.nuance.players[0].concerns[0].creator_ids == ["andy"]
        assert record.dropped_creator_ids == ["1:ghost"]
        assert record.model == nuance_mod.MODEL
