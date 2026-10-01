#!/usr/bin/env python3
"""Generate tools/fixtures/monthly/ — SYNTHETIC cow-backtester --json-out rows, offline.

Hand-run, not a test. It drives the tool's REAL `main()` in-process and replaces only
the functions that reach the network (RPC, S3, the solver, the competition API), so
every row and `_meta` line is written by the tool's own code over stand-in inputs: it is not a recorded run,
the transaction hashes are invented and the submitter addresses are dummies. The pinned Arbitrum
fixture (auction 8339027) is cloned into many auctions by rewriting the auction id in
the settlement calldata (its last 8 bytes) and giving each clone its own block.

    COW_BACKTESTER_SRC=~/cow-backtester python3 tools/make_fixture.py --out tools/fixtures/monthly

Needs the tool CHECKOUT (for fixtures/ and mock_solver.py, which the wheel does not
ship) and `cow_backtester` importable from it. Run it from the repo root: the reproduce
line inside the expected files carries the archive path as given. Output, laid out as an
evidence root (what `add_run.py --evidence-root` and `verify.py --evidence-root` expect):

  runs/arbitrum-one/fixture-a.jsonl   a watch run, 3 cycles
  runs/arbitrum-one/fixture-b.jsonl   a restart: 2 cycles whose first re-scans part of
                                               the previous file (duplicate auction ids), then
                                               rows of a killed run with no closing _meta
  runs/arbitrum-one/empty.jsonl                a watch that died before its first row
  archive/bodies-<major.minor>/manifest.jsonl  the --archive-bodies manifest (bodies removed)
  expected_file1.json        the tool's own state + readiness over file 1's window
  expected_full.json         the same over both files' window (one-shot run)
  fixture.json               the parameters behind it all
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import shutil
import sys
import tempfile
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SRC = Path(os.environ.get("COW_BACKTESTER_SRC", Path.home() / "cow-backtester")).expanduser()
sys.path.insert(0, str(SRC))
try:
    import mock_solver  # noqa: E402  (checkout-only module)
    from cow_backtester import backtest, competition, scorer  # noqa: E402
except ImportError as e:
    raise SystemExit(f"need the cow-backtester checkout at {SRC} (COW_BACKTESTER_SRC): {e}") from e

ORIGINAL_READINESS_REPORT = backtest.readiness_report  # wrapped per run; never wrap a wrapper

CHAIN = "arbitrum-one"
SOLVER = "kaisersolver"
SOLVER_URL = "http://solver.invalid/prod/arbitrum-one"
KAISER = "0x00000000000000000000000000000000000d0c0a"  # dummy address, not a real account
RIVAL = "0x0000000000000000000000000000000000000abc"
FIRST_AID = 8339027
BLOCK0 = 491743388
BLOCK_STEP = 6000  # ~25 min of Arbitrum blocks between auctions → ~18 h over 43 auctions
T0 = 1_772_409_600  # 2026-03-02T00:00:00Z, deliberately not a month the record covers
BUDGET_S = 4.84
# per-auction behaviour of "our" solver, by index; everything else answers uniformly
MODES: dict[int, str] = {
    3: "empty",
    9: "empty",
    16: "empty",
    27: "empty",
    35: "empty",
    7: "filtered",
    11: "invalid",
    14: "garbage",
    21: "late",
    24: "timeout",
    29: "http_500",
    32: "bad_response",
    5: "artefact",
    38: "garbage",
}
CYCLES_FILE1 = [(0, 12), (13, 25), (26, 33)]  # auction index ranges per watch cycle
CYCLES_FILE2 = [(30, 36), (37, 39)]  # restart: re-scans 30..33
TAIL = (40, 42)  # killed run: rows, no _meta
N_AUCTIONS = 43


@dataclass
class World:
    """The fake chain: which auction sits at which block, and how each behaves."""

    aids: list[int] = field(default_factory=lambda: [FIRST_AID + 7 * i for i in range(N_AUCTIONS)])
    head: int = 0
    now: float = float(T0)

    def block_of(self, i: int) -> int:
        return BLOCK0 + BLOCK_STEP * i

    def index_of_block(self, block: int) -> int:
        return (block - BLOCK0) // BLOCK_STEP

    def ts_of(self, block: int) -> int:
        return T0 + (block - BLOCK0) // 4

    def tx_of(self, i: int) -> str:
        return "0x" + hashlib.sha256(f"tx-{self.aids[i]}".encode()).hexdigest()

    def mode(self, i: int) -> str:
        return MODES.get(i, "uniform")


def load_fixture(name: str) -> dict[str, Any]:
    path = SRC / "fixtures" / name
    try:
        return json.loads(path.read_text())
    except OSError as e:
        raise SystemExit(f"cannot read tool fixture {path}: {e}") from e


SETTLEMENT = load_fixture("settlement_8339027.json")
BODY = load_fixture("body_8339027_trimmed.json")


def uniform_solution() -> dict[str, Any]:
    """The honest replica of the fixture's winner (copied from the tool's tests/test_v0_11.py)."""
    dec = scorer.decode_settlement(SETTLEMENT["calldata"])
    ev = scorer.trade_events(SETTLEMENT["logs"])
    toks, tr = dec["tokens"], dec["trades"][0]
    st, bt = toks[tr[0]], toks[tr[1]]
    u_si, u_bi = scorer.uniform_price_indices(toks, st, bt)
    pr = dec["clearing_prices"]
    return {
        "id": 0,
        "prices": {st: str(pr[u_si]), bt: str(pr[u_bi])},
        "trades": [{"kind": "fulfillment", "order": ev[0]["uid"], "executedAmount": str(tr[9]), "fee": "0"}],
        "interactions": [],
        "gas": 150000,
    }


def clone_settlement(w: World, i: int) -> dict[str, Any]:
    s = copy.deepcopy(SETTLEMENT)
    s["calldata"] = s["calldata"][:-16] + f"{w.aids[i]:016x}"
    s["block"] = w.block_of(i)
    s["from"] = KAISER if i % 3 == 0 else RIVAL
    return s


def clone_body(w: World, i: int) -> dict[str, Any]:
    b = copy.deepcopy(BODY)
    b["id"] = str(w.aids[i])
    start_ts = w.ts_of(w.block_of(i) - 8)
    b["deadline"] = datetime.fromtimestamp(start_ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + ".500000000Z"
    scale = 10_000 if w.mode(i) == "artefact" else 1 + (i % 4)
    for t in b["tokens"].values():
        if t.get("referencePrice") is not None:
            t["referencePrice"] = str(int(t["referencePrice"]) * scale)
    return b


def competition_record(w: World, i: int) -> dict[str, Any]:
    o0 = BODY["orders"][0]
    score = "1000000000000000000000" if w.mode(i) == "filtered" else "1"  # a huge baseline filters us
    return {
        "auctionId": w.aids[i],
        "auctionStartBlock": w.block_of(i) - 8,
        "auctionDeadlineBlock": w.block_of(i) + 10,
        "solutions": [
            {
                "solverAddress": RIVAL,
                "score": score,
                "isWinner": True,
                "filteredOut": False,
                "referenceScore": "0",
                "orders": [{"id": o0["uid"], "sellAmount": o0["sellAmount"], "buyAmount": o0["buyAmount"]}],
            }
        ],
    }


def answer(w: World, i: int, body: dict[str, Any]) -> tuple[Any, str | None, int]:
    """(resp, err, latency_ms) for one auction — the shape dispatch_solvers returns per solver."""
    mode, budget_ms = w.mode(i), int(BUDGET_S * 1000)
    ms = 400 + 37 * (i % 11)
    if mode == "timeout":
        return None, "timeout", budget_ms + 5000
    if mode == "http_500":
        return None, "http_500", 250
    if mode == "bad_response":
        return {"solutions": "not-a-list"}, None, ms
    if mode == "empty":
        return {"solutions": []}, None, ms
    if mode in ("invalid", "garbage"):
        mock_solver.MODE = mode
        return {"solutions": mock_solver.build_solution(body)}, None, ms
    if mode == "late":
        return {"solutions": [uniform_solution()]}, None, budget_ms + 600
    return {"solutions": [uniform_solution()]}, None, ms


def patch_world(w: World) -> None:
    """Replace every network leaf of the tool with the fake chain. Process-wide, on purpose."""

    def enumerate_settlements(rpcs: Any, frm: int, to: int, step: int = 800) -> tuple[list[str], list[Any]]:
        return [w.tx_of(i) for i in range(N_AUCTIONS) if frm <= w.block_of(i) <= to], []

    def fetch_settlement(rpcs: Any, tx: str, cache: Any, mcb: Any) -> dict[str, Any]:
        return clone_settlement(w, next(i for i in range(N_AUCTIONS) if w.tx_of(i) == tx))

    def s3(env: str, chain: str, aid: int, cache: Any = None, errors: Any = None) -> dict[str, Any]:
        return clone_body(w, w.aids.index(int(aid)))

    def dispatch(solvers: list[dict[str, str]], payload: bytes, timeout: float) -> list[tuple[Any, ...]]:
        body = json.loads(payload)
        i = w.aids.index(int(body["id"]))
        resp, err, ms = answer(w, i, body)
        return [(sv, resp, err, ms) for sv in solvers]

    def rpc(rpcs: Any, method: str, params: Any, tries: int = 3) -> str:
        return {"eth_chainId": hex(42161), "eth_blockNumber": hex(w.head)}[method]

    backtest.enumerate_settlements = enumerate_settlements
    backtest.fetch_settlement_cached = fetch_settlement
    backtest.block_ts = lambda rpcs, block, mem, cache, mcb: w.ts_of(block)
    backtest.s3_auction = s3
    backtest.dispatch_solvers = dispatch
    backtest.preflight = lambda url, timeout=10: (True, "ok")
    backtest.time.time = lambda: w.now
    scorer.rpc = rpc
    competition.fetch_competition = lambda api_base, aid, http_get, cache=None: competition_record(w, w.aids.index(int(aid)))


@dataclass
class Capture:
    """What the tool's readiness_report saw and returned, per cycle."""

    states: list[dict[str, Any]] = field(default_factory=list)
    reports: list[dict[str, Any]] = field(default_factory=list)


