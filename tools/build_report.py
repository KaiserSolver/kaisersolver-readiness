#!/usr/bin/env python3
"""Build the public readiness report from a run's --json-out rows (README step 1).

    tools/build_report.py --chain base --from-block 51300926 --to-block 51336483 \\
        --rows evidence/runs/base/2026-09-14.jsonl --rows evidence/runs/base/2026-09-14T143134Z.jsonl \\
        --compete --solver-url http://127.0.0.1:11090/prod/base --engine-sha e777ab394 \\
        --own-address 0x... --manifest evidence/archive/bodies-0.11/manifest.jsonl --out-dir build/base

Reads every row of the run (one per auction id, the first replay wins), keeps the block
window, rebuilds the counters `process_window` accrues and runs the tool's own
`readiness_report()` over them, then renders `report.md` in the published shape plus
`figures.json` (for `add_run.py --figures`), `meta.json` (for `add_run.py --meta`) and
`readiness.json` (the tool's full dict). Needs `cow_backtester` — the version that produced
the rows, unless `--allow-version-mismatch`.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import rebuild_state  # noqa: E402
import render_report  # noqa: E402
import rows as rowsmod  # noqa: E402

try:
    from cow_backtester import backtest as bt
except ImportError as e:  # the pure modules above stay importable without the tool
    raise SystemExit("build_report.py needs cow_backtester: pip install cow-backtester") from e

DEFAULT_SOLVER = "kaisersolver"
ENGINE_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")  # what record.py's _TOOL_RE accepts
GENERATOR_DESCRIPTION = (
    "tools/build_report.py over every --json-out file of the run, one row per auction id (first replay kept), "
    "restricted to the cycle window, re-running the tool's own readiness_report() over the cumulative counters"
)


@dataclass
class ReadinessArgs:
    """What `readiness_report(st, args)` reads off `args` (it uses getattr, so any object works)."""

    solvers: list[dict[str, str]]
    chain: str
    env: str
    budget_s: float
    budget_source: str
    min_evidence: int | None  # None = the tool's table value (500); a number is a CLI override
    max_auctions: int
    compete: bool
    archive_bodies: str | None
    bodies_dir: str | None = None
    engine_sha: str | None = None
    solve_timeout: float | None = None
    self_address: str | None = None
    quiet: bool = True


@dataclass(frozen=True)
class RowFacts:
    tool_version: str
    budget_s: float
    budget_source: str


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--chain", required=True, help="CoW chain slug as the rows name it (arbitrum-one, base, bnb, ...)")
    ap.add_argument("--env", default="prod")
    ap.add_argument("--from-block", type=int, required=True)
    ap.add_argument("--to-block", type=int, required=True)
    ap.add_argument(
        "--rows", action="append", required=True, type=Path, metavar="FILE", help="a --json-out file; repeat per launch"
    )
    ap.add_argument("--solver-name", default=DEFAULT_SOLVER)
    ap.add_argument("--solver-url", required=True, help="the --solver-url the run used (reproduce line)")
    ap.add_argument("--own-address", action="append", default=[], metavar="0x...", help="our settlement sender(s); repeat")
    ap.add_argument("--engine-sha", required=True, help="the engine's build sha, 7-40 hex chars")
    ap.add_argument("--compete", action="store_true", help="the run used --compete (reproduce line)")
    ap.add_argument("--manifest", type=Path, default=None, help="the --archive-bodies manifest.jsonl")
    ap.add_argument(
        "--min-evidence",
        type=int,
        default=None,
        help="override the tool's 500-auction floor (the published runs use the default)",
    )
    ap.add_argument("--watch-seconds", type=int, default=60, help="the --watch interval the run used (prose)")
    ap.add_argument("--instance-note", default=None, help="prose: what served the replay")
    ap.add_argument("--budget-note", default=None, help="prose appended to the Budget bullet")
    ap.add_argument("--bottom-line", default=None, help="the editorial paragraph; default is a neutral summary")
    ap.add_argument("--regenerated-at", default=None, help="ISO timestamp for the Method notes (default: now)")
    ap.add_argument("--record-root", type=Path, default=HERE.parent, help="the record repo (for the generator's commit)")
    ap.add_argument("--allow-version-mismatch", action="store_true")
    ap.add_argument("--out-dir", type=Path, required=True)
    return ap


# ------------------------------------------------------------------ validation


def validate_rows(rows: list[rowsmod.Row], cycles: rowsmod.CycleSelection, a: argparse.Namespace) -> RowFacts:
    versions = sorted(
        {str(r.get("v")) for r in rows if r.get("v")} | {str(c.tool_version) for c in cycles.kept if c.tool_version}
    )
    if len(versions) != 1:
        raise SystemExit(f"rows come from several tool versions {versions}; build one report per version")
    if versions[0] != bt.VERSION and not a.allow_version_mismatch:
        raise SystemExit(
            f"rows were produced by cow-backtester {versions[0]} but {bt.VERSION} is installed; "
            "install the matching version or pass --allow-version-mismatch"
        )
    chains = {str(r.get("chain")) for r in rows} | {str(c.chain) for c in cycles.kept if c.chain}
    if chains != {a.chain}:
        raise SystemExit(f"rows name chain(s) {sorted(chains)}, not --chain {a.chain}")
    budgets = {(float(r["budget_s"]), str(r.get("budget_source"))) for r in rows if r.get("budget_s") is not None}
    if len(budgets) != 1:
        raise SystemExit(f"rows carry {len(budgets)} distinct budgets {sorted(budgets)}; one report per budget")
    if not ENGINE_SHA_RE.match(a.engine_sha):
        raise SystemExit(f"--engine-sha {a.engine_sha!r} is not 7-40 hex chars; the record would drop it")
    ((budget_s, budget_source),) = budgets
    return RowFacts(versions[0], budget_s, budget_source)


def warn(msg: str) -> None:
    print(f"build_report: warning: {msg}", file=sys.stderr)


# ------------------------------------------------------------------ the tool


def run_readiness(st: dict[str, Any], args: ReadinessArgs) -> tuple[dict[str, Any], str]:
    """The tool's own readiness_report(); returns (its dict, the screen it printed)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        reps = bt.readiness_report(st, args)
    return reps[0], buf.getvalue().strip("\n")


