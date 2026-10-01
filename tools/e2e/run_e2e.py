#!/usr/bin/env python3
"""End-to-end test of the monthly run: the REAL run_month.py, the REAL cow-backtester CLI doing a
real watch over HTTP, the real add_run/verify, a real git push — against a stand-in world.

What is real: every line of tools/ (run through `RealShell`), the tool's network client, log
enumeration, body fetch, /solve wire path, scoring and readiness screen, git.
What is stood in for, and how:
  - the chain, the S3 bucket and the CoW API → tools/e2e/mock_world.py, one local HTTP server that
    advances 4 blocks/s in wall-clock time and settles a cloned auction every few blocks
  - the solver engine → the tool's own mock_solver.py (checkout only), a real HTTP /solve server
  - the cow-backtester binary → tools/e2e/shims/cow-backtester, the real CLI with its two host
    constants rebound to the mock world (no other change)
  - GitHub → tools/e2e/shims/gh, which logs and answers like an authenticated gh
  - the record's remote → a bare git repository on disk; the push is real

    COW_BACKTESTER_SRC=~/cow-backtester <python-with-cow_backtester> tools/e2e/run_e2e.py --duration 150s

Exit 0 only when the pushed branch, cloned back from the bare remote, verifies against the evidence
on disk and every assertion below holds. Needs the tool checkout (fixtures/ + mock_solver.py).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
TOOLS = HERE.parent
RECORD = TOOLS.parent
SRC = Path(os.environ.get("COW_BACKTESTER_SRC", Path.home() / "cow-backtester")).expanduser()
KAISER = "0xdd5aecdd8ba8498706e2583f6e2ff90e08e1c01b"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def sh(
    cmd: list[str], cwd: Path | None = None, env: dict[str, str] | None = None, timeout: int = 120
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout, check=False)


def must(p: subprocess.CompletedProcess[str], what: str) -> subprocess.CompletedProcess[str]:
    if p.returncode != 0:
        raise SystemExit(f"{what} failed ({p.returncode}):\n{p.stdout}\n{p.stderr}")
    return p


def wait_http(url: str, post: bytes | None, seconds: int = 20) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            req = urllib.request.Request(url, data=post, headers={"content-type": "application/json"})
            with urllib.request.urlopen(req, timeout=2):
                return
        except OSError:
            time.sleep(0.2)
    raise SystemExit(f"{url} did not come up in {seconds}s")


class World:
    """The processes and directories one e2e run needs."""

    def __init__(self, root: Path, python: str) -> None:
        self.root, self.python = root, python
        self.procs: list[subprocess.Popen[bytes]] = []
        self.mock_port, self.solver_port = free_port(), free_port()
        self.mock_url = f"http://127.0.0.1:{self.mock_port}"
        self.solver_url = f"http://127.0.0.1:{self.solver_port}/prod/arbitrum-one"
        self.bin = root / "bin"
        self.evidence = root / "evidence"
        self.remote = root / "remote.git"
        self.work = root / "work"
        self.logs = root / "logs"

    def start(self, step: int, head_start: int) -> None:
        for d in (self.bin, self.evidence, self.logs):
            d.mkdir(parents=True, exist_ok=True)
        for shim in ("cow-backtester", "gh"):
            dst = self.bin / shim
            shutil.copyfile(HERE / "shims" / shim, dst)
            dst.chmod(0o755)
        (self.bin / "cow-backtester").write_text(
            (self.bin / "cow-backtester").read_text().replace("#!/usr/bin/env python3", f"#!{self.python}", 1)
        )
        world_env = {**os.environ, "COW_BACKTESTER_SRC": str(SRC)}
        self.procs.append(subprocess.Popen(
            [self.python, str(HERE / "mock_world.py"), "--port", str(self.mock_port), "--step", str(step),
             "--head-start", str(head_start), "--log", str(self.logs / "mock_world.log")],
            stdout=open(self.logs / "mock_world.out", "wb"), stderr=subprocess.STDOUT, env=world_env,
        ))  # fmt: skip
        self.procs.append(subprocess.Popen(
            [self.python, str(SRC / "mock_solver.py"), str(self.solver_port)],
            stdout=open(self.logs / "mock_solver.out", "wb"), stderr=subprocess.STDOUT, env={**os.environ, "MODE": "better"},
        ))  # fmt: skip
        wait_http(self.mock_url, json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": []}).encode())
        wait_http(self.solver_url + "/solve", json.dumps({"id": "0", "orders": [], "tokens": {}}).encode())

    def stop(self) -> None:
        for p in self.procs:
            p.terminate()
        for p in self.procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()

    def make_record(self) -> None:
        """The record under test = this working tree (committed or not), as a fresh git repo on
        `main`, with a bare repository standing in for GitHub as `origin`."""
        must(sh(["git", "init", "--bare", "-b", "main", str(self.remote)]), "git init --bare")
        self.work.mkdir()
        must(sh(["rsync", "-a", "--exclude", ".git", "--exclude", "build", f"{RECORD}/", f"{self.work}/"]), "rsync record")
        g = ["git", "-c", "user.name=e2e", "-c", "user.email=e2e@example.invalid"]
        must(sh(["git", "init", "-q", "-b", "main"], cwd=self.work), "git init")
        must(sh(["git", "add", "-A"], cwd=self.work), "git add")
        must(sh([*g, "commit", "-q", "-m", "record under test"], cwd=self.work), "git commit")
        must(sh(["git", "remote", "add", "origin", str(self.remote)], cwd=self.work), "git remote add")
        must(sh(["git", "push", "-q", "-u", "origin", "main"], cwd=self.work), "git push main")
        (self.work / ".git" / "config").open("a").write("[user]\n\tname = e2e\n\temail = e2e@example.invalid\n")

    def config(self, duration: str, watch_s: int, blocks: int) -> Path:
        cfg = self.root / "monthly.e2e.json"
        cfg.write_text(json.dumps({
            "evidence_root": str(self.evidence), "remote": "origin",
            "forum_link": "https://forum.cow.fi/t/3572", "duration": duration, "grace_seconds": 30,
            "max_restarts": 2, "restart_backoff_seconds": 5,
            "chains": {"arbitrum-one": {"solver_url": self.solver_url, "own_addresses": [KAISER], "rpc_env": "E2E_RPC",
                                        "engine_sha_cmd": "echo e2e0c0ffee", "watch_seconds": watch_s, "blocks": blocks}},
        }, indent=1))  # fmt: skip
        return cfg

    def env(self) -> dict[str, str]:
        return {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}", "E2E_MOCK_URL": self.mock_url,
                "E2E_GH_LOG": str(self.logs / "gh.log"), "E2E_RPC": self.mock_url, "COW_BACKTESTER_SRC": str(SRC)}  # fmt: skip


def run_month(w: World, cfg: Path, timeout: int) -> tuple[subprocess.CompletedProcess[str], str]:
    run_id = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    p = sh([w.python, str(w.work / "tools" / "run_month.py"), "--chain", "arbitrum-one", "--config", str(cfg),
            "--record-root", str(w.work)], cwd=w.work, env=w.env(), timeout=timeout)  # fmt: skip
    (w.logs / "run_month.out").write_text(p.stdout + "\n--- stderr ---\n" + p.stderr)
    return p, run_id


def check(w: World, run_id: str, result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    """Every assertion; raises AssertionError with the first failure."""
    branch = f"readiness/arbitrum-one-{run_id}"
    assert result.returncode == 0, f"run_month exited {result.returncode}"
    remote_branches = must(sh(["git", "--git-dir", str(w.remote), "branch", "--list"]), "git branch").stdout
    assert branch in remote_branches, f"branch {branch} was not pushed; remote has: {remote_branches}"
    check_dir = w.root / "check"
    must(sh(["git", "clone", "-q", "-b", branch, str(w.remote), str(check_dir)]), "git clone from the bare remote")
    # the record's own integrity (every run), then THIS run's evidence the way the README tells a
    # reader to check it: `sha256sum -c EVIDENCE.sha256` from the evidence root. The September runs'
    # private evidence is not under this root, so `verify --evidence-root` over every run cannot apply.
    verify = sh([w.python, str(check_dir / "tools" / "verify.py"), "--root", str(check_dir)])
    assert verify.returncode == 0, f"verify on the cloned branch failed:\n{verify.stdout}{verify.stderr}"
    evidence_sums = check_dir / "runs" / "arbitrum-one" / run_id / "EVIDENCE.sha256"
    sums = sh(["sha256sum", "-c", "--quiet", str(evidence_sums)], cwd=w.evidence)
    assert sums.returncode == 0, f"this run's evidence does not match its fingerprints:\n{sums.stdout}{sums.stderr}"
    run_dir = check_dir / "runs" / "arbitrum-one" / run_id
    run = json.loads((run_dir / "run.json").read_text())
    report = (run_dir / "report.md").read_text()
    assert run["verdict"] in ("READY", "REVIEW", "NOT READY"), run["verdict"]
    assert run["window"]["from_block"] < run["window"]["to_block"]
    assert run["figures"]["attempted"] >= 10, f"only {run['figures']['attempted']} auctions attempted"
    assert run["generator"]["path"] == "tools/build_report.py" and run["engine"] == {"build_sha": "e2e0c0ffee"}
    assert len(run["evidence"]) >= 2 and run["evidence"][-1]["kind"] == "body-archive-manifest"
    assert f"{run_id}-arbitrum-one/manifest.jsonl" in run["evidence"][-1]["path"], run["evidence"][-1]["path"]
    assert "READINESS — kaisersolver" in report and "## Reproduce" in report and "## Method notes" in report
    index = json.loads((check_dir / "index.json").read_text())
    assert any(r["run_id"] == run_id and r["chain"] == "arbitrum-one" for r in index["runs"])
    gh_log = (w.logs / "gh.log").read_text().splitlines()
    assert any(line.startswith("auth status") for line in gh_log), gh_log
    assert any(line.startswith("pr create") for line in gh_log), gh_log
    evidence_files = sorted((w.evidence / "runs" / "arbitrum-one").glob(f"{run_id}T*.jsonl"))
    assert evidence_files, "no evidence file written"
    metas = sum(1 for f in evidence_files for ln in f.read_text().splitlines() if '"_meta"' in ln)
    assert metas >= 2, f"only {metas} watch cycle(s) completed"
    assert not (w.work / ".cowbt-cache").exists(), "the tool's cache landed inside the record checkout"
    assert (w.evidence / ".cowbt-cache").exists(), "the tool's cache was not redirected to the evidence root"
    status = must(sh(["git", "status", "--porcelain"], cwd=w.work), "git status").stdout
    assert status.strip() == "", f"record checkout left dirty:\n{status}"
    return {"verdict": run["verdict"], "attempted": run["figures"]["attempted"], "cycles": metas,
            "launches": len(evidence_files), "warns": run["figures"]["warns"],
            "fails": [c["label"] for c in run["checks"] if c["level"] == "fail"], "branch": branch,
            "report": str(run_dir / "report.md"), "verify": verify.stdout.strip()}  # fmt: skip


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--python", default=sys.executable, help="interpreter with cow_backtester importable")
    ap.add_argument("--duration", default="150s", help="how long the watch runs (the run's 20 h)")
    ap.add_argument("--watch", type=int, default=10, help="--watch seconds between cycles")
    ap.add_argument("--blocks", type=int, default=400, help="the first cycle's look-back")
    ap.add_argument("--step", type=int, default=20, help="mock chain: blocks between settlements")
    ap.add_argument("--head-start", type=int, default=120, help="mock chain: seconds of history at start")
    ap.add_argument("--workdir", type=Path, default=None, help="keep everything here (default: a temp dir, removed on success)")
    a = ap.parse_args(argv)
    root = a.workdir or Path(tempfile.mkdtemp(prefix="readiness-e2e-"))
    root.mkdir(parents=True, exist_ok=True)
    w = World(root, a.python)
    try:
        w.start(a.step, a.head_start)
        w.make_record()
        cfg = w.config(a.duration, a.watch, a.blocks)
        seconds = int(a.duration.rstrip("smh")) * {"s": 1, "m": 60, "h": 3600}[a.duration[-1]]
        result, run_id = run_month(w, cfg, timeout=seconds + 300)
        summary = check(w, run_id, result)
    except (AssertionError, SystemExit) as e:
        print(json.dumps({"e2e": "FAILED", "error": str(e), "workdir": str(root)}), file=sys.stderr)
        for name in ("run_month.out", "mock_solver.out", "mock_world.out"):
            p = w.logs / name
            if p.exists():
                print(f"--- {name} (tail) ---\n" + "\n".join(p.read_text().splitlines()[-25:]), file=sys.stderr)
        return 1
    finally:
        w.stop()
    print(json.dumps({"e2e": "PASSED", **summary, "workdir": str(root)}, indent=1))
    if not a.workdir:
        shutil.rmtree(root, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
