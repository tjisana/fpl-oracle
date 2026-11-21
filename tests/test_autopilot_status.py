"""Harvest readiness report (`ingest/status.py`). Offline."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from rich.console import Console

from fpl_oracle.ingest import status as st
from fpl_oracle.ingest.ledger import Ledger, LedgerRow
from fpl_oracle.roster.models import Creator
from tests.test_autopilot_units import D, creator, hrs, row

NOW = D - hrs(20)


def weighted(cid: str, weight: float) -> Creator:
    return creator(cid, f"UC_{cid}", weight=weight)


def ledger_of(*rows: LedgerRow) -> Ledger:
    ledger = Ledger()
    for r in rows:
        ledger.rows[r.video_id] = r
    return ledger


def report(registry: list[Creator], *rows: LedgerRow, left: int = 7) -> st.ReadinessReport:
    return st.build_readiness(12, D, ledger_of(*rows), registry, now=NOW, gemini_left=left)


def by_id(rep: st.ReadinessReport) -> dict[str, st.CreatorReadiness]:
    return {c.creator_id: c for c in rep.creators}


class TestFreshness:
    def test_fresh_stale_missing(self) -> None:
        registry = [weighted("fresh", 0.5), weighted("stale", 0.3), weighted("missing", 0.2)]
        rep = report(
            registry,
            row("f1", "fresh", "selection", D - hrs(30), "ok"),
            row("s_old", "stale", "plan", D - hrs(50), "ok"),
            row("s_new", "stale", "selection", D - hrs(25), "pending"),
        )
        got = by_id(rep)
        assert got["fresh"].state is st.Freshness.FRESH
        assert got["stale"].state is st.Freshness.STALE
        assert got["missing"].state is st.Freshness.MISSING
        assert got["stale"].transcribed_hours_old == pytest.approx(30.0)  # NOW is D-20h, pub D-50h
        assert got["stale"].newest_hours_old == pytest.approx(5.0)
        assert got["fresh"].newest_hours_old == pytest.approx(10.0)
        assert rep.missing == ["missing"]

    def test_source_is_shown_for_fresh(self) -> None:
        r = row("f1", "fresh", "selection", D - hrs(30), "ok")
        r.source = "gemini:gemini-3.5-flash"
        got = by_id(report([weighted("fresh", 1.0)], r))["fresh"]
        assert got.source == "gemini:gemini-3.5-flash"

    def test_newest_not_transcribed_and_nothing_older_is_missing(self) -> None:
        rep = report(
            [weighted("a", 1.0)], row("a1", "a", "selection", D - hrs(25), "needs_captions")
        )
        got = by_id(rep)["a"]
        assert got.state is st.Freshness.MISSING
        assert "needs_captions" in got.note

    def test_other_review_and_skipped_rows_are_not_relevant(self) -> None:
        rep = report(
            [weighted("a", 1.0)],
            row("o", "a", "other", D - hrs(25), "ok"),
            row("r", "a", "review", D - hrs(26), "skipped"),
            row("s", "a", "selection", D - hrs(27), "skipped"),
        )
        assert by_id(rep)["a"].state is st.Freshness.MISSING

    def test_other_gameweeks_are_ignored(self) -> None:
        rep = report(
            [weighted("a", 1.0)], row("old", "a", "selection", D - hrs(200), "ok", gameweek=11)
        )
        assert by_id(rep)["a"].state is st.Freshness.MISSING

    def test_creators_listed_in_weight_order(self) -> None:
        rep = report([weighted("low", 0.1), weighted("high", 0.9), weighted("mid", 0.5)])
        assert [c.creator_id for c in rep.creators] == ["high", "mid", "low"]

    def test_default_weight_for_unweighted_creators(self) -> None:
        rep = report([creator("a", "UC_a"), weighted("b", 0.2)])
        assert by_id(rep)["a"].weight == pytest.approx(0.2)
        assert by_id(rep)["a"].weight_share == pytest.approx(0.5)


class TestVerdict:
    def _rep(self, fresh: float, stale: float, missing: float) -> st.ReadinessReport:
        registry = [weighted("f", fresh), weighted("s", stale), weighted("m", missing)]
        registry = [c for c in registry if c.weight]  # drop zero-weight creators
        rows = []
        if fresh:
            rows.append(row("f1", "f", "selection", D - hrs(30), "ok"))
        if stale:
            rows += [
                row("s_old", "s", "plan", D - hrs(50), "ok"),
                row("s_new", "s", "selection", D - hrs(30), "pending"),
            ]
        return report(registry, *rows)

    def test_ready_at_exactly_80_percent_fresh(self) -> None:
        rep = self._rep(0.8, 0.1, 0.1)
        assert rep.verdict is st.Verdict.READY
        assert rep.fresh_share == pytest.approx(0.8)

    def test_just_under_80_percent_is_partial(self) -> None:
        assert self._rep(0.79, 0.11, 0.10).verdict is st.Verdict.PARTIAL

    def test_partial_at_exactly_50_percent_covered(self) -> None:
        rep = self._rep(0.3, 0.2, 0.5)
        assert rep.verdict is st.Verdict.PARTIAL
        assert rep.covered_share == pytest.approx(0.5)

    def test_thin_below_50_percent_covered(self) -> None:
        assert self._rep(0.3, 0.19, 0.51).verdict is st.Verdict.THIN

    def test_stale_counts_for_covered_but_not_fresh(self) -> None:
        rep = self._rep(0.0, 0.9, 0.1)
        assert rep.fresh_share == 0
        assert rep.covered_share == pytest.approx(0.9)
        assert rep.verdict is st.Verdict.PARTIAL

    def test_empty_ledger_is_thin(self) -> None:
        rep = report([weighted("a", 0.5), weighted("b", 0.5)])
        assert rep.verdict is st.Verdict.THIN
        assert rep.missing == ["a", "b"]

    def test_verdict_for_function(self) -> None:
        assert st.verdict_for(0.8, 0.8) is st.Verdict.READY
        assert st.verdict_for(0.0, 0.5) is st.Verdict.PARTIAL
        assert st.verdict_for(0.0, 0.49) is st.Verdict.THIN


class TestEdgeCases:
    def test_no_eligible_creators_is_thin_not_a_crash(self) -> None:
        rep = report([])
        assert rep.verdict is st.Verdict.THIN
        assert rep.creators == [] and rep.fresh_share == 0

    def test_all_zero_weights_do_not_divide_by_zero(self) -> None:
        rep = report([weighted("a", 0.0)])
        assert rep.verdict is st.Verdict.THIN

    def test_retry_after_is_shown_in_the_note(self) -> None:
        pending = row("a_new", "a", "selection", D - hrs(25), "pending")
        pending.retry_after = D - hrs(19)
        rep = report([weighted("a", 1.0)], row("a_old", "a", "plan", D - hrs(50), "ok"), pending)
        assert "retry after" in by_id(rep)["a"].note
        rep2 = report([weighted("a", 1.0)], pending)
        assert "retry after" in by_id(rep2)["a"].note

    def test_low_coverage_needs_the_flag_and_the_window(self) -> None:
        reg = [weighted("a", 1.0)]
        now = D - hrs(3)
        inside = st.build_readiness(12, D, Ledger(), reg, now=now, gemini_left=0, backstop_hours=6)
        assert inside.low_coverage
        assert not st.build_readiness(12, D, Ledger(), reg, now=now, gemini_left=0).low_coverage
        far = st.build_readiness(
            12, D, Ledger(), reg, now=D - hrs(30), gemini_left=0, backstop_hours=6
        )
        assert not far.low_coverage


class TestOutputs:
    def test_footer_fields(self) -> None:
        rep = report([weighted("a", 1.0)], left=7)
        assert rep.hours_to_deadline == pytest.approx(20.0)
        assert rep.gemini_budget_left == 7

    def test_unknown_deadline(self) -> None:
        rep = st.build_readiness(12, None, Ledger(), [weighted("a", 1.0)], now=NOW, gemini_left=0)
        assert rep.hours_to_deadline is None

    def test_readiness_json_round_trip(self, tmp_path: Path) -> None:
        rep = report([weighted("a", 1.0)], row("a1", "a", "selection", D - hrs(30), "ok"))
        path = st.write_readiness(rep, tmp_path)
        assert path == tmp_path / "readiness.json"
        assert st.ReadinessReport.model_validate_json(path.read_text()) == rep
        assert json.loads(path.read_text())["verdict"] == "READY"

    def test_print_status_renders(self) -> None:
        rep = report(
            [weighted("a", 0.6), weighted("b", 0.4)],
            row("a1", "a", "selection", D - hrs(30), "ok"),
            row("b_old", "b", "plan", D - hrs(60), "ok"),
            row("b_new", "b", "selection", D - hrs(30), "new"),
        )
        buf = io.StringIO()
        st.print_status(rep, Console(file=buf, width=200, color_system=None))
        text = buf.getvalue()
        assert "FRESH" in text and "STALE" in text
        assert "Verdict: PARTIAL" in text
        assert "Gemini requests left today" in text

    def test_from_s3_is_not_implemented_yet(self) -> None:
        with pytest.raises(NotImplementedError, match="M2"):
            st.main(["--from-s3"])
