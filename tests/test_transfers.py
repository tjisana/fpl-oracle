"""Transfer recommender tests.

The recommender's job is to be HONEST more than clever: it must refuse to
price a -4 in units it doesn't have, refuse to assert affordability it
can't prove, and treat "roll" as a real answer. These pin exactly that.
"""

from __future__ import annotations

from fpl_oracle.consensus.scoring import PlayerScore
from fpl_oracle.fpl.availability import Availability, AvailabilityVerdict
from fpl_oracle.fpl.players import Player, Position
from fpl_oracle.fpl.selling import (
    DerivedPrice,
    SquadPricing,
    season_start_price,
    selling_price,
)
from fpl_oracle.solver.transfers import (
    MIN_BACKERS_TO_RECOMMEND,
    Affordability,
    Verdict,
    build_moves,
    recommend_transfer,
)


def _player(pid: int, name: str, pos: Position, cost: int, team: int = 1) -> Player:
    return Player(
        player_id=pid,
        web_name=name,
        first_name=name,
        second_name=name,
        team_id=team,
        team_short=f"T{team}",
        position=pos,
        now_cost=cost,
        status="a",
        chance_of_playing_next_round=None,
    )


def _score(pid: int, band_score: float, backers: int = 0, detractors: int = 0) -> PlayerScore:
    return PlayerScore(
        player_id=pid,
        band="MID-5",
        weighted_votes=backers * 1.0,
        band_score=band_score,
        backers=backers,
        detractors=detractors,
        votes=[],
    )


def _pricing(owned: list[Player], bank: int = 0, verified: bool = True) -> SquadPricing:
    return SquadPricing(
        prices={
            p.player_id: DerivedPrice(
                player_id=p.player_id,
                purchase_price=p.now_cost,
                now_cost=p.now_cost,
                selling_price=p.now_cost,
                source="season-start",
            )
            for p in owned
        },
        bank=bank,
        verified=verified,
        note="test",
    )


def _excluded(pid: int) -> AvailabilityVerdict:
    return AvailabilityVerdict(
        player_id=pid,
        availability=Availability.EXCLUDED,
        multiplier=0.0,
        reason="injured",
        status="i",
        chance_of_playing_next_round=0,
    )


class TestSellingPriceRule:
    """FPL shares a rise at half, rounded DOWN, and bears a fall in full.
    Getting this wrong overstates budget on every risen player."""

    def test_a_one_tenth_rise_returns_nothing(self) -> None:
        # 1 // 2 == 0 — the case that makes a naive now_cost reading wrong
        # even in the opening week of a season.
        assert selling_price(75, 76) == 75

    def test_a_two_tenth_rise_is_shared(self) -> None:
        assert selling_price(75, 77) == 76

    def test_a_fall_is_borne_in_full(self) -> None:
        assert selling_price(75, 70) == 70

    def test_no_change_sells_at_cost(self) -> None:
        assert selling_price(75, 75) == 75

    def test_season_start_price_backs_out_the_drift(self) -> None:
        assert season_start_price(now_cost=76, cost_change_start=1) == 75


class TestReconciliation:
    def test_verified_when_totals_agree(self) -> None:
        from fpl_oracle.fpl.selling import derive_squad_pricing

        result = derive_squad_pricing(
            owned_player_ids=[1],
            elements_by_id={1: {"now_cost": 76, "cost_change_start": 1}},
            purchase_from_transfers={},
            bank=0,
            reported_squad_value=75,
        )
        assert result.verified is True
        assert result.prices[1].selling_price == 75

    def test_unverified_when_totals_disagree(self) -> None:
        from fpl_oracle.fpl.selling import derive_squad_pricing

        result = derive_squad_pricing(
            owned_player_ids=[1],
            elements_by_id={1: {"now_cost": 76, "cost_change_start": 1}},
            purchase_from_transfers={},
            bank=0,
            reported_squad_value=900,
        )
        assert result.verified is False
        assert "UNVERIFIED" in result.note

    def test_transfer_log_price_beats_season_start(self) -> None:
        from fpl_oracle.fpl.selling import derive_squad_pricing

        result = derive_squad_pricing(
            owned_player_ids=[1],
            elements_by_id={1: {"now_cost": 80, "cost_change_start": 5}},
            purchase_from_transfers={1: 78},
            bank=0,
            reported_squad_value=None,
        )
        assert result.prices[1].purchase_price == 78
        assert result.prices[1].source == "transfer-log"