def generator_block(record_root: Path) -> dict[str, Any]:
    me = Path(__file__).resolve()
    try:
        commit: str | None = subprocess.run(
            ["git", "-C", str(record_root), "rev-parse", "HEAD"], capture_output=True, text=True, check=True, timeout=10
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as e:
        warn(f"cannot read the record repo's commit at {record_root}: {e}")
        commit = None
    return {
        "description": GENERATOR_DESCRIPTION,
        "path": "tools/build_report.py",
        "sha256": hashlib.sha256(me.read_bytes()).hexdigest(),
        "commit": commit,
        "tool_version": bt.VERSION,
    }


# ------------------------------------------------------------------ assembly


@dataclass
class Inputs:
    read: rowsmod.JsonlRead
    window: rowsmod.Window
    windowed: rowsmod.WindowedRows
    cycles: rowsmod.CycleSelection
    facts: RowFacts


def load_inputs(a: argparse.Namespace) -> Inputs:
    read = rowsmod.read_jsonl(a.rows)
    for b in read.bad_lines:
        warn(f"{b.path}:{b.lineno}: {b.reason} (skipped)")
    for p in read.empty_files:
        warn(f"{p}: empty file (a watch that wrote nothing)")
    if not read.rows:
        raise SystemExit("no rows in the given files")
    window = rowsmod.Window(a.from_block, a.to_block)
    windowed = rowsmod.dedupe_in_window(read.rows, window)
    if windowed.unplaced:
        warn(f"{windowed.unplaced} row(s) carry no integer settlement_block and cannot be windowed")
    if not windowed.rows:
        raise SystemExit(f"no auction inside blocks {window.lo}..{window.hi}")
    cycles = rowsmod.select_cycles(read.metas, window)
    return Inputs(read, window, windowed, cycles, validate_rows(windowed.rows, cycles, a))


def readiness_args(a: argparse.Namespace, facts: RowFacts, max_auctions: int) -> ReadinessArgs:
    return ReadinessArgs(
        solvers=[{"name": a.solver_name, "url": a.solver_url}],
        chain=a.chain,
        env=a.env,
        budget_s=facts.budget_s,
        budget_source=facts.budget_source,
        min_evidence=a.min_evidence,
        max_auctions=max_auctions,
        compete=a.compete,
        archive_bodies="<archive>" if a.manifest else None,
        engine_sha=a.engine_sha,
    )


def report_options(a: argparse.Namespace, inp: Inputs) -> render_report.ReportOptions:
    return render_report.ReportOptions(
        tool_version=bt.VERSION,
        rows_tool_version=inp.facts.tool_version,
        watch_seconds=a.watch_seconds,
        instance_note=a.instance_note or f"the production {render_report.chain_title(a.chain)} instance",
        budget_note=a.budget_note,
        bottom_line=a.bottom_line,
        regenerated_at=a.regenerated_at or render_report.iso(datetime.now(timezone.utc).timestamp()),
        manifest=render_report.ManifestFacts.read(a.manifest) if a.manifest else None,
        duplicates=inp.windowed.duplicates,
        overlap_blocks=inp.cycles.overlap_blocks,
        launches=len(inp.read.files) - len(inp.read.empty_files),
    )


def build_meta(a: argparse.Namespace, inp: Inputs, opts: render_report.ReportOptions) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "generator": generator_block(a.record_root),
        "rows_tool_version": inp.facts.tool_version,
        "build": {
            "files": [Path(f).name for f in inp.read.files],
            "launches": opts.launches,
            "rows_read": len(inp.read.rows),
            "rows_in_window": len(inp.windowed.rows),
            "duplicates": inp.windowed.duplicates,
            "unplaced": inp.windowed.unplaced,
            "cycles": len(inp.cycles.kept),
            "cycles_dropped_contained": inp.cycles.dropped_contained,
            "overlap_blocks": inp.cycles.overlap_blocks,
            "bad_lines": [asdict(b) for b in inp.read.bad_lines],
            "empty_files": [Path(p).name for p in inp.read.empty_files],
        },
    }
    if opts.manifest and a.manifest:
        meta["body_archive_manifest"] = {"sha256": opts.manifest.sha256, "lines": opts.manifest.lines, "path": str(a.manifest)}
    return meta


