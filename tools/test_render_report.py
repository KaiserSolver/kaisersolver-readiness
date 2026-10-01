"""Pure tests for render_report.py: the figures block matches the published key set, and the
report sections render from the tool's readiness dict (taken from the fixture's reference run)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import rebuild_state  # noqa: E402
import render_report as rr  # noqa: E402
import rows  # noqa: E402

FIX = HERE / "fixtures" / "monthly"
RUNS = FIX / "runs" / "arbitrum-one"
PARAMS = json.loads((FIX / "fixture.json").read_text())
EXPECTED = json.loads((FIX / "expected_full.json").read_text())
PUBLISHED_RUN = HERE.parent / "runs" / "base" / "2026-09-14" / "run.json"
SCREEN = "====\n  READINESS — kaisersolver   [NOT READY]\n  arbitrum-one · prod · blocks 1..2\n===="


@pytest.fixture(scope="module")
def ours() -> list[dict[str, Any]]:
    read = rows.read_jsonl([RUNS / "fixture-a.jsonl", RUNS / "fixture-b.jsonl"])
    windowed = rows.dedupe_in_window(read.rows, rows.Window(*PARAMS["window_full"]))
    return rebuild_state.attempted_rows(windowed.rows, PARAMS["solver"])


@pytest.fixture(scope="module")
def rep() -> dict[str, Any]:
    return dict(EXPECTED["readiness"], native="ETH")


@pytest.fixture(scope="module")
def fig(rep: dict[str, Any], ours: list[dict[str, Any]]) -> rr.PublicFigures:
    return rr.figures(rep, ours, PARAMS["solver"], PARAMS["own_addresses"])


def opts(**over: Any) -> rr.ReportOptions:
    base: dict[str, Any] = dict(
        tool_version="0.11.3",
        rows_tool_version="0.11.3",
        watch_seconds=60,
        instance_note="the production Arbitrum instance",
        budget_note=None,
        bottom_line=None,
        regenerated_at="2026-10-02T06:00:00Z",
        manifest=None,
        duplicates=0,
        overlap_blocks=0,
        launches=1,
    )
    base.update(over)
    return rr.ReportOptions(**base)


def test_figures_keys_match_the_published_record() -> None:
    published = list(json.loads(PUBLISHED_RUN.read_text())["figures"])
    assert rr.PublicFigures.keys() == published


def test_figures_values_from_the_reference_run(fig: rr.PublicFigures) -> None:
    assert (fig.lo, fig.hi) == tuple(PARAMS["window_full"])
    assert fig.attempted == 40 and fig.replayed == 37 and fig.returned == 31
    assert fig.arte == [8339062] and fig.verdict == "NOT READY"
    assert fig.own_wins == sum(1 for i in range(40) if i % 3 == 0)  # make_fixture: every third clone is ours
    assert fig.span[0] < fig.span[1] and fig.span_h is not None and fig.span_h > 0
    assert fig.lag_med_min is not None and fig.lag_med_min > 0
    assert set(fig.warns) == {"prices look plausible", "winner surplus plausible"}
    assert fig.skip == {} and fig.to_dict()["arte"] == [8339062]


GOLDEN_FIGURES = {
    "lo": 491743288, "hi": 491977488, "attempted": 40, "replayed": 37, "returned": 31, "valid": 30,
    "transport": 1, "deadline_miss": 2, "p50": 585, "p95": 770, "budget": 4.84,
    "valid_pct": 96.7741935483871, "fair_eval": 31, "fair_filtered": 0,
    "capture": 96344.30524436326, "capture_x": 9917191.779159348, "arte": [8339062], "own_wins": 14,
    "perbid": 1.0, "n_perbid": 28, "flagged": 2, "udcp": 31, "udcp_v": 0, "verdict": "NOT READY",
    "warns": ["prices look plausible", "winner surplus plausible"],
    "span": ["2026-03-02T00:00:00Z", "2026-03-02T16:15:00Z"], "span_h": 16.25, "rate": 2.46, "found": 40,
    "excluded": 0, "skip": {}, "unscanned": 0, "lag_med_min": 101.39999999999999,
}


def test_figures_equal_the_golden_block(fig: rr.PublicFigures) -> None:
    assert fig.to_dict() == GOLDEN_FIGURES


def test_figures_are_mapped_from_the_tools_own_readiness_fields(fig: rr.PublicFigures) -> None:
    """Each published figure against the tool's field it must come from, so a mis-mapped key
    (p95 from p50, capture from capture_ex_artefact, ...) cannot hide behind a self-consistent golden."""
    r = EXPECTED["readiness"]
    assert (fig.p50, fig.p95) == (r["p50_ms"], r["p95_ms"]) and fig.p50 != fig.p95
    assert (fig.capture, fig.capture_x) == (r["capture_pct"], r["capture_ex_artefact_pct"]) and fig.capture != fig.capture_x
    assert fig.valid_pct == r["valid_rate_pct"] and fig.valid_pct != r["bid_rate_pct"]
    assert fig.rate == r["auctions_per_hour"] and fig.found == r["counts"]["settlements_found"]
    assert (fig.attempted, fig.replayed, fig.returned, fig.valid) == (
        r["auctions_attempted"], r["answered"], r["bids"], r["valid"],
    )
    assert (fig.transport, fig.deadline_miss, fig.excluded, fig.unscanned) == (
        r["transport"], r["deadline_miss"], r["excluded"]["n"], r["unscanned_blocks"],
    )
    assert (fig.fair_eval, fig.fair_filtered, fig.udcp, fig.udcp_v) == (
        r["fairness_evaluated"], r["fairness_filtered"], r["udcp_checked"], r["udcp_violations"],
    )
    assert (fig.perbid, fig.n_perbid, fig.flagged, fig.budget) == (
        r["per_bid_median_ratio"], r["per_bid_ratio_n"], r["implausible"], r["budget_s"],
    )


def _row(aid: int, winner: int, n_sol: int = 0, ts: int | None = None, age: float | None = None, flagged: bool = False,
         submitter: str | None = None, quality: str = "exact_uniform", best: int = 0) -> dict[str, Any]:
    r: dict[str, Any] = {
        "auction_id": aid, "winner_surplus_wei": winner, "baseline_quality": quality,
        "solvers": {"s": {"n_solutions": n_sol, "best_surplus_wei": best, "flags": ["implausible_surplus"] if flagged else []}},
    }
    if ts is not None:
        r["block_ts"] = ts
    if age is not None:
        r["age_hours"] = age
    if submitter is not None:
        r["winner_txs"] = [{"submitter": submitter}]
    return r


def test_settlement_span_and_lag_on_hand_computed_rows() -> None:
    rs = [_row(1, 1, ts=1000, age=1.0), _row(2, 1, ts=4600, age=0.5), _row(3, 1, ts=2800, age=2.0)]
    assert rr.settlement_span(rs) == ("1970-01-01T00:16:40Z", "1970-01-01T01:16:40Z", 1.0)
    assert rr.replay_lag_minutes(rs) == 60.0  # median of 60, 30, 120 minutes
    assert rr.settlement_span([_row(1, 1, ts=7)]) == ("1970-01-01T00:00:07Z", "1970-01-01T00:00:07Z", 0.0)
    assert rr.settlement_span([_row(1, 1)]) == ("n/a", "n/a", None) and rr.replay_lag_minutes([_row(1, 1)]) is None


def test_top_five_on_hand_computed_rows() -> None:
    rs = [_row(i, w, n_sol=i % 2) for i, w in enumerate((10, 20, 30, 40, 50, 60, 70), start=1)]
    share, entered = rr.top_five(rs, "s", set())
    assert share == 100 * (70 + 60 + 50 + 40 + 30) / 280  # five rows, not four
    assert entered == 3  # auctions 7, 5, 3 carry a solution of ours; 6 and 4 do not
    share_x, entered_x = rr.top_five(rs, "s", {7})
    assert share_x == 100 * (60 + 50 + 40 + 30 + 20) / 210 and entered_x == 2
    assert rr.top_five([_row(1, 0)], "s", set()) == (None, 0)


def test_plausibility_on_hand_computed_rows() -> None:
    rs = [
        _row(1, 1000), _row(2, 1000), _row(3, 1000),
        _row(4, 10, flagged=True, best=500, quality="exact_uniform"),
        _row(5, 400, flagged=True, best=800, quality="mixed"),
    ]
    p = rr.plausibility(rs, "s")
    assert p.n == 2 and p.by_type == {"exact_uniform": 1, "mixed": 1}
    assert p.largest_ratio == 50.0  # 500/10 beats 800/400
    assert p.window_median_wei == 1000 and p.below_tenth == 1  # only the winner of 10 is under 1000/10
    assert p.flagged_median_wei == 205


def test_own_wins_counts_case_insensitively() -> None:
    rs = [_row(1, 1, submitter="0xABC"), _row(2, 1, submitter="0xdef"), _row(3, 1, submitter="0xabc"), _row(4, 1)]
    assert rr.own_wins(rs, ["0xAbC"]) == 2 and rr.own_wins(rs, ["0xabc", "0xdef"]) == 3 and rr.own_wins(rs, []) is None


def test_pct_and_native_amount_formats() -> None:
    assert rr.pct(1.005, 1) == "1.0 %" and rr.pct(96.7741935, 2) == "96.77 %" and rr.pct(None) == "n/a"
    assert rr.native_amount(1_500_000_000_000_000_000) == "1.5000000"


GOLDEN_REPORT = FIX / "golden_report.md"


def test_report_matches_the_golden_snapshot(rep: dict[str, Any], fig: rr.PublicFigures, ours: list[dict[str, Any]]) -> None:
    text = rr.render(rep, SCREEN, fig, ours, opts())
    if os.environ.get("UPDATE_GOLDEN"):
        GOLDEN_REPORT.write_text(text)
    assert text == GOLDEN_REPORT.read_text(), "report text changed; if intended rerun with UPDATE_GOLDEN=1"


def test_plausibility_counts_flagged_rows(ours: list[dict[str, Any]]) -> None:
    p = rr.plausibility(ours, PARAMS["solver"])
    assert p.n == 2 and sum(p.by_type.values()) == 2
    assert p.largest_ratio is not None and p.largest_ratio > 10
    assert p.window_median_wei is not None and p.window_median_wei > 0


def test_top_five_excludes_artefacts(ours: list[dict[str, Any]]) -> None:
    share_all, _ = rr.top_five(ours, PARAMS["solver"], set())
    share_ex, entered = rr.top_five(ours, PARAMS["solver"], {8339062})
    assert share_all is not None and share_ex is not None and share_all > share_ex
    assert 0 <= entered <= 5


def test_render_has_every_section(rep: dict[str, Any], fig: rr.PublicFigures, ours: list[dict[str, Any]]) -> None:
    text = rr.render(rep, SCREEN, fig, ours, opts())
    assert text.startswith("# Readiness report: kaisersolver on arbitrum-one — 2026-03-02\n")
    for heading in ("## Reading the verdict", "## Method notes", "## Reproduce", "**Bottom line for Arbitrum:**"):
        assert heading in text
    assert "```\n" + SCREEN + "\n```" in text  # the screen is embedded verbatim
    assert "- **Valuation artefact — auction 8339062:**" in text
    assert "- **FAIL — no transport errors:**" in text and "- **WARN — prices look plausible:**" in text
    assert "Body archive manifest" not in text  # no manifest given
    assert "The watch ran in 1 launch(es). 0 auction(s) seen more than once" in text


def test_bottom_line_default_and_override(rep: dict[str, Any], fig: rr.PublicFigures, ours: list[dict[str, Any]]) -> None:
    default = rr.render(rep, SCREEN, fig, ours, opts())
    assert "**Bottom line for Arbitrum:** NOT READY: 2 failing: no transport errors, inside the deadline" in default
    custom = rr.render(rep, SCREEN, fig, ours, opts(bottom_line="Reviewed by hand."))
    assert "**Bottom line for Arbitrum:** Reviewed by hand." in custom


def test_method_notes_disclose_restarts_and_version_drift(
    rep: dict[str, Any], fig: rr.PublicFigures, ours: list[dict[str, Any]]
) -> None:
    text = rr.render(rep, SCREEN, fig, ours, opts(launches=2, duplicates=4, overlap_blocks=18201, rows_tool_version="0.11.2"))
    assert "- The watch ran in 2 launch(es). 4 auction(s) seen more than once keep their first replay; " in text
    assert "18201 block(s) were scanned more than once" in text
    assert (
        "- The rows were produced by cow-backtester 0.11.2; the verdict and screen come from `readiness_report` of 0.11.3" in text
    )
    assert "with cow-backtester **0.11.2**" in text  # the intro names the version that ran


def test_manifest_line_and_archive_note(
    tmp_path: Path, rep: dict[str, Any], fig: rr.PublicFigures, ours: list[dict[str, Any]]
) -> None:
    m = tmp_path / "manifest.jsonl"
    m.write_text('{"a":1}\n{"a":2}\n')
    facts = rr.ManifestFacts.read(m)
    text = rr.render(rep, SCREEN, fig, ours, opts(manifest=facts))
    assert facts.lines == 2 and f"SHA-256 `{facts.sha256}` · 2 lines · " in text
    assert "- Bodies were archived (`--archive-bodies`, SHA-256 per body)" in text
    with pytest.raises(SystemExit, match="cannot read body-archive manifest"):
        rr.ManifestFacts.read(tmp_path / "missing.jsonl")


def test_verdict_tail_shapes() -> None:
    ok = {"checks": [{"level": "ok", "label": "a"}]}
    warn = {"checks": [{"level": "ok", "label": "a"}, {"level": "warn", "label": "b"}]}
    fail = {"checks": [{"level": "fail", "label": "c"}, {"level": "warn", "label": "b"}]}
    assert rr.verdict_tail(ok) == "every check passed"
    assert rr.verdict_tail(warn) == "no failing check; 1 warn-level: b"
    assert rr.verdict_tail(fail) == "1 failing: c; 1 warn-level: b"


def test_own_wins_without_an_address_is_not_a_zero(rep: dict[str, Any], ours: list[dict[str, Any]]) -> None:
    fig_none = rr.figures(rep, ours, PARAMS["solver"], [])
    assert fig_none.own_wins is None
    text = rr.render(rep, SCREEN, fig_none, ours, opts())
    assert "- **Own wins:** not configured" in text and "Own wins:** 0 of" not in text
