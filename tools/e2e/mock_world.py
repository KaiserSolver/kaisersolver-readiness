#!/usr/bin/env python3
"""A stand-in for the outside world the tool talks to, on one local HTTP port.

Serves, for chain `arbitrum-one`:
  POST /                                     JSON-RPC: eth_chainId, eth_blockNumber, eth_getLogs,
                                             eth_getTransactionByHash, eth_getTransactionReceipt, eth_getBlockByNumber
  GET  /prod/arbitrum-one/auction/<id>.json  the S3 instance bucket (auction bodies)
  GET  /arbitrum_one/api/v2/solver_competition/<id>   the CoW API competition record

The chain advances in REAL time: 4 blocks per second from the moment the server starts (minus a
head start), and a settlement of the pinned fixture auction 8339027 — cloned to a new auction id
by rewriting the last 8 bytes of its calldata — lands every `--step` blocks. Timestamps are wall
clock, so a watch replaying "now" sees the same ages it would on a live chain. Nothing here is the
tool's code; the tool runs unmodified against this over HTTP (see the cow-backtester shim).

    python3 tools/e2e/mock_world.py --port 18545 [--step 20] [--head-start 120]
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

SRC = Path(os.environ.get("COW_BACKTESTER_SRC", Path.home() / "cow-backtester")).expanduser()
FIX = SRC / "fixtures"
CHAIN_ID = 42161
SETTLEMENT = "0x9008d19f58aabd9ed0d60971565aa8510560ab41"
KAISER = "0x00000000000000000000000000000000000d0c0a"
RIVAL = "0x0000000000000000000000000000000000000abc"
FIRST_AID = 8339027
BLOCK0 = 491743388
BLOCKS_PER_SECOND = 4


def load(name: str) -> dict[str, Any]:
    try:
        return json.loads((FIX / name).read_text())
    except OSError as e:
        raise SystemExit(f"need the cow-backtester checkout fixtures at {FIX}: {e}") from e


class Chain:
    """Blocks, timestamps and the settlements that exist so far, all from the wall clock."""

    def __init__(self, step: int, head_start_s: int) -> None:
        self.settlement = load("settlement_8339027.json")
        self.body = load("body_8339027_trimmed.json")
        self.step = step
        self.t0 = time.time() - head_start_s  # BLOCK0 was mined at t0

    def head(self) -> int:
        return BLOCK0 + int((time.time() - self.t0) * BLOCKS_PER_SECOND)

    def ts_of(self, block: int) -> int:
        return int(self.t0 + (block - BLOCK0) / BLOCKS_PER_SECOND)

    def block_of(self, i: int) -> int:
        return BLOCK0 + self.step * i

    def aid_of(self, i: int) -> int:
        return FIRST_AID + 7 * i

    def index_of_aid(self, aid: int) -> int | None:
        i, r = divmod(aid - FIRST_AID, 7)
        return i if r == 0 and i >= 0 and self.block_of(i) <= self.head() else None

    def tx_of(self, i: int) -> str:
        return "0x" + hashlib.sha256(f"e2e-tx-{self.aid_of(i)}".encode()).hexdigest()

    def indices_in(self, lo: int, hi: int) -> list[int]:
        lo_i = max(0, -(-(lo - BLOCK0) // self.step))
        hi_i = (min(hi, self.head()) - BLOCK0) // self.step
        return list(range(lo_i, hi_i + 1)) if hi_i >= lo_i else []

    def index_of_tx(self, tx: str) -> int | None:
        return (
            next((i for i in range(self.indices_in(BLOCK0, self.head())[-1] + 1) if self.tx_of(i) == tx), None)
            if self.head() >= BLOCK0
            else None
        )

    def calldata(self, i: int) -> str:
        return self.settlement["calldata"][:-16] + f"{self.aid_of(i):016x}"

    def log_entries(self, i: int) -> list[dict[str, Any]]:
        out = []
        for n, lg in enumerate(self.settlement["logs"]):
            e = dict(lg)
            e.update(
                {
                    "transactionHash": self.tx_of(i),
                    "blockNumber": hex(self.block_of(i)),
                    "logIndex": hex(n),
                    "transactionIndex": "0x0",
                    "removed": False,
                }
            )
            out.append(e)
        return out

    def tx(self, i: int) -> dict[str, Any]:
        return {
            "hash": self.tx_of(i),
            "input": self.calldata(i),
            "from": KAISER if i % 3 == 0 else RIVAL,
            "to": SETTLEMENT,
            "blockNumber": hex(self.block_of(i)),
        }

    def receipt(self, i: int) -> dict[str, Any]:
        return {
            "transactionHash": self.tx_of(i),
            "status": self.settlement["status"],
            "blockNumber": hex(self.block_of(i)),
            "gasUsed": hex(self.settlement["gas_used"]),
            "effectiveGasPrice": hex(self.settlement["eff_gas_price"]),
            "logs": self.log_entries(i),
        }

    def auction_body(self, i: int) -> dict[str, Any]:
        b = copy.deepcopy(self.body)
        b["id"] = str(self.aid_of(i))
        start = self.ts_of(self.block_of(i) - 8)
        b["deadline"] = datetime.fromtimestamp(start + 6, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + ".500000000Z"
        scale = 1 + (i % 4)
        for t in b["tokens"].values():
            if t.get("referencePrice") is not None:
                t["referencePrice"] = str(int(t["referencePrice"]) * scale)
        return b

    def competition(self, i: int) -> dict[str, Any]:
        o0 = self.body["orders"][0]
        return {
            "auctionId": self.aid_of(i),
            "auctionStartBlock": self.block_of(i) - 8,
            "auctionDeadlineBlock": self.block_of(i) + 10,
            "solutions": [
                {
                    "solverAddress": RIVAL,
                    "score": "1",
                    "isWinner": True,
                    "filteredOut": False,
                    "referenceScore": "0",
                    "orders": [{"id": o0["uid"], "sellAmount": o0["sellAmount"], "buyAmount": o0["buyAmount"]}],
                }
            ],
        }


class Handler(BaseHTTPRequestHandler):
    chain: Chain
    log_file: Any = None
    receipt_delay_s: float = 0.0

    def log_message(self, fmt: str, *args: Any) -> None:  # quiet; the orchestrator prints its own summary
        if self.log_file:
            self.log_file.write((fmt % args) + "\n")
            self.log_file.flush()

    def _json(self, status: int, obj: Any) -> None:
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        c = self.chain
        parts = self.path.strip("/").split("/")
        if len(parts) == 4 and parts[:3] == ["prod", "arbitrum-one", "auction"] and parts[3].endswith(".json"):
            i = c.index_of_aid(int(parts[3][:-5]))
            return self._json(200, c.auction_body(i)) if i is not None else self._json(404, {"error": "no such auction"})
        if len(parts) == 5 and parts[:4] == ["arbitrum_one", "api", "v2", "solver_competition"]:
            i = c.index_of_aid(int(parts[4]))
            return self._json(200, c.competition(i)) if i is not None else self._json(404, {"error": "no record"})
        self._json(404, {"error": f"unknown path {self.path}"})

    def do_POST(self) -> None:  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        try:
            req = json.loads(self.rfile.read(n))
        except json.JSONDecodeError:
            return self._json(400, {"error": "bad json"})
        result = self.rpc(req.get("method"), req.get("params") or [])
        self._json(200, {"jsonrpc": "2.0", "id": req.get("id"), "result": result})

    def rpc(self, method: str, params: list[Any]) -> Any:
        c = self.chain
        if method == "eth_chainId":
            return hex(CHAIN_ID)
        if method == "eth_blockNumber":
            return hex(c.head())
        if method == "eth_getLogs":
            f = params[0]
            lo, hi = int(f["fromBlock"], 16), int(f["toBlock"], 16)
            return [e for i in c.indices_in(lo, hi) for e in c.log_entries(i)]
        if method in ("eth_getTransactionByHash", "eth_getTransactionReceipt"):
            i = c.index_of_tx(params[0])
            if i is None:
                return None
            if method == "eth_getTransactionReceipt" and self.receipt_delay_s:
                time.sleep(self.receipt_delay_s)  # makes a cycle long enough for a stop to land inside it
            return c.tx(i) if method == "eth_getTransactionByHash" else c.receipt(i)
        if method == "eth_getBlockByNumber":
            block = c.head() if params[0] == "latest" else int(params[0], 16)
            return {"number": hex(block), "timestamp": hex(c.ts_of(block))}
        return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--step", type=int, default=20, help="blocks between settlements (20 = one every 5 s)")
    ap.add_argument("--head-start", type=int, default=120, help="seconds of chain history that exist at start")
    ap.add_argument("--log", type=Path, default=None, help="request log file")
    ap.add_argument("--receipt-delay-ms", type=int, default=0, help="latency of every receipt fetch (long cycles)")
    a = ap.parse_args(argv)
    Handler.receipt_delay_s = a.receipt_delay_ms / 1000
    Handler.chain = Chain(a.step, a.head_start)
    Handler.log_file = open(a.log, "a") if a.log else None
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    print(json.dumps({"mock_world": f"http://127.0.0.1:{a.port}", "head": Handler.chain.head(), "step": a.step}), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