class TestLegality:
    def test_cannot_swap_across_positions(self) -> None:
        owned = [_player(1, "Keeper", Position.GK, 45)]
        pool = [_player(2, "Striker", Position.FWD, 45)]
        moves = build_moves(owned=owned, pool=pool, scores={}, verdicts={}, pricing=_pricing(owned))
        assert moves == []

    def test_club_limit_is_enforced_after_the_swap(self) -> None:
        owned = [_player(i, f"P{i}", Position.MID, 50, team=9) for i in range(1, 4)]
        owned.append(_player(4, "Other", Position.MID, 50, team=2))
        # A fourth player from club 9 would break the 3-per-club rule.
        pool = [_player(5, "Fourth", Position.MID, 50, team=9)]
        moves = build_moves(owned=owned, pool=pool, scores={}, verdicts={}, pricing=_pricing(owned))
        assert all(m.out_player_id != 4 for m in moves), "swapping in a 4th from club 9 is illegal"

    def test_swapping_within_the_same_club_is_always_legal(self) -> None:
        owned = [_player(i, f"P{i}", Position.MID, 50, team=9) for i in range(1, 4)]
        pool = [_player(5, "Same", Position.MID, 50, team=9)]
        moves = build_moves(owned=owned, pool=pool, scores={}, verdicts={}, pricing=_pricing(owned))
        assert moves, "the outgoing player frees the slot, so the count is unchanged"

    def test_unaffordable_moves_are_dropped(self) -> None:
        owned = [_player(1, "Cheap", Position.MID, 45)]
        pool = [_player(2, "Dear", Position.MID, 120)]
        moves = build_moves(
            owned=owned, pool=pool, scores={}, verdicts={}, pricing=_pricing(owned, bank=0)
        )
        assert moves == []

    def test_bank_extends_the_budget(self) -> None:
        owned = [_player(1, "Cheap", Position.MID, 45)]
        pool = [_player(2, "Dear", Position.MID, 60)]
        moves = build_moves(
            owned=owned, pool=pool, scores={}, verdicts={}, pricing=_pricing(owned, bank=15)
        )
        assert len(moves) == 1

    def test_an_excluded_player_is_never_a_transfer_target(self) -> None:
        owned = [_player(1, "Mine", Position.MID, 50)]
        pool = [_player(2, "Injured", Position.MID, 50)]
        moves = build_moves(
            owned=owned,
            pool=pool,
            scores={},
            verdicts={2: _excluded(2)},
            pricing=_pricing(owned),
        )
        assert moves == []


class TestAffordabilityHonesty:
    def test_unverified_pricing_flags_rather_than_asserts(self) -> None:
        """With £0 in the bank a wrong selling price is the difference
        between a legal transfer and one the owner cannot make. Unproven
        affordability must be stated, not assumed away."""
        owned = [_player(1, "Mine", Position.MID, 50)]
        pool = [_player(2, "Theirs", Position.MID, 50)]
        moves = build_moves(
            owned=owned,
            pool=pool,
            scores={},
            verdicts={},
            pricing=_pricing(owned, verified=False),
        )
        assert moves[0].affordability is Affordability.UNVERIFIED
        assert any("unverified" in n for n in moves[0].notes)

    def test_verified_pricing_states_affordability(self) -> None:
        owned = [_player(1, "Mine", Position.MID, 50)]
        pool = [_player(2, "Theirs", Position.MID, 50)]
        moves = build_moves(owned=owned, pool=pool, scores={}, verdicts={}, pricing=_pricing(owned))
        assert moves[0].affordability is Affordability.AFFORDABLE