def capture_readiness(cap: Capture) -> Callable[..., Any]:
    def wrapped(st: dict[str, Any], args: Any) -> Any:
        cap.states.append(serializable_state(st))
        reps = ORIGINAL_READINESS_REPORT(st, args)
        cap.reports.append(reps[0])
        return reps

    return wrapped


def serializable_state(st: dict[str, Any]) -> dict[str, Any]:
    """The parts of `st` readiness reads, JSON-safe (Counters → dicts, latency sorted)."""
    per_solver = {}
    for name, s in st["per_solver"].items():
        out = {
            k: (dict(v) if isinstance(v, Counter) else sorted(v) if k == "latency" else v)
            for k, v in s.items()
            if k not in ("rank_rows", "econ_rows")
        }
        per_solver[name] = out
    return {
        "per_solver": per_solver,
        "from_block": st["from_block"],
        "to_block": st["to_block"],
        "settlements_found": len(st["txs"]),
        "auctions_formed": st["n_found"],
        "skip": dict(st["skip"]),
        "failed_ranges": [list(r) for r in st["failed_ranges"]],
        "rows": len(st["rows"]),
        "attempted_ts": sorted(st.get("attempted_ts") or []),
        "attempted_start_ts": sorted(st.get("attempted_start_ts") or []),
        "budget_upper": sorted(st.get("budget_upper") or []),
    }


