"""Pure tests for render_report.py: the figures block matches the published key set, and the
report sections render from the tool's readiness dict (taken from the fixture's reference run)."""

from __future__ import annotations

import json
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
