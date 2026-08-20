"""Tests for fpl_oracle.deadline: the deadline-morning stage chain.

Everything here is network-free and LLM-free: `ingest_main`,
`run_extraction`, and `run_pipeline` are monkeypatched at the module level
(`fpl_oracle.deadline.<name>`) with fakes rather than ever calling the real
stages, and `PlayerDB.load` is monkeypatched wherever the final summary
needs a player DB to print names from. `diff_vs_previous`/`load_previous_run`
now live in `pipeline.py` and are tested in tests/test_pipeline.py; this
module only checks that `main()` reads `result.diff` for its summary.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from fpl_oracle import deadline
from fpl_oracle.fpl.players import PlayerDB
from fpl_oracle.pipeline import SquadDiff

# ---------------------------------------------------------------------------
# Test helpers.
# ---------------------------------------------------------------------------


def _raiser(exc: BaseException | type[BaseException]):
    def _r(*args, **kwargs):
        raise exc

    return _r


def _fake_db() -> SimpleNamespace:
    """Duck-typed stand-in for `PlayerDB` — `_player_name` only ever calls
    `.get(player_id)`."""
    return SimpleNamespace(get=lambda player_id: None)


def _fake_diff(previous_run_id: str | None = None) -> SquadDiff:
    """A minimal, valid `SquadDiff` — `_print_final_summary` only reads it,
    never `deadline.py` computing one anymore (see the module docstring),
    so the exact contents rarely matter to these tests."""
    return SquadDiff(
        previous_run_id=previous_run_id,
        players_in=[],
        players_out=[],
        previous_captain=None,
        new_captain=None,
        captain_changed=False,
    )


def _fake_result(
    player_ids: tuple[int, ...] = (1, 2, 3),
    captain: int | None = 1,
    vice: int | None = 2,
    run_id: str = "run-new",
    cost: float = 100.0,
    formation: str = "3-4-3",
    relaxed: bool = False,
    report_path: Path | None = None,
    diff: SquadDiff | None = None,
) -> SimpleNamespace:
    """Duck-typed stand-in for `pipeline.PipelineResult` — everything
    `_print_final_summary` touches, nothing more. Real `PipelineResult`
    construction needs a full `RunRecord`/`Squad`, which is exactly the
    concurrently-evolving machinery these tests must stay decoupled from;
    attribute access doesn't care which it's given."""
    squad = SimpleNamespace(
        players=[SimpleNamespace(player_id=pid) for pid in player_ids], total_cost_m=cost
    )
    solver = SimpleNamespace(
        captain=captain,
        vice_captain=vice,
        formation=formation,
        relaxed_captaincy=relaxed,
        captain_options_required=2,
    )
    record = SimpleNamespace(run_id=run_id, solver=solver)
    return SimpleNamespace(
        squad=squad,
        record=record,
        report_path=report_path,
        diff=diff if diff is not None else _fake_diff(),
    )


def _fake_extract_summary(
    report_path: Path, attempted: int = 3, succeeded: int = 3, failures=None
) -> SimpleNamespace:
    """Duck-typed stand-in for `extract.run_extract.ExtractionRunSummary` —
    the counts `main()` gates stage 3 on, plus the report path it prints."""
    fails = failures or []
    return SimpleNamespace(
        report_path=report_path,
        attempted=attempted,
        succeeded=succeeded,
        failures=fails,
        failed=len(fails),
    )


def _patch_happy_path(monkeypatch, tmp_path: Path, captured: dict | None = None):
    """Wire ingest/extract/solve to trivially-succeeding fakes and point
    DEFAULT_RUNS_DIR at an empty tmp dir, so `main()` can run end-to-end
    without touching the real project or the network."""

    def fake_run_pipeline(**kwargs):
        if captured is not None:
            captured.update(kwargs)
        return _fake_result()

    monkeypatch.setattr(deadline, "ingest_main", lambda: 0)
    monkeypatch.setattr(
        deadline,
        "run_extraction",
        lambda *a, **k: _fake_extract_summary(tmp_path / "match_quality.md"),
    )
    monkeypatch.setattr(deadline, "run_pipeline", fake_run_pipeline)
    monkeypatch.setattr(deadline, "DEFAULT_RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(PlayerDB, "load", classmethod(lambda cls, force_refresh=False: _fake_db()))


# ---------------------------------------------------------------------------
# --dry-run: the safe default. Must never touch a stage callable.
# ---------------------------------------------------------------------------


class TestDryRun:
    def test_makes_zero_stage_calls_and_exits_0(self, monkeypatch, capsys) -> None:
        monkeypatch.setattr(deadline, "ingest_main", _raiser(AssertionError("ingest called")))
        monkeypatch.setattr(
            deadline, "run_extraction", _raiser(AssertionError("run_extraction called"))
        )
        monkeypatch.setattr(
            deadline, "run_pipeline", _raiser(AssertionError("run_pipeline called"))
        )

        rc = deadline.main(["--dry-run"])

        assert rc == 0
        out = capsys.readouterr().out
        assert "DRY RUN" in out
        assert "warm transcript cache" in out
        assert "re-extract picks" in out
        assert "re-solve" in out

    def test_reports_costs_in_plain_terms(self, monkeypatch, capsys) -> None:
        monkeypatch.setattr(deadline, "ingest_main", _raiser(AssertionError))
        monkeypatch.setattr(deadline, "run_extraction", _raiser(AssertionError))
        monkeypatch.setattr(deadline, "run_pipeline", _raiser(AssertionError))

        deadline.main(["--dry-run"])
        out = capsys.readouterr().out

        assert "COSTS MONEY" in out
        assert "FREE" in out
        assert "Claude extraction call" in out

    def test_respects_skip_flags_in_the_plan(self, monkeypatch, capsys) -> None:
        monkeypatch.setattr(deadline, "ingest_main", _raiser(AssertionError))
        monkeypatch.setattr(deadline, "run_extraction", _raiser(AssertionError))
        monkeypatch.setattr(deadline, "run_pipeline", _raiser(AssertionError))

        deadline.main(["--dry-run", "--skip-ingest", "--skip-extract", "--no-nuance"])
        out = capsys.readouterr().out

        assert "SKIPPED (--skip-ingest)" in out
        assert "SKIPPED (--skip-extract)" in out
        assert "SKIPPED (--no-nuance)" in out


# ---------------------------------------------------------------------------
# Stage skipping.
# ---------------------------------------------------------------------------


class TestStageSkipping:
    def test_skip_ingest_never_calls_ingest(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.setattr(deadline, "ingest_main", _raiser(AssertionError("ingest called")))
        monkeypatch.setattr(
            deadline, "run_extraction", lambda *a, **k: _fake_extract_summary(tmp_path / "mq.md")
        )
        monkeypatch.setattr(deadline, "run_pipeline", lambda **k: _fake_result())
        monkeypatch.setattr(deadline, "DEFAULT_RUNS_DIR", tmp_path / "runs")
        monkeypatch.setattr(
            PlayerDB, "load", classmethod(lambda cls, force_refresh=False: _fake_db())
        )

        rc = deadline.main(["--skip-ingest"])
        assert rc == 0

    def test_skip_extract_never_calls_extract(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.setattr(deadline, "ingest_main", lambda: 0)
        monkeypatch.setattr(
            deadline, "run_extraction", _raiser(AssertionError("run_extraction called"))
        )
        monkeypatch.setattr(deadline, "run_pipeline", lambda **k: _fake_result())
        monkeypatch.setattr(deadline, "DEFAULT_RUNS_DIR", tmp_path / "runs")
        monkeypatch.setattr(
            PlayerDB, "load", classmethod(lambda cls, force_refresh=False: _fake_db())
        )

        rc = deadline.main(["--skip-extract"])
        assert rc == 0

    def test_skip_both_reaches_solve_stage(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.setattr(deadline, "ingest_main", _raiser(AssertionError))
        monkeypatch.setattr(deadline, "run_extraction", _raiser(AssertionError))
        called = {}

        def fake_run_pipeline(**kwargs):
            called["hit"] = True
            return _fake_result()

        monkeypatch.setattr(deadline, "run_pipeline", fake_run_pipeline)
        monkeypatch.setattr(deadline, "DEFAULT_RUNS_DIR", tmp_path / "runs")
        monkeypatch.setattr(
            PlayerDB, "load", classmethod(lambda cls, force_refresh=False: _fake_db())
        )

        rc = deadline.main(["--skip-ingest", "--skip-extract"])
        assert rc == 0
        assert called.get("hit") is True


# ---------------------------------------------------------------------------
# Failure handling: earlier work is never lost, resume command is exact.
# ---------------------------------------------------------------------------


class TestVideoRefreshWiring:
    def test_extract_stage_forces_a_fresh_uploads_listing(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """The uploads listing is cached for 6h. On deadline morning that is
        exactly wrong: an 08:00 run reusing a 06:00 listing cannot see the
        07:30 team reveal, and would extract yesterday's team instead."""
        captured: dict = {}

        def fake_run_extraction(*args, **kwargs):
            captured.update(kwargs)
            return _fake_extract_summary(tmp_path / "mq.md")

        _patch_happy_path(monkeypatch, tmp_path)
        monkeypatch.setattr(deadline, "run_extraction", fake_run_extraction)

        deadline.main(["--skip-ingest", "--no-nuance"])

        assert captured.get("force_refresh") is True