def base_argv(out: Path, archive: Path) -> list[str]:
    return [
        "--chain",
        CHAIN,
        "--env",
        "prod",
        "--rpc-url",
        "http://stub",
        "--no-cache",
        "--quiet",
        "--readiness",
        "--compete",
        "--min-evidence",
        "500",
        "--solver-url",
        SOLVER_URL,
        "--solver-name",
        SOLVER,
        "--json-out",
        str(out),
        "--archive-bodies",
        str(archive),
    ]


def run_watch(w: World, cycles: list[tuple[int, int]], out: Path, archive: Path) -> Capture:
    """One watch launch: cycle 0 scans back to the first auction of `cycles[0]`, later cycles
    scan `last_to + 1 .. head`; the fake sleep advances the head and stops after the last cycle."""
    cap, pending = Capture(), list(cycles)
    lo0, hi0 = pending.pop(0)
    w.head = w.block_of(hi0) + 100
    w.now = w.ts_of(w.head) + 45
    blocks = w.head - (w.block_of(lo0) - 100) + 1

    def sleep(_s: float) -> None:
        if not pending:
            raise KeyboardInterrupt
        _, hi = pending.pop(0)
        w.head = w.block_of(hi) + 100
        w.now = w.ts_of(w.head) + 45

    backtest.time.sleep = sleep
    backtest.readiness_report = capture_readiness(cap)
    try:
        backtest.main(base_argv(out, archive) + ["--watch", "1", "--blocks", str(blocks)])
    except SystemExit as e:
        if e.code not in (None, 0):
            raise SystemExit(f"tool exited {e.code} during watch {cycles}") from e
    return cap


