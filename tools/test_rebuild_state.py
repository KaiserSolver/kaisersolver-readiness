"""Parity tests: the state rebuilt from a run's rows equals the tool's own state from a one-shot
run over the same auctions (captured by make_fixture.py into expected_*.json)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import rebuild_state  # noqa: E402
import rows  # noqa: E402

FIX = HERE / "fixtures" / "monthly"
RUNS = FIX / "runs" / "arbitrum-one"
FILE1, FILE2 = RUNS / "2026-10-01T000000Z.jsonl", RUNS / "2026-10-01T090000Z.jsonl"
PARAMS = json.loads((FIX / "fixture.json").read_text())
SOLVER = PARAMS["solver"]
# what readiness_report reads (rank_* / econ_rows are for the scorecard and the tool's own rows)
COMPARED = [k for k in rebuild_state.SolverStat.__dataclass_fields__ if k not in ("rank_rows", "econ_rows")]


def rebuilt(files: list[Path], window: list[int]) -> dict[str, Any]:
    read = rows.read_jsonl(files)
    w = rows.Window(*window)
    windowed = rows.dedupe_in_window(read.rows, w)
    cycles = rows.select_cycles(read.metas, w)
    return rebuild_state.build_state(windowed.rows, cycles, w, SOLVER, "ETH").to_dict()


def comparable(stat: dict[str, Any]) -> dict[str, Any]:
    out = {k: stat[k] for k in COMPARED if k in stat}
    out["latency"] = sorted(out["latency"])
    for k in ("invalid", "errors", "basis_mix"):
        out[k] = dict(out[k])
    return out


@pytest.mark.parametrize(
    ("files", "expected_name", "window_key", "overlap"),
    [
        ([FILE1], "expected_file1.json", "window_file1", False),
        ([FILE1, FILE2], "expected_full.json", "window_full", True),  # restart: the field line double-counts the re-scan
    ],
)
def test_counters_match_the_tools_own_state(files: list[Path], expected_name: str, window_key: str, overlap: bool) -> None:
    expected = json.loads((FIX / expected_name).read_text())["state"]
    st = rebuilt(files, PARAMS[window_key])
    assert comparable(st["per_solver"][SOLVER]) == comparable(expected["per_solver"][SOLVER])
    if overlap:
        assert len(st["txs"]) > expected["settlements_found"] and st["n_found"] > expected["auctions_formed"]
    else:
        assert len(st["txs"]) == expected["settlements_found"] and st["n_found"] == expected["auctions_formed"]
    assert st["skip"] == expected["skip"]
    assert st["failed_ranges"] == [tuple(r) for r in expected["failed_ranges"]]
    assert sorted(st["attempted_ts"]) == expected["attempted_ts"]
    assert sorted(st["attempted_start_ts"]) == expected["attempted_start_ts"]
    assert sorted(st["budget_upper"]) == expected["budget_upper"]
    assert len(st["rows"]) == expected["rows"]


def test_restart_overlap_does_not_double_count_auctions_but_does_inflate_field_counts() -> None:
    """Both files: the tool's one-shot reference counts each settlement once; the summed cycles
    count the re-scanned overlap twice. select_cycles publishes that, build_state keeps the sum."""
    read = rows.read_jsonl([FILE1, FILE2])
    w = rows.Window(*PARAMS["window_full"])
    sel = rows.select_cycles(read.metas, w)
    expected = json.loads((FIX / "expected_full.json").read_text())["state"]
    assert sel.overlap_blocks > 0
    assert sel.settlements_found >= expected["settlements_found"]


def test_late_answer_appends_latency_timeout_does_not() -> None:
    s = rebuild_state.SolverStat()
    s.accrue({"winner_surplus_wei": 10}, {"outcome": "deadline_miss", "solve_error": "late", "latency_ms": 5400})
    s.accrue({"winner_surplus_wei": 10}, {"outcome": "deadline_miss", "solve_error": "timeout", "latency_ms": 9840})
    assert s.deadline_miss == 2 and s.latency == [5400] and s.attempted == 2
    assert dict(s.errors) == {"late": 1, "timeout": 1} and s.lost_to_errors == 20


def test_bad_solver_response_reason_is_normalised() -> None:
    s = rebuild_state.SolverStat()
    s.accrue({}, {"outcome": "transport", "solve_error": "bad_solver_response (ValueError)", "latency_ms": 12})
    assert s.transport == 1 and dict(s.errors) == {"bad_solver_response": 1} and s.latency == []


def test_answer_accounting() -> None:
    s = rebuild_state.SolverStat()
    row = {"winner_surplus_wei": 100, "baseline_quality": "exact_uniform"}
    s.accrue(
        row,
        {
            "outcome": "answered",
            "latency_ms": 300,
            "n_solutions": 1,
            "n_valid": 1,
            "best_surplus_wei": 150,
            "fairness": "evaluated",
            "fairness_filtered": 0,
            "valid_zero_surplus": 0,
            "udcp_checked": 2,
            "udcp_violations": 0,
            "invalid": {"limit_violation": 1},
            "flags": ["implausible_surplus"],
        },
    )
    s.accrue(
        row,
        {
            "outcome": "answered",
            "latency_ms": 200,
            "n_solutions": 0,
            "n_valid": 0,
            "best_surplus_wei": 0,
            "fairness": "not_evaluated",
        },
    )
    d = s.to_dict()
    assert (d["attempted"], d["replayed"], d["returned"], d["valid"]) == (2, 2, 1, 1)
    assert (d["positive"], d["beat"], d["implausible"]) == (1, 1, 1)
    assert d["our_surplus"] == 150 and d["winner_surplus"] == 200 and d["winner_surplus_attempted"] == 200
    assert d["our_surplus_exact"] == 150 and d["winner_surplus_exact"] == 200
    assert d["fairness_evaluated"] == 1 and d["fairness_not_evaluated"] == 0  # an empty answer is not evaluated
    assert dict(d["invalid"]) == {"limit_violation": 1} and dict(d["basis_mix"]) == {"exact_uniform": 2}
    assert d["udcp_checked"] == 2 and d["latency"] == [300, 200]


def test_state_dict_shape_for_readiness_report() -> None:
    st = rebuilt([FILE1], PARAMS["window_file1"])
    assert set(st) >= {
        "per_solver",
        "rows",
        "from_block",
        "to_block",
        "txs",
        "n_found",
        "skip",
        "failed_ranges",
        "attempted_ts",
        "attempted_start_ts",
        "budget_upper",
        "agg",
        "native",
        "interrupted",
    }
    assert st["agg"] == {"validto_clamped_auctions": 0} and st["native"] == "ETH"