class TestStageFailures:
    def test_ingest_raising_is_warned_but_never_aborts_the_run(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        """Stage 1 is a best-effort cache warm-up. `run_ingest` is the Phase 1
        demo runner (stops at 3 transcripts) and stage 2 refreshes every
        creator's video itself — letting stage 1 gate the chain would mean
        the least important stage could cost a deadline-morning squad."""
        _patch_happy_path(monkeypatch, tmp_path)
        monkeypatch.setattr(deadline, "ingest_main", _raiser(RuntimeError("youtube quota hit")))

        rc = deadline.main(["--no-nuance"])

        assert rc == 0
        captured = capsys.readouterr()
        assert "WARNING" in captured.err
        assert "youtube quota hit" in captured.err
        assert "STAGE FAILED" not in captured.err

    def test_ingest_nonzero_return_is_warned_but_never_aborts(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        """run_ingest exits 1 whenever it saves fewer than its 3-transcript
        target — routine, and not a signal that this rerun is unhealthy."""
        _patch_happy_path(monkeypatch, tmp_path)
        monkeypatch.setattr(deadline, "ingest_main", lambda: 1)

        rc = deadline.main(["--no-nuance"])

        assert rc == 0
        captured = capsys.readouterr()
        assert "WARNING" in captured.err
        assert "STAGE FAILED" not in captured.err

    def test_zero_successful_extractions_is_fatal(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        """Per-creator failures are non-fatal inside run_extraction, so an
        all-failed run returns as quietly as a perfect one. Continuing would
        solve on the PREVIOUS run's extractions and ship a stale squad that
        looks entirely normal — the worst outcome available here."""
        _patch_happy_path(monkeypatch, tmp_path)
        monkeypatch.setattr(
            deadline,
            "run_extraction",
            lambda *a, **k: _fake_extract_summary(
                tmp_path / "mq.md", attempted=18, succeeded=0, failures=[("a", "no video")] * 18
            ),
        )
        monkeypatch.setattr(
            deadline, "run_pipeline", _raiser(AssertionError("must not solve on stale extractions"))
        )

        rc = deadline.main(["--skip-ingest", "--no-nuance"])

        assert rc == 1
        err = capsys.readouterr().err
        assert "STAGE FAILED" in err
        assert "stale" in err

    def test_partial_extraction_warns_but_continues(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        """Some evidence still beats none — warn loudly, but ship."""
        _patch_happy_path(monkeypatch, tmp_path)
        monkeypatch.setattr(
            deadline,
            "run_extraction",
            lambda *a, **k: _fake_extract_summary(
                tmp_path / "mq.md", attempted=18, succeeded=4, failures=[("a", "no video")] * 14
            ),
        )

        rc = deadline.main(["--skip-ingest", "--no-nuance"])

        assert rc == 0
        assert "WARNING" in capsys.readouterr().err

    def test_summary_failure_still_reports_where_the_squad_is(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        """Everything of value is on disk before the summary prints. A
        formatting slip must not read as 'the run failed'."""
        _patch_happy_path(monkeypatch, tmp_path)
        monkeypatch.setattr(
            deadline, "_print_final_summary", _raiser(RuntimeError("bad format string"))
        )

        rc = deadline.main(["--skip-ingest", "--skip-extract", "--no-nuance"])

        assert rc == 0
        err = capsys.readouterr().err
        assert "ARE written" in err
        assert "run-new" in err

    def test_extract_failure_exits_nonzero_and_prints_resume(self, monkeypatch, capsys) -> None:
        monkeypatch.setattr(deadline, "run_extraction", _raiser(RuntimeError("claude API down")))
        monkeypatch.setattr(
            deadline, "run_pipeline", _raiser(AssertionError("must not run — extract failed"))
        )

        rc = deadline.main(["--skip-ingest"])

        assert rc == 1
        err = capsys.readouterr().err
        assert "STAGE FAILED" in err
        # RETRY the failed stage; skipping it is offered only as an explicit
        # fallback, because it would ship a squad built on stale extractions.
        assert deadline.RESUME_AFTER_INGEST in err
        assert "stale" in err

    def test_solve_failure_exits_nonzero_and_prints_resume(self, monkeypatch, capsys) -> None:
        monkeypatch.setattr(deadline, "run_pipeline", _raiser(RuntimeError("solver infeasible")))

        rc = deadline.main(["--skip-ingest", "--skip-extract"])

        assert rc == 1
        err = capsys.readouterr().err
        assert "STAGE FAILED" in err
        assert deadline.RESUME_AFTER_EXTRACT in err

    def test_earlier_stage_output_is_never_touched_on_later_failure(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """A stage-3 failure must not delete/overwrite anything stage 1/2
        already wrote to disk — deadline.py has no code path that deletes
        under data/runs/ at all, so this asserts the absence of any such
        call by construction: the extraction-output path fake never gets
        torn down, and no `runs_dir`-clearing helper exists to invoke."""
        marker = tmp_path / "extractions" / "vid1.json"
        marker.parent.mkdir(parents=True)
        marker.write_text("{}")

        monkeypatch.setattr(deadline, "ingest_main", lambda: 0)
        monkeypatch.setattr(
            deadline,
            "run_extraction",
            lambda *a, **k: _fake_extract_summary(marker.parent / "mq.md"),
        )
        monkeypatch.setattr(deadline, "run_pipeline", _raiser(RuntimeError("solver infeasible")))
        monkeypatch.setattr(deadline, "DEFAULT_RUNS_DIR", tmp_path / "runs")

        rc = deadline.main([])

        assert rc == 1
        assert marker.exists()


# ---------------------------------------------------------------------------
# Nuance wiring: on by default here (unlike pipeline.main()), off with
# --no-nuance. The actual non-fatal-failure behaviour is exercised against
# `run_pipeline` directly in tests/test_pipeline.py; here we only check
# deadline.py wires the right thing into `run_pipeline`'s `nuance_fn`.
# ---------------------------------------------------------------------------


class TestNuanceWiring:
    def test_nuance_on_by_default_wires_generate_nuance(self, monkeypatch, tmp_path: Path) -> None:
        captured: dict = {}
        _patch_happy_path(monkeypatch, tmp_path, captured)

        rc = deadline.main([])

        assert rc == 0
        from fpl_oracle.report.nuance import generate_nuance

        assert captured["nuance_fn"] is generate_nuance

    def test_no_nuance_flag_disables_the_pass(self, monkeypatch, tmp_path: Path) -> None:
        captured: dict = {}
        _patch_happy_path(monkeypatch, tmp_path, captured)

        rc = deadline.main(["--no-nuance"])

        assert rc == 0
        assert captured["nuance_fn"] is None


class TestForceRefreshWiring:
    def test_solve_stage_always_forces_refresh(self, monkeypatch, tmp_path: Path) -> None:
        captured: dict = {}
        _patch_happy_path(monkeypatch, tmp_path, captured)

        deadline.main(["--no-nuance"])

        assert captured["force_refresh"] is True


# ---------------------------------------------------------------------------
# Squad diff: `load_previous_run`/`diff_vs_previous`/`SquadDiff` moved to
# `pipeline.py` (see this module's docstring) — their tests moved to
# tests/test_pipeline.py along with them. What's left here is that
# `main()` reads the diff off `result.diff` rather than computing it.
# ---------------------------------------------------------------------------


class TestFinalSummaryReadsResultDiff:
    def test_main_passes_result_diff_to_the_summary(self, monkeypatch, tmp_path: Path) -> None:
        captured: dict = {}
        expected_diff = _fake_diff(previous_run_id="run-old")

        def fake_run_pipeline(**kwargs):
            return _fake_result(diff=expected_diff)

        def fake_print_final_summary(result, diff, db):
            captured["diff"] = diff

        monkeypatch.setattr(deadline, "ingest_main", lambda: 0)
        monkeypatch.setattr(
            deadline, "run_extraction", lambda *a, **k: _fake_extract_summary(tmp_path / "mq.md")
        )
        monkeypatch.setattr(deadline, "run_pipeline", fake_run_pipeline)
        monkeypatch.setattr(deadline, "_print_final_summary", fake_print_final_summary)
        monkeypatch.setattr(deadline, "DEFAULT_RUNS_DIR", tmp_path / "runs")
        monkeypatch.setattr(
            PlayerDB, "load", classmethod(lambda cls, force_refresh=False: _fake_db())
        )

        rc = deadline.main(["--skip-ingest", "--no-nuance"])

        assert rc == 0
        assert captured["diff"] is expected_diff