def run_oneshot(w: World, lo_idx: int, hi_block: int, archive: Path) -> Capture:
    """The reference: the tool over the whole window in one pass."""
    cap = Capture()
    w.head = hi_block
    w.now = w.ts_of(hi_block) + 45
    backtest.readiness_report = capture_readiness(cap)
    with tempfile.TemporaryDirectory() as tmp:
        argv = base_argv(Path(tmp) / "ref.jsonl", archive)
        argv += ["--from-block", str(w.block_of(lo_idx) - 100), "--to-block", str(hi_block)]
        try:
            backtest.main(argv)
        except SystemExit as e:
            if e.code not in (None, 0):
                raise SystemExit(f"tool exited {e.code} during one-shot reference") from e
    return cap


def killed_tail(w: World, out: Path, archive: Path) -> int:
    """Rows of a run killed before its _meta: run a real cycle into a temp file, append its rows only."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_out = Path(tmp) / "tail.jsonl"
        run_watch(w, [TAIL], tmp_out, archive)
        rows = [ln for ln in tmp_out.read_text().splitlines() if ln and '"_meta"' not in ln]
    with out.open("a") as f:
        f.write("\n".join(rows) + "\n")
    return len(rows)


def last_meta(path: Path) -> dict[str, Any]:
    metas = [json.loads(ln)["_meta"] for ln in path.read_text().splitlines() if '"_meta"' in ln]
    return metas[-1]


def write_json(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, indent=1, sort_keys=True, default=str) + "\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", required=True, help="fixture directory (created; existing files overwritten)")
    a = ap.parse_args(argv)
    out = Path(a.out)
    runs_dir = out / "runs" / CHAIN
    archive = out / "archive" / f"bodies-{'.'.join(backtest.VERSION.split('.')[:2])}"
    for d in (runs_dir, archive):
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True)
    w = World()
    patch_world(w)
    file1, file2 = runs_dir / "fixture-a.jsonl", runs_dir / "fixture-b.jsonl"
    cap1 = run_watch(w, CYCLES_FILE1, file1, archive)
    cap2 = run_watch(w, CYCLES_FILE2, file2, archive)
    tail_rows = killed_tail(w, file2, archive)
    (runs_dir / "empty.jsonl").write_bytes(b"")
    hi1, hi_full = last_meta(file1)["to_block"], last_meta(file2)["to_block"]
    ref1 = run_oneshot(w, CYCLES_FILE1[0][0], hi1, archive)
    ref_full = run_oneshot(w, CYCLES_FILE1[0][0], hi_full, archive)
    shutil.rmtree(archive / CHAIN, ignore_errors=True)  # keep the manifest, not the gzipped bodies
    write_json(out / "expected_file1.json", {"state": ref1.states[0], "readiness": ref1.reports[0]})
    write_json(out / "expected_full.json", {"state": ref_full.states[0], "readiness": ref_full.reports[0]})
    write_json(
        out / "fixture.json",
        {
            "tool_version": backtest.VERSION,
            "chain": CHAIN,
            "solver": SOLVER,
            "solver_url": SOLVER_URL,
            "own_addresses": [KAISER],
            "budget_s": BUDGET_S,
            "aids": w.aids,
            "blocks": [w.block_of(i) for i in range(N_AUCTIONS)],
            "modes": {str(i): w.mode(i) for i in range(N_AUCTIONS)},
            "cycles_file1": CYCLES_FILE1,
            "cycles_file2": CYCLES_FILE2,
            "killed_tail": TAIL,
            "killed_tail_rows": tail_rows,
            "window_file1": [ref1.states[0]["from_block"], hi1],
            "window_full": [ref_full.states[0]["from_block"], hi_full],
            "per_cycle_states": {"file1": cap1.states, "file2": cap2.states},
            "world": asdict(w) | {"note": "head/now are the values after the last run"},
        },
    )
    print(
        json.dumps(
            {
                "file1_cycles": len(cap1.reports),
                "file2_cycles": len(cap2.reports),
                "tail_rows": tail_rows,
                "verdict_file1": ref1.reports[0]["verdict"],
                "verdict_full": ref_full.reports[0]["verdict"],
                "artefacts_full": [x["auction_id"] for x in ref_full.reports[0]["artefact_auctions"]],
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