def write_outputs(out: Path, report: str, fig: render_report.PublicFigures, rep: dict[str, Any], meta: dict[str, Any]) -> None:
    try:
        out.mkdir(parents=True, exist_ok=True)
        (out / "report.md").write_text(report, encoding="utf-8")
        (out / "figures.json").write_text(json.dumps(fig.to_dict(), indent=1) + "\n", encoding="utf-8")
        (out / "readiness.json").write_text(json.dumps(rep, indent=1, default=str) + "\n", encoding="utf-8")
        (out / "meta.json").write_text(json.dumps(meta, indent=1) + "\n", encoding="utf-8")
    except OSError as e:
        raise SystemExit(f"cannot write outputs under {out}: {e}") from e


def build(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    if a.from_block > a.to_block:
        raise SystemExit("--from-block must be <= --to-block")
    inp = load_inputs(a)
    if a.compete and not any("auction_start_block" in r for r in inp.windowed.rows):
        warn("--compete given but no row carries a competition record; the reproduce line will still say --compete")
    max_auctions = max((c.max_auctions for c in inp.cycles.kept), default=0)
    native = str(bt.CHAINS.get(a.chain, {}).get("native", "ETH"))
    state = rebuild_state.build_state(inp.windowed.rows, inp.cycles, inp.window, a.solver_name, native)
    if state.stat.attempted == 0:
        raise SystemExit(f"no row carries a result for solver {a.solver_name!r}")
    rep, screen = run_readiness(state.to_dict(), readiness_args(a, inp.facts, max_auctions))
    rep = dict(rep, native=native)
    ours = rebuild_state.attempted_rows(inp.windowed.rows, a.solver_name)
    fig = render_report.figures(rep, ours, a.solver_name, a.own_address)
    opts = report_options(a, inp)
    write_outputs(a.out_dir, render_report.render(rep, screen, fig, ours, opts), fig, rep, build_meta(a, inp, opts))
    print(
        json.dumps(
            {
                "verdict": rep["verdict"],
                "warns": fig.warns,
                "attempted": fig.attempted,
                "duplicates": inp.windowed.duplicates,
                "overlap_blocks": inp.cycles.overlap_blocks,
                "out_dir": str(a.out_dir),
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(build())
