"""Render `report.md` and the public figures in the shape of the published reports
(runs/*/2026-09-14/report.md). Every number comes from the tool's readiness dict or the
rows; the only editorial line ("Bottom line") is a neutral computed sentence unless the
operator supplies one. Stdlib only.
"""

from __future__ import annotations

import hashlib
import statistics
from collections.abc import Iterable
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rows import Row

# CoW's `settlement_reverted` is not an exclusion from the field (backtest.py, _NON_EXCLUSION_SKIPS);
# the published `figures.skip` lists exclusions only.
NON_EXCLUSION_SKIPS = frozenset({"settlement_reverted"})
CHAIN_TITLES = {
    "arbitrum-one": "Arbitrum",
    "base": "Base",
    "bnb": "BNB",
    "xdai": "Gnosis",
    "polygon": "Polygon",
    "mainnet": "Ethereum",
}


@dataclass(frozen=True)
class PublicFigures:
    """The `figures` block of the published run.json files, key for key, in their order."""

    lo: int
    hi: int
    attempted: int
    replayed: int
    returned: int
    valid: int
    transport: int
    deadline_miss: int
    p50: int | None
    p95: int | None
    budget: float
    valid_pct: float | None
    fair_eval: int
    fair_filtered: int
    capture: float | None
    capture_x: float | None
    arte: list[int]
    own_wins: int
    perbid: float | None
    n_perbid: int
    flagged: int
    udcp: int
    udcp_v: int
    verdict: str
    warns: list[str]
    span: list[str]
    span_h: float | None
    rate: float | None
    found: int
    excluded: int
    skip: dict[str, int]
    unscanned: int
    lag_med_min: float | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def keys() -> list[str]:
        return [f.name for f in fields(PublicFigures)]


@dataclass(frozen=True)
class Plausibility:
    """Behind the 'On "prices look plausible"' paragraph."""

    n: int
    by_type: dict[str, int]
    largest_ratio: float | None
    below_tenth: int
    flagged_median_wei: int | None
    window_median_wei: int | None


