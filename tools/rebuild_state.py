"""Rebuild the state `cow_backtester.backtest.readiness_report` reads from a run's rows.

`process_window` accrues one counter dict per solver while it replays (backtest.py, the
`per_solver` init and the dispatch loop). Under `--watch` it does so per cycle and prints a
screen per cycle; nothing in the tool sums cycles. This module replays that accounting over
the rows of a whole run, one auction once, so the tool's own `readiness_report()` can be run
over the cumulative counters. Every rule here mirrors a line of the tool; the parity test
compares the result with the tool's own state from a one-shot run over the same auctions.

Stdlib only; the tool is imported by build_report.py, not here.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from typing import Any

from rows import CycleSelection, Row, Window

FAILURE_OUTCOMES = ("transport", "deadline_miss")


def normalise_reason(solve_error: str | None) -> str:
    """The tool counts `errors["bad_solver_response"]` but writes the row's `solve_error` as
    `bad_solver_response (ValueError)`; the counter key is the part before the parenthesis."""
    return (solve_error or "unknown").split(" (", 1)[0]


@dataclass
class SolverStat:
    """The per-solver dict `process_window` starts from, field for field."""

    replayed: int = 0
    returned: int = 0
    valid: int = 0
    positive: int = 0
    beat: int = 0
    our_surplus: int = 0
    winner_surplus: int = 0
    implausible: int = 0
    attempted: int = 0
    winner_surplus_attempted: int = 0
    lost_to_errors: int = 0
    our_surplus_exact: int = 0
    winner_surplus_exact: int = 0
    rank_rows: list[Any] = field(default_factory=list)
    econ_rows: list[Any] = field(default_factory=list)
    invalid: Counter[str] = field(default_factory=Counter)
    errors: Counter[str] = field(default_factory=Counter)
    latency: list[int] = field(default_factory=list)
    transport: int = 0
    deadline_miss: int = 0
    fairness_filtered: int = 0
    fairness_evaluated: int = 0
    fairness_not_evaluated: int = 0
    valid_zero_surplus: int = 0
    udcp_checked: int = 0
    udcp_violations: int = 0
    basis_mix: Counter[str] = field(default_factory=Counter)

    def accrue(self, row: Row, vs: dict[str, Any]) -> None:
        """One auction the solver was sent: the shared prologue, then the outcome branch."""
        winner_total = int(row.get("winner_surplus_wei") or 0)
        quality = str(row.get("baseline_quality") or "mixed")
        self.attempted += 1
        self.winner_surplus_attempted += winner_total
        self.basis_mix[quality] += 1
        if quality == "exact_uniform":
            self.winner_surplus_exact += winner_total
        if vs.get("outcome") == "answered":
            self._accrue_answer(vs, winner_total, quality)
        else:
            self._accrue_failure(vs, winner_total)

    def _accrue_failure(self, vs: dict[str, Any], winner_total: int) -> None:
        outcome = vs.get("outcome")
        bucket = outcome if outcome in FAILURE_OUTCOMES else "transport"
        setattr(self, bucket, getattr(self, bucket) + 1)
        reason = normalise_reason(vs.get("solve_error"))
        self.errors[reason] += 1
        self.lost_to_errors += winner_total
        # a LATE answer is still an answer for latency purposes; a timeout or a transport
        # failure has nothing to time (the tool appends only when err is None)
        if reason == "late" and vs.get("latency_ms") is not None:
            self.latency.append(int(vs["latency_ms"]))

    def _accrue_answer(self, vs: dict[str, Any], winner_total: int, quality: str) -> None:
        if vs.get("latency_ms") is not None:
            self.latency.append(int(vs["latency_ms"]))
        self.replayed += 1
        self.fairness_filtered += int(vs.get("fairness_filtered") or 0)
        self.valid_zero_surplus += int(vs.get("valid_zero_surplus") or 0)
        self.udcp_checked += int(vs.get("udcp_checked") or 0)
        self.udcp_violations += int(vs.get("udcp_violations") or 0)
        n_solutions = int(vs.get("n_solutions") or 0)
        if n_solutions:
            if vs.get("fairness") == "evaluated":
                self.fairness_evaluated += 1
            else:
                self.fairness_not_evaluated += 1
        self.winner_surplus += winner_total
        ours = int(vs.get("best_surplus_wei") or 0)
        self.our_surplus += ours
        if quality == "exact_uniform":
            self.our_surplus_exact += ours
        if n_solutions:
            self.returned += 1
        if int(vs.get("n_valid") or 0):
            self.valid += 1
        if ours > 0:
            self.positive += 1
        if ours > winner_total:
            self.beat += 1
        for k, v in (vs.get("invalid") or {}).items():
            self.invalid[str(k)] += int(v)
        if "implausible_surplus" in (vs.get("flags") or []):
            self.implausible += 1

    def to_dict(self) -> dict[str, Any]:
        """The dict readiness_report indexes (Counters stay Counters, as in the tool)."""
        return asdict(self) | {"invalid": self.invalid, "errors": self.errors, "basis_mix": self.basis_mix}


def attempted_rows(rows: Iterable[Row], solver: str) -> list[Row]:
    """The rows on which this solver was sent the auction (answered or not)."""
    return [r for r in rows if solver in (r.get("solvers") or {})]


@dataclass
class RunState:
    """Everything readiness_report reads from `st`, rebuilt for a whole run."""

    solver: str
    stat: SolverStat
    rows: list[Row]
    window: Window
    settlements_found: int
    auctions_formed: int
    skip: dict[str, int]
    failed_ranges: list[tuple[int, int]]
    attempted_ts: list[int]
    attempted_start_ts: list[int]
    budget_upper: list[float]
    validto_clamped_auctions: int
    native: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "per_solver": {self.solver: self.stat.to_dict()},
            "rows": self.rows,
            "from_block": self.window.lo,
            "to_block": self.window.hi,
            "txs": [None] * self.settlements_found,  # readiness reads only len(st["txs"])
            "n_found": self.auctions_formed,
            "skip": dict(self.skip),
            "failed_ranges": list(self.failed_ranges),
            "attempted_ts": list(self.attempted_ts),
            "attempted_start_ts": list(self.attempted_start_ts),
            "budget_upper": list(self.budget_upper),
            "agg": {"validto_clamped_auctions": self.validto_clamped_auctions},
            "native": self.native,
            "interrupted": False,
        }


def build_state(rows: list[Row], cycles: CycleSelection, w: Window, solver: str, native: str) -> RunState:
    """`rows` are already one-per-auction and inside `w` (rows.dedupe_in_window)."""
    ours = attempted_rows(rows, solver)
    stat = SolverStat()
    for r in ours:
        stat.accrue(r, r["solvers"][solver])
    return RunState(
        solver=solver,
        stat=stat,
        rows=rows,
        window=w,
        settlements_found=cycles.settlements_found,
        auctions_formed=cycles.auctions_formed,
        skip=cycles.skipped(),
        failed_ranges=cycles.failed_ranges(w),
        attempted_ts=[int(r["block_ts"]) for r in ours if r.get("block_ts") is not None],
        attempted_start_ts=[int(r["auction_start_ts"]) for r in ours if r.get("auction_start_ts") is not None],
        budget_upper=[float(r["original_budget_upper_s"]) for r in ours if r.get("original_budget_upper_s") is not None],
        validto_clamped_auctions=sum(1 for r in ours if r.get("validto_clamped")),
        native=native,
    )