class TestVerdicts:
    def test_an_unavailable_player_forces_a_move(self) -> None:
        owned = [_player(1, "Injured", Position.MID, 50), _player(2, "Fine", Position.MID, 50)]
        pool = [_player(3, "Replacement", Position.MID, 50)]
        rec = recommend_transfer(
            owned=owned,
            pool=pool,
            scores={3: _score(3, 0.9, backers=4)},
            verdicts={1: _excluded(1)},
            pricing=_pricing(owned),
            free_transfers=1,
            gameweek=2,
        )
        assert rec.verdict is Verdict.FORCED_MOVE
        assert rec.recommended is not None
        assert rec.recommended.out_name == "Injured"
        assert rec.unavailable_owned == ["Injured"]

    def test_a_thinly_backed_move_does_not_spend_the_transfer(self) -> None:
        """Consensus here is sparse — one enthusiastic creator is noise."""
        owned = [_player(1, "Mine", Position.MID, 50)]
        pool = [_player(2, "Hyped", Position.MID, 50)]
        rec = recommend_transfer(
            owned=owned,
            pool=pool,
            scores={2: _score(2, 1.0, backers=MIN_BACKERS_TO_RECOMMEND - 1)},
            verdicts={},
            pricing=_pricing(owned),
            free_transfers=1,
        )
        assert rec.verdict is Verdict.ROLL

    def test_a_well_backed_clear_upgrade_uses_the_transfer(self) -> None:
        owned = [_player(1, "Mine", Position.MID, 50)]
        pool = [_player(2, "Better", Position.MID, 50)]
        rec = recommend_transfer(
            owned=owned,
            pool=pool,
            scores={1: _score(1, 0.0), 2: _score(2, 0.9, backers=5)},
            verdicts={},
            pricing=_pricing(owned),
            free_transfers=1,
        )
        assert rec.verdict is Verdict.USE_FREE_TRANSFER
        assert rec.recommended is not None
        assert rec.recommended.in_name == "Better"

    def test_roll_is_fully_populated_not_empty(self) -> None:
        """A roll is an answer, not a failure — it must still carry the
        shortlist that was considered and rejected."""
        owned = [_player(1, "Mine", Position.MID, 50)]
        pool = [_player(2, "Meh", Position.MID, 50)]
        rec = recommend_transfer(
            owned=owned,
            pool=pool,
            scores={2: _score(2, 0.5, backers=1)},
            verdicts={},
            pricing=_pricing(owned),
            free_transfers=1,
        )
        assert rec.verdict is Verdict.ROLL
        assert rec.shortlist, "a roll must still show what it looked at"
        assert rec.reasoning
        assert "next gameweek" in rec.reasoning

    def test_no_legal_move_is_reported_distinctly(self) -> None:
        owned = [_player(1, "Mine", Position.MID, 50)]
        rec = recommend_transfer(
            owned=owned,
            pool=[],
            scores={},
            verdicts={},
            pricing=_pricing(owned),
            free_transfers=1,
        )
        assert rec.verdict is Verdict.NO_LEGAL_MOVE
        assert "extraction" in rec.reasoning

    def test_undetermined_free_transfers_assume_the_guaranteed_minimum(self) -> None:
        owned = [_player(1, "Mine", Position.MID, 50)]
        pool = [_player(2, "Other", Position.MID, 50)]
        rec = recommend_transfer(
            owned=owned,
            pool=pool,
            scores={},
            verdicts={},
            pricing=_pricing(owned),
            free_transfers=None,
        )
        assert rec.free_transfers == 1
        assert "assuming 1" in rec.reasoning


class TestShortlistIsInformative:
    def test_shortlist_shows_one_row_per_outgoing_player(self) -> None:
        """An unavailable player's depressed coefficient makes every swap
        involving them outrank everything else, so a raw ranking becomes
        five ways to replace one person."""
        owned = [_player(1, "Weak", Position.MID, 50), _player(2, "Fine", Position.MID, 50)]
        pool = [_player(i, f"Cand{i}", Position.MID, 50) for i in range(3, 8)]
        rec = recommend_transfer(
            owned=owned,
            pool=pool,
            scores={
                1: _score(1, 0.0),
                2: _score(2, 0.4),
                **{i: _score(i, 0.9) for i in range(3, 8)},
            },
            verdicts={},
            pricing=_pricing(owned),
            free_transfers=1,
        )
        outgoing = [m.out_player_id for m in rec.shortlist]
        assert len(outgoing) == len(set(outgoing)), "one row per outgoing player"