@dataclass(frozen=True)
class ManifestFacts:
    sha256: str
    lines: int
    mtime: str

    @classmethod
    def read(cls, path: Path) -> ManifestFacts:
        try:
            data = path.read_bytes()
            mtime = path.stat().st_mtime
        except OSError as e:
            raise SystemExit(f"cannot read body-archive manifest {path}: {e}") from e
        return cls(
            hashlib.sha256(data).hexdigest(),
            data.count(b"\n"),
            datetime.fromtimestamp(mtime, timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ"),
        )


@dataclass(frozen=True)
class ReportOptions:
    tool_version: str
    rows_tool_version: str
    watch_seconds: int
    instance_note: str
    budget_note: str | None
    bottom_line: str | None
    regenerated_at: str
    manifest: ManifestFacts | None
    duplicates: int
    overlap_blocks: int
    launches: int


# ------------------------------------------------------------------ small helpers


def iso(ts: float | None) -> str:
    if ts is None:
        return "n/a"
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def pct(x: float | None, nd: int = 2) -> str:
    return "n/a" if x is None else f"{x:.{nd}f} %"


def native_amount(wei: int) -> str:
    return f"{wei / 1e18:.7f}"


def chain_title(chain: str) -> str:
    return CHAIN_TITLES.get(chain, chain)


def median_or_none(xs: list[float]) -> float | None:
    return statistics.median(xs) if xs else None


def vs_of(row: Row, solver: str) -> dict[str, Any]:
    return (row.get("solvers") or {}).get(solver) or {}


def n_solutions(vs: dict[str, Any]) -> int:
    return int(vs.get("n_solutions") or 0)


# ------------------------------------------------------------------ derived figures


def own_wins(rows: Iterable[Row], own_addresses: Iterable[str]) -> int:
    own = {a.lower() for a in own_addresses}
    return sum(1 for r in rows if any((t.get("submitter") or "").lower() in own for t in r.get("winner_txs") or []))


def settlement_span(rows: list[Row]) -> tuple[str, str, float | None]:
    """(start, end, hours) over the rows' settlement-block timestamps — the header's basis."""
    ts = [int(r["block_ts"]) for r in rows if r.get("block_ts") is not None]
    if not ts:
        return "n/a", "n/a", None
    return iso(min(ts)), iso(max(ts)), (max(ts) - min(ts)) / 3600 if len(ts) > 1 else 0.0


def replay_lag_minutes(rows: Iterable[Row]) -> float | None:
    return median_or_none([float(r["age_hours"]) * 60 for r in rows if r.get("age_hours") is not None])


def plausibility(rows: list[Row], solver: str) -> Plausibility:
    winners = [int(r.get("winner_surplus_wei") or 0) for r in rows]
    win_med = median_or_none([float(w) for w in winners if w > 0])
    flagged = [r for r in rows if "implausible_surplus" in (vs_of(r, solver).get("flags") or [])]
    by_type: dict[str, int] = {}
    for r in flagged:
        q = str(r.get("baseline_quality") or "mixed")
        by_type[q] = by_type.get(q, 0) + 1
    ratios = [
        int(vs_of(r, solver).get("best_surplus_wei") or 0) / int(r["winner_surplus_wei"])
        for r in flagged
        if int(r.get("winner_surplus_wei") or 0) > 0
    ]
    flagged_w = [int(r.get("winner_surplus_wei") or 0) for r in flagged]
    below = sum(1 for w in flagged_w if win_med is not None and w < win_med / 10)
    fmed = median_or_none([float(w) for w in flagged_w])
    return Plausibility(
        len(flagged),
        by_type,
        max(ratios) if ratios else None,
        below,
        int(fmed) if fmed is not None else None,
        int(win_med) if win_med is not None else None,
    )


def top_five(rows: list[Row], solver: str, exclude_ids: set[Any]) -> tuple[float | None, int]:
    """Share of the window's winner surplus in the five largest auctions (artefacts excluded)
    and how many of those we entered."""
    entries = [(int(r.get("winner_surplus_wei") or 0), vs_of(r, solver)) for r in rows if r.get("auction_id") not in exclude_ids]
    total = sum(w for w, _ in entries)
    top = sorted(entries, key=lambda e: -e[0])[:5]
    share = 100 * sum(w for w, _ in top) / total if total else None
    return share, sum(1 for _, vs in top if n_solutions(vs) > 0)


def figures(rep: dict[str, Any], rows: list[Row], solver: str, own_addresses: Iterable[str]) -> PublicFigures:
    """`rows` are the attempted rows of the window (one per auction)."""
    w = rep["window"]
    start, end, span_h = settlement_span(rows)
    return PublicFigures(
        lo=w["from_block"],
        hi=w["to_block"],
        attempted=rep["auctions_attempted"],
        replayed=rep["answered"],
        returned=rep["bids"],
        valid=rep["valid"],
        transport=rep["transport"],
        deadline_miss=rep["deadline_miss"],
        p50=rep["p50_ms"],
        p95=rep["p95_ms"],
        budget=rep["budget_s"],
        valid_pct=rep["valid_rate_pct"],
        fair_eval=rep["fairness_evaluated"],
        fair_filtered=rep["fairness_filtered"],
        capture=rep["capture_pct"],
        capture_x=rep["capture_ex_artefact_pct"],
        arte=[a["auction_id"] for a in rep["artefact_auctions"]],
        own_wins=own_wins(rows, own_addresses),
        perbid=rep["per_bid_median_ratio"],
        n_perbid=rep["per_bid_ratio_n"],
        flagged=rep["implausible"],
        udcp=rep["udcp_checked"],
        udcp_v=rep["udcp_violations"],
        verdict=rep["verdict"],
        warns=[c["label"] for c in rep["checks"] if c["level"] == "warn"],
        span=[start, end],
        span_h=span_h,
        rate=rep["auctions_per_hour"],
        found=rep["counts"]["settlements_found"],
        excluded=rep["excluded"]["n"],
        skip={k: v for k, v in rep["skipped"].items() if k not in NON_EXCLUSION_SKIPS},
        unscanned=rep["unscanned_blocks"],
        lag_med_min=replay_lag_minutes(rows),
    )


# ------------------------------------------------------------------ rendering


def render(rep: dict[str, Any], screen: str, fig: PublicFigures, rows: list[Row], opts: ReportOptions) -> str:
    solver = rep["solver"]
    plaus = plausibility(rows, solver)
    native = str(rep.get("native") or "ETH")
    parts = [
        _title(rep, rows),
        _intro(rep, opts),
        _header_bullets(rep, fig, rows, opts),
        "```\n" + screen + "\n```",
        _reading(rep, native),
        _plausibility_paragraph(plaus, native),
        _bottom_line(rep, fig, opts),
        _method_notes(fig, opts),
        "## Reproduce\n\n```\n    " + rep["reproduce"] + "\n```",
        _manifest_line(opts.manifest),
    ]
    return "\n\n".join(p for p in parts if p) + "\n"


def _title(rep: dict[str, Any], rows: list[Row]) -> str:
    start, end, _ = settlement_span(rows)
    a, b = start[:10], end[:10]
    if a == b or "n/a" in (a, b):
        when = a
    elif a[:7] == b[:7]:
        when = f"{a} → {b[8:]}"
    else:
        when = f"{a} → {b}"
    return f"# Readiness report: {rep['solver']} on {rep['chain']} — {when}"


def _intro(rep: dict[str, Any], opts: ReportOptions) -> str:
    return (
        f"The Readiness Standard, applied to ourselves, with cow-backtester **{opts.rows_tool_version}**. This run uses the "
        f"observed per-request budget for the chain and the standard's {rep['min_evidence']}-auction evidence floor: "
        f"`--readiness --watch {opts.watch_seconds}`, every settled auction in the window replayed against {opts.instance_note}."
    )


def _header_bullets(rep: dict[str, Any], fig: PublicFigures, rows: list[Row], opts: ReportOptions) -> str:
    artefacts = rep["artefact_auctions"]
    share, entered = top_five(rows, rep["solver"], {a["auction_id"] for a in artefacts})
    lines = [
        f"- **Window:** blocks {fig.lo}–{fig.hi} (fixed), {fig.span[0]} → {fig.span[1]}"
        + (f" ({fig.span_h:.1f} h of chain time)" if fig.span_h is not None else ""),
        f"- **Sample:** {fig.attempted} auctions attempted (floor {rep['min_evidence']}), {fig.replayed} answered, "
        f"{fig.returned} with ≥1 solution",
        f"- **Budget:** {fig.budget:g} s ({rep['budget_source']}{'; ' + opts.budget_note if opts.budget_note else ''})",
    ]
    if fig.lag_med_min is not None:
        lines.append(
            f"- **Replay lag:** median {fig.lag_med_min:.1f} min between an auction settling and our replay of it"
            " — live liquidity at replay time is close to auction-time liquidity"
        )
    lines.append(
        f"- **Fairness:** the CIP-67 filter was applied to our solutions on every bidding auction with a competition record "
        f"({fig.fair_eval} of {fig.returned}); {fig.fair_filtered} of our solutions were filtered, exactly as the protocol "
        "would have filtered them"
    )
    if artefacts:
        lines.append(
            f"- **Capture:** {pct(fig.capture)} as the tool computes it (screen below), **{pct(fig.capture_x)}** with the "
            f"{len(artefacts)} valuation artefact(s) described under *Reading the verdict* excluded — both coverage-adjusted"
        )
    else:
        lines.append(
            f"- **Capture:** {pct(fig.capture)} of the on-chain winners' surplus, coverage-adjusted "
            "(the screen below rounds to whole percent)"
        )
    lines.append(
        f"- **Own wins:** {fig.own_wins} of the {fig.attempted} attempted auctions were settled on-chain by "
        f"{rep['solver']}'s own account (counted per auction)"
    )
    if fig.perbid is not None and share is not None:
        excl = " (artefact excluded)" if artefacts else ""
        lines.append(
            f"- **Per bid:** on the {fig.n_perbid} unflagged auctions we entered, our surplus was {fig.perbid:.3f}× "
            "the winner's at the median. The capture figure is sum-weighted: the five largest auctions by winner "
            f"surplus hold {share:.0f} % of the window's winner surplus{excl}, and we entered {entered} of them"
        )
    lines.append(f"- **Verdict:** **{rep['verdict']}** — {verdict_tail(rep)}")
    return "\n".join(lines)


def verdict_tail(rep: dict[str, Any]) -> str:
    fails = [c["label"] for c in rep["checks"] if c["level"] == "fail"]
    warns = [c["label"] for c in rep["checks"] if c["level"] == "warn"]
    if fails:
        return f"{len(fails)} failing: {', '.join(fails)}" + (f"; {len(warns)} warn-level: {', '.join(warns)}" if warns else "")
    if warns:
        return f"no failing check; {len(warns)} warn-level: {', '.join(warns)}"
    return "every check passed"


def _reading(rep: dict[str, Any], native: str) -> str:
    lines = ["## Reading the verdict", ""]
    for c in rep["checks"]:
        if c["level"] != "ok":
            lines.append(f"- **{'FAIL' if c['level'] == 'fail' else 'WARN'} — {c['label']}:** {c['detail']}")
    for a in rep["artefact_auctions"]:
        ref = (
            f" against a competition reference score of {a['winner_reference_score']}"
            if a.get("winner_reference_score") is not None
            else ""
        )
        surplus = f"{a['winner_surplus_wei'] / 1e18:,.6f} {native}"
        lines.append(
            f"- **Valuation artefact — auction {a['auction_id']}:** decoded winner surplus {surplus} "
            f"({a['ratio']:,.0f}× the rest of the attempted window combined){ref}. The tool excludes it from "
            "the capture its verdict reads; both figures print on the screen."
        )
    return "\n".join(lines) if len(lines) > 2 else "## Reading the verdict\n\n- Every check passed."


def _plausibility_paragraph(p: Plausibility, native: str) -> str:
    if not p.n:
        return ""
    ratio = f"{p.largest_ratio:,.0f}×" if p.largest_ratio is not None else "n/a"
    fmed = native_amount(p.flagged_median_wei or 0)
    wmed = native_amount(p.window_median_wei or 0)
    return (
        '**On "prices look plausible":** the tool flags an auction when our *claimed* surplus exceeds ten times '
        f"what the on-chain winner actually delivered. {p.n} auctions are flagged here ({p.by_type} by "
        f"winner-baseline type; largest ratio {ratio}). Claimed prices are not simulated, so these rows are "
        f"treated as suspect rather than as evidence of out-competing the winner. {p.below_tenth} of the {p.n} "
        "had a winner surplus below a tenth of the window's median winner surplus (flagged-row median "
        f"{fmed} {native} vs window median {wmed} {native}). The capture figure above includes them; a reader "
        "who wants a conservative number can subtract them."
    )


def _bottom_line(rep: dict[str, Any], fig: PublicFigures, opts: ReportOptions) -> str:
    text = opts.bottom_line or (
        f"{rep['verdict']}: {verdict_tail(rep)}. Answered {rep['answer_rate_pct'] or 0:.0f} % of {fig.attempted} attempted "
        f"auctions, bid on {rep['bid_rate_pct'] or 0:.0f} % of them, captured {pct(fig.capture_x, 1)} of the winners' surplus "
        f"(coverage-adjusted), p95 latency {fig.p95} ms against a {fig.budget * 1000:.0f} ms budget."
    )
    return f"**Bottom line for {chain_title(rep['chain'])}:** {text}"


def _method_notes(fig: PublicFigures, opts: ReportOptions) -> str:
    lines = [
        "## Method notes",
        f"- Regenerated {opts.regenerated_at} over a fixed block window ({fig.lo}–{fig.hi}) from the watch run's "
        "rows: every settled auction whose settlement block lies inside it, one row per auction id "
        "(`tools/build_report.py`).",
        "- Counters are cumulative over the watch run (the tool's per-cycle screens are summed; each auction "
        "counted once). The verdict is the tool's own `readiness_report` over those counters.",
    ]
    if opts.launches > 1:
        lines.append(
            f"- The watch was (re)started {opts.launches} times; {opts.duplicates} auction(s) replayed twice keep "
            f"their first replay, and {opts.overlap_blocks} block(s) were scanned twice, so the field line's "
            "settlement and auction counts include both scans."
        )
    if opts.rows_tool_version != opts.tool_version:
        lines.append(
            f"- The rows were produced by cow-backtester {opts.rows_tool_version}; the verdict and screen come from "
            f"`readiness_report` of {opts.tool_version}, the version installed when the report was built."
        )
    dm = "none occurred" if not fig.deadline_miss else f"{fig.deadline_miss} occurred"
    tr = "none occurred" if not fig.transport else f"{fig.transport} occurred"
    lines.append(
        '- A "deadline miss" is a timeout OR an answer that arrived after the budget, mirroring the driver; '
        f'{dm}. "transport" is every other failure; {tr}.'
    )
    lines.append("- Capture is coverage-adjusted: auctions we failed to answer would keep the winner in the denominator.")
    if opts.manifest:
        lines.append(
            "- Bodies were archived (`--archive-bodies`, SHA-256 per body) so the run is reproducible after the "
            "public instance bucket evicts them; the archive is ours, hence `<archive>` in the reproduce line."
        )
    lines.append("- Signal, not guarantee: replays quote live liquidity against archived auctions.")
    return "\n".join(lines)


def _manifest_line(m: ManifestFacts | None) -> str:
    if m is None:
        return ""
    return (
        "Body archive manifest (root `manifest.jsonl` of the `--archive-bodies` directory, one line per archived "
        f"body with its SHA-256):\nSHA-256 `{m.sha256}` · {m.lines:,} lines · {m.mtime}."
    )