class TestNeverPricesAHit:
    def test_a_strongly_backed_move_is_surfaced_never_endorsed_as_a_hit(self) -> None:
        """Band scores have no points unit, so the system must not claim a
        gain is 'worth -4'. It shows the move and leaves the call to a human."""
        owned = [_player(1, "Mine", Position.MID, 50)]
        pool = [_player(2, "Great", Position.MID, 50)]
        rec = recommend_transfer(
            owned=owned,
            pool=pool,
            scores={1: _score(1, 0.49), 2: _score(2, 0.5, backers=5)},
            verdicts={},
            pricing=_pricing(owned),
            free_transfers=1,
        )
        # marginal gain, well backed -> rolled, but kept visible
        assert rec.verdict is Verdict.ROLL
        assert rec.worth_considering
        assert rec.worth_considering[0].in_name == "Great"


class TestBenchedUnavailableDoesNotForceAMove:
    """Found on the real GW2 run. Chiesa was injured on the BENCH, and the
    recommender called it a FORCED_MOVE — proposing to spend the only free
    transfer on a player whose absence costs autosub cover and nothing
    else, while a 1-backer starter sat next to a 5-backer replacement.

    An unavailable player only forces a transfer when they are in the XI,
    because that is when the squad cannot field 11 who will play."""

    def test_benched_unavailable_player_does_not_force(self) -> None:
        owned = [
            _player(1, "BenchInjured", Position.MID, 50),
            _player(2, "Starter", Position.MID, 50),
        ]
        pool = [_player(3, "Sub", Position.MID, 50)]
        rec = recommend_transfer(
            owned=owned,
            pool=pool,
            scores={3: _score(3, 0.9, backers=4)},
            verdicts={1: _excluded(1)},
            pricing=_pricing(owned),
            free_transfers=1,
            starting_player_ids={2},  # player 1 is on the bench
        )
        assert rec.verdict is not Verdict.FORCED_MOVE

    def test_the_benched_case_is_explained_not_hidden(self) -> None:
        owned = [
            _player(1, "BenchInjured", Position.MID, 50),
            _player(2, "Starter", Position.MID, 50),
        ]
        pool = [_player(3, "Sub", Position.MID, 50)]
        rec = recommend_transfer(
            owned=owned,
            pool=pool,
            scores={3: _score(3, 0.5, backers=1)},
            verdicts={1: _excluded(1)},
            pricing=_pricing(owned),
            free_transfers=1,
            starting_player_ids={2},
        )
        assert "BENCHED" in rec.reasoning
        assert "BenchInjured" in rec.unavailable_owned

    def test_an_unavailable_STARTER_still_forces(self) -> None:
        owned = [
            _player(1, "StarterInjured", Position.MID, 50),
            _player(2, "Fine", Position.MID, 50),
        ]
        pool = [_player(3, "Sub", Position.MID, 50)]
        rec = recommend_transfer(
            owned=owned,
            pool=pool,
            scores={3: _score(3, 0.9, backers=4)},
            verdicts={1: _excluded(1)},
            pricing=_pricing(owned),
            free_transfers=1,
            starting_player_ids={1, 2},
        )
        assert rec.verdict is Verdict.FORCED_MOVE
        assert rec.recommended is not None
        assert rec.recommended.out_name == "StarterInjured"

    def test_absent_xi_information_is_conservative(self) -> None:
        """With no XI data, assume everyone starts — that can only make an
        exclusion look more urgent, never less."""
        owned = [_player(1, "Injured", Position.MID, 50)]
        pool = [_player(2, "Sub", Position.MID, 50)]
        rec = recommend_transfer(
            owned=owned,
            pool=pool,
            scores={2: _score(2, 0.9, backers=4)},
            verdicts={1: _excluded(1)},
            pricing=_pricing(owned),
            free_transfers=1,
        )
        assert rec.verdict is Verdict.FORCED_MOVE
