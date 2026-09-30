#!/usr/bin/env python3
"""The monthly readiness run, end to end, on the engine host.

    tools/run_month.py --chain base                      # the real thing (cron, 1st of the month)
    tools/run_month.py --chain base --dry-run            # everything but git/gh; prints the commands
    tools/run_month.py --chain base --skip-run --run-id 2026-10-01   # rebuild from evidence already on disk

Stages, each leaving what it made on disk when the next one fails:
  1. preflight  — config, tool version, engine sha, a clean record checkout on main, the run id free
  2. run        — `cow-backtester --readiness --watch … --json-out … --archive-bodies …` until the
                  deadline (SIGINT, then SIGTERM after the grace); relaunched on exit 1, each launch
                  its own evidence file; exit 130 means the last cycle was cut short
  3. build      — tools/build_report.py over every evidence file of the run (in-process)
  4. record     — tools/add_run.py, then tools/verify.py --evidence-root
  5. publish    — branch readiness/<chain>-<run-id>, commit, push, `gh pr create` when gh is
                  authenticated; otherwise (or with --dry-run) the exact commands are printed

Config is tools/monthly.json (no secrets); the RPC URL comes from the env var it names, GH_TOKEN
from the env. Every external call goes through `Shell`, so the tests drive a fake one.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import rows as rowsmod  # noqa: E402

ENGINE_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
DURATION_RE = re.compile(r"^(\d+)([hms])$")
SOLVER = "kaisersolver"
EXIT_USAGE, EXIT_FAILED, EXIT_INTERRUPTED = 2, 1, 130


# ------------------------------------------------------------------ config


@dataclass(frozen=True)
class ChainConfig:
    slug: str
    solver_url: str
    own_addresses: list[str]
    rpc_env: str
    engine_sha_cmd: str | None
    watch_seconds: int = 60
    blocks: int = 2000


@dataclass(frozen=True)
class Config:
    evidence_root: Path
    remote: str
    forum_link: str
    duration_s: int
    grace_seconds: int
    max_restarts: int
    restart_backoff_seconds: int
    chains: dict[str, ChainConfig]

    @classmethod
    def load(cls, path: Path) -> Config:
        try:
            raw = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            raise SystemExit(f"cannot read config {path}: {e}") from e
        chains = {
            slug: ChainConfig(slug=slug, **{k: v for k, v in c.items() if not k.startswith("_")})
            for slug, c in raw["chains"].items()
        }
        return cls(
            evidence_root=Path(raw["evidence_root"]),
            remote=raw.get("remote", "origin"),
            forum_link=raw["forum_link"],
            duration_s=parse_duration(raw.get("duration", "20h")),
            grace_seconds=int(raw.get("grace_seconds", 600)),
            max_restarts=int(raw.get("max_restarts", 5)),
            restart_backoff_seconds=int(raw.get("restart_backoff_seconds", 60)),
            chains=chains,
        )


def parse_duration(text: str) -> int:
    m = DURATION_RE.match(text.strip())
    if not m:
        raise SystemExit(f"bad duration {text!r}; use e.g. 20h, 90m, 300s")
    return int(m[1]) * {"h": 3600, "m": 60, "s": 1}[m[2]]


# ------------------------------------------------------------------ the outside world


@dataclass
class Completed:
    returncode: int
    stdout: str = ""
    stderr: str = ""


class Shell(Protocol):
    """Every external effect the runner has. Tests inject a fake."""

    def run(self, cmd: list[str], cwd: Path | None = None, timeout: float | None = None) -> Completed: ...
    def watch(self, cmd: list[str], deadline_ts: float, grace_s: int) -> int: ...
    def sleep(self, seconds: float) -> None: ...
    def now(self) -> float: ...
    def block_number(self, rpc_url: str) -> int | None: ...


class RealShell:
    def run(self, cmd: list[str], cwd: Path | None = None, timeout: float | None = None) -> Completed:
        try:
            p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as e:
            return Completed(EXIT_FAILED, "", f"{cmd[0]}: {e}")
        return Completed(p.returncode, p.stdout, p.stderr)

    def watch(self, cmd: list[str], deadline_ts: float, grace_s: int) -> int:
        """Run the tool until the deadline: SIGINT (the tool's clean stop), then SIGTERM after the grace."""
        try:
            p = subprocess.Popen(cmd)
        except OSError as e:
            print(f"run_month: cannot launch {cmd[0]}: {e}", file=sys.stderr)
            return EXIT_USAGE
        try:
            return p.wait(timeout=max(0.0, deadline_ts - time.time()))
        except subprocess.TimeoutExpired:
            p.send_signal(signal.SIGINT)
            try:
                return p.wait(timeout=grace_s)
            except subprocess.TimeoutExpired:
                p.terminate()
                return p.wait(timeout=60)

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def now(self) -> float:
        return time.time()

    def block_number(self, rpc_url: str) -> int | None:
        import urllib.request

        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []}).encode()
        req = urllib.request.Request(rpc_url, data=body, headers={"content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return int(json.load(r)["result"], 16)
        except (OSError, ValueError, KeyError) as e:
            print(f"run_month: eth_blockNumber via {rpc_url} failed ({e}); using the default --blocks", file=sys.stderr)
            return None


# ------------------------------------------------------------------ the run


@dataclass
class Launch:
    file: Path
    exit_code: int


@dataclass
class RunPlan:
    chain: ChainConfig
    cfg: Config
    run_id: str
    start_ts: float
    record_root: Path
    engine_sha: str
    rpc_url: str
    dry_run: bool
    force: bool
    build_root: Path
    skip_run: bool = False
    launches: list[Launch] = field(default_factory=list)

    @property
    def evidence_dir(self) -> Path:
        return self.cfg.evidence_root / "runs" / self.chain.slug

    @property
    def archive_rel(self) -> Path:
        """One archive per run and chain: the tool appends to the manifest for as long as it runs,
        and every recorded run fingerprints its manifest, so a shared archive would break the
        earlier runs' evidence the moment the next watch started. The September runs used one
        shared `archive/bodies-0.11/`; `--skip-run` falls back to it when the per-run one is absent."""
        per_run = Path("archive") / f"bodies-{tool_minor_version()}" / f"{self.run_id}-{self.chain.slug}"
        legacy = Path("archive") / f"bodies-{tool_minor_version()}"
        legacy_only = (
            not (self.cfg.evidence_root / per_run).exists() and (self.cfg.evidence_root / legacy / "manifest.jsonl").exists()
        )
        return legacy if self.skip_run and legacy_only else per_run

    @property
    def archive_dir(self) -> Path:
        return self.cfg.evidence_root / self.archive_rel

    @property
    def build_dir(self) -> Path:
        return self.build_root / self.chain.slug / self.run_id

    @property
    def run_dir(self) -> Path:
        return self.record_root / "runs" / self.chain.slug / self.run_id

    @property
    def branch(self) -> str:
        return f"readiness/{self.chain.slug}-{self.run_id}"


def tool_minor_version() -> str:
    from cow_backtester import backtest as bt

    return ".".join(bt.VERSION.split(".")[:2])


def utc(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, timezone.utc)


def resolve_engine_sha(chain: ChainConfig, override: str | None, shell: Shell) -> str:
    if override:
        sha = override.strip()
    elif chain.engine_sha_cmd:
        r = shell.run(["sh", "-c", chain.engine_sha_cmd], timeout=30)
        if r.returncode != 0:
            raise SystemExit(f"engine_sha_cmd failed ({r.returncode}): {r.stderr.strip()}")
        sha = r.stdout.strip()
    else:
        raise SystemExit(f"no engine build sha for {chain.slug}: set engine_sha_cmd in monthly.json or pass --engine-sha")
    if not ENGINE_SHA_RE.match(sha):
        raise SystemExit(f"engine build sha {sha!r} is not 7-40 hex chars")
    return sha


def preflight(plan: RunPlan, shell: Shell, skip_run: bool) -> None:
    from cow_backtester import backtest as bt

    ver = shell.run(["cow-backtester", "--version"], timeout=30)
    if ver.returncode != 0 or bt.VERSION not in ver.stdout:
        raise SystemExit(
            f"cow-backtester on PATH is {ver.stdout.strip() or ver.stderr.strip()!r}, the importable one is {bt.VERSION}"
        )
    if plan.run_dir.exists() and not plan.force:
        raise SystemExit(f"{plan.run_dir} already exists (use --force to re-record)")
    status = shell.run(["git", "status", "--porcelain"], cwd=plan.record_root)
    if status.returncode != 0 or status.stdout.strip():
        raise SystemExit(f"record checkout {plan.record_root} is not clean:\n{status.stdout}{status.stderr}")
    branch = shell.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=plan.record_root).stdout.strip()
    if branch != "main" and not plan.force:
        raise SystemExit(f"record checkout is on {branch!r}, not main (use --force)")
    if not plan.rpc_url and not skip_run:
        raise SystemExit(f"no RPC URL: set {plan.chain.rpc_env} in the environment")
    for d in (plan.evidence_dir, plan.archive_dir, plan.build_dir):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise SystemExit(f"cannot create {d}: {e}") from e


def tool_command(plan: RunPlan, out_file: Path, blocks: int) -> list[str]:
    c = plan.chain
    return [
        "cow-backtester", "--chain", c.slug, "--env", "prod", "--readiness", "--compete",
        "--watch", str(c.watch_seconds), "--blocks", str(blocks), "--rpc-url", plan.rpc_url,
        "--solver-url", c.solver_url, "--solver-name", SOLVER,
        "--json-out", str(out_file), "--archive-bodies", str(plan.archive_dir), "--quiet",
    ]  # fmt: skip


def last_to_block(files: list[Path]) -> int | None:
    read = rowsmod.read_jsonl(files)
    return max((int(m["to_block"]) for m in read.metas), default=None)


def blocks_for_relaunch(plan: RunPlan, shell: Shell) -> int:
    """A relaunch scans from the previous cycle's end, not `--blocks` back from head, so the two
    files do not overlap; if the head cannot be read the default applies and the overlap is
    disclosed by build_report instead."""
    last_to = last_to_block([launch.file for launch in plan.launches])
    head = shell.block_number(plan.rpc_url) if last_to is not None else None
    if last_to is None or head is None or head <= last_to:
        return plan.chain.blocks
    return head - last_to


def run_watch(plan: RunPlan, shell: Shell) -> None:
    deadline = plan.start_ts + plan.cfg.duration_s
    restarts = 0
    while True:
        out_file = plan.evidence_dir / (utc(shell.now()).strftime("%Y-%m-%dT%H%M%SZ") + ".jsonl")
        blocks = plan.chain.blocks if not plan.launches else blocks_for_relaunch(plan, shell)
        rc = shell.watch(tool_command(plan, out_file, blocks), deadline, plan.cfg.grace_seconds)
        plan.launches.append(Launch(out_file, rc))
        print(json.dumps({"stage": "run", "launch": len(plan.launches), "file": str(out_file), "exit": rc}))
        if rc in (0, EXIT_INTERRUPTED):
            return
        if rc == EXIT_USAGE:
            raise SystemExit(f"cow-backtester refused the arguments (exit 2); see {out_file}")
        restarts += 1
        if restarts > plan.cfg.max_restarts or shell.now() + plan.cfg.restart_backoff_seconds >= deadline:
            raise SystemExit(f"cow-backtester failed {restarts} time(s); evidence kept under {plan.evidence_dir}")
        shell.sleep(plan.cfg.restart_backoff_seconds)


def evidence_files_for(plan: RunPlan) -> list[Path]:
    """--skip-run: the launches of this run are the files dated its run-id day."""
    if plan.launches:
        return [launch.file for launch in plan.launches]
    files = sorted(plan.evidence_dir.glob(f"{plan.run_id}T*.jsonl"))
    if not files:
        raise SystemExit(f"no evidence files {plan.run_id}T*.jsonl under {plan.evidence_dir}")
    return files


def resolve_window(plan: RunPlan, files: list[Path]) -> rowsmod.Window:
    read = rowsmod.read_jsonl(files)
    cut_short = bool(plan.launches) and plan.launches[-1].exit_code == EXIT_INTERRUPTED
    w = rowsmod.resolve_window(read.metas_by_file, drop_last_cycle=cut_short)
    (plan.build_dir / "window.json").write_text(
        json.dumps(
            {"from_block": w.lo, "to_block": w.hi, "last_cycle_dropped": cut_short, "files": [f.name for f in files]}, indent=1
        )
        + "\n"
    )
    return w


def build(plan: RunPlan, files: list[Path], w: rowsmod.Window) -> None:
    import build_report

    argv = [
        "--chain", plan.chain.slug, "--from-block", str(w.lo), "--to-block", str(w.hi), "--compete",
        "--solver-url", plan.chain.solver_url, "--engine-sha", plan.engine_sha,
        "--manifest", str(plan.archive_dir / "manifest.jsonl"), "--watch-seconds", str(plan.chain.watch_seconds),
        "--record-root", str(plan.record_root), "--out-dir", str(plan.build_dir),
    ]  # fmt: skip
    for a in plan.chain.own_addresses:
        argv += ["--own-address", a]
    for f in files:
        argv += ["--rows", str(f)]
    if build_report.build(argv) != 0:
        raise SystemExit("build_report failed")


def record(plan: RunPlan, files: list[Path]) -> None:
    import add_run
    import verify

    argv = [
        "--root", str(plan.record_root), "--chain", plan.chain.slug, "--run-id", plan.run_id,
        "--report", str(plan.build_dir / "report.md"), "--evidence-root", str(plan.cfg.evidence_root),
        "--figures", str(plan.build_dir / "figures.json"), "--meta", str(plan.build_dir / "meta.json"),
        "--engine-sha", plan.engine_sha, "--link", f"forum={plan.cfg.forum_link}", "--note", run_note(plan),
    ]  # fmt: skip
    for f in files:
        argv += ["--evidence", f"runs/{plan.chain.slug}/{f.name}:rows"]
    argv += ["--evidence", f"{plan.archive_rel.as_posix()}/manifest.jsonl:body-archive-manifest"]
    if plan.force:
        argv.append("--force")
    add_run.main(argv)
    # the record's own integrity over every run, then THIS run's evidence against the files on disk;
    # `verify --evidence-root` would also demand the earlier runs' private evidence under the same root
    if verify.main(["--root", str(plan.record_root)]) != 0:
        raise SystemExit("verify failed after recording; the run directory is written but not committed")
    check_own_evidence(plan)


def check_own_evidence(plan: RunPlan) -> None:
    import record as recordmod

    run = json.loads((plan.run_dir / "run.json").read_text())
    bad = []
    for e in run.get("evidence", []):
        path = plan.cfg.evidence_root / e["path"]
        try:
            ok = recordmod.sha256_file(path) == e["sha256"] and path.stat().st_size == e["bytes"]
        except OSError:
            ok = False
        if not ok:
            bad.append(e["path"])
    if bad:
        raise SystemExit(f"evidence changed between recording and checking: {', '.join(bad)}")


def run_note(plan: RunPlan) -> str:
    start = utc(plan.start_ts).isoformat(timespec="seconds").replace("+00:00", "Z")
    exits = ", ".join(str(launch.exit_code) for launch in plan.launches) or "n/a (--skip-run)"
    return (
        f"Monthly run: watch from {start} for {plan.cfg.duration_s // 3600} h, {len(plan.launches) or 'existing'} launch(es), "
        f"exit code(s) {exits}; built by tools/run_month.py. The body archive is shared by every chain that ran that day."
    )


def publish_commands(plan: RunPlan) -> list[list[str]]:
    verdict = json.loads((plan.build_dir / "readiness.json").read_text())["verdict"]
    return [
        ["git", "switch", "-c", plan.branch],
        ["git", "add", f"runs/{plan.chain.slug}/{plan.run_id}", "index.json", "README.md", "SHA256SUMS"],
        ["git", "commit", "-m", f"readiness: {plan.chain.slug} {plan.run_id} ({verdict})"],
        ["git", "push", "-u", plan.cfg.remote, plan.branch],
    ]


def write_pr_body(plan: RunPlan) -> Path:
    rep = json.loads((plan.build_dir / "readiness.json").read_text())
    warns = [c["label"] for c in rep["checks"] if c["level"] == "warn"]
    fails = [c["label"] for c in rep["checks"] if c["level"] == "fail"]
    launches = len(plan.launches) or "existing"
    body = (
        f"Readiness run `{plan.chain.slug}/{plan.run_id}` — **{rep['verdict']}**\n\n"
        f"- attempted {rep['auctions_attempted']}, bid coverage {rep['bid_rate_pct'] or 0:.0f} %, "
        f"capture {rep['capture_ex_artefact_pct'] or 0:.1f} % (coverage-adjusted, artefacts excluded)\n"
        f"- fails: {', '.join(fails) or 'none'}; warns: {', '.join(warns) or 'none'}\n"
        f"- window blocks {rep['window']['from_block']}–{rep['window']['to_block']}, {launches} launch(es)\n\n"
        f"Recorded by `tools/run_month.py`; report and figures by `tools/build_report.py`. "
        f"Review `runs/{plan.chain.slug}/{plan.run_id}/report.md`, edit the bottom line if you want, "
        f"then re-run `tools/add_run.py --force` before merging so the checksums follow.\n"
    )
    path = plan.build_dir / "pr.md"
    path.write_text(body)
    return path


def publish(plan: RunPlan, shell: Shell) -> None:
    cmds = publish_commands(plan)
    pr_body = write_pr_body(plan)
    title = f"readiness: {plan.chain.slug} {plan.run_id}"
    create = ["gh", "pr", "create", "--title", title, "--body-file", str(pr_body), "--head", plan.branch]
    if plan.dry_run:
        print("dry run — would run, in " + str(plan.record_root) + ":")
        for c in [*cmds, create]:
            print("  " + " ".join(c))
        return
    for c in cmds:
        r = shell.run(c, cwd=plan.record_root, timeout=300)
        if r.returncode != 0:
            raise SystemExit(
                f"{' '.join(c[:2])} failed ({r.returncode}): {r.stderr.strip()}\nremaining steps:\n  "
                + "\n  ".join(" ".join(x) for x in [*cmds[cmds.index(c) :], create])
            )
    if shell.run(["gh", "auth", "status"], timeout=60).returncode != 0:
        print("gh is not authenticated here; open the PR by hand:\n  " + " ".join(create))
        return
    existing = shell.run(
        ["gh", "pr", "list", "--head", plan.branch, "--json", "url", "--jq", ".[0].url"], cwd=plan.record_root, timeout=60
    )
    if existing.returncode == 0 and existing.stdout.strip():
        print(f"PR already open: {existing.stdout.strip()}")
        return
    created = shell.run(create, cwd=plan.record_root, timeout=120)
    if created.returncode != 0:
        raise SystemExit(f"gh pr create failed ({created.returncode}): {created.stderr.strip()}")
    print(f"PR opened: {created.stdout.strip()}")


# ------------------------------------------------------------------ CLI


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--chain", required=True)
    ap.add_argument("--config", type=Path, default=HERE / "monthly.json")
    ap.add_argument("--record-root", type=Path, default=HERE.parent)
    ap.add_argument("--build-root", type=Path, default=None, help="scratch outputs (default: <record-root>/build, git-ignored)")
    ap.add_argument("--evidence-root", type=Path, default=None, help="override monthly.json")
    ap.add_argument("--start", default=None, help="ISO UTC start (default: now); the run id is its date")
    ap.add_argument("--run-id", default=None, help="override the run id (YYYY-MM-DD[suffix])")
    ap.add_argument("--duration", default=None, help="override monthly.json, e.g. 20h")
    ap.add_argument("--engine-sha", default=None)
    ap.add_argument("--skip-run", action="store_true", help="build from the evidence files of --run-id already on disk")
    ap.add_argument("--dry-run", action="store_true", help="everything but git/gh; print the commands")
    ap.add_argument("--force", action="store_true", help="re-record an existing run id / run off main")
    return ap


def make_plan(a: argparse.Namespace, shell: Shell) -> RunPlan:
    cfg = Config.load(a.config)
    if a.evidence_root or a.duration:
        cfg = Config(
            evidence_root=a.evidence_root or cfg.evidence_root,
            remote=cfg.remote,
            forum_link=cfg.forum_link,
            duration_s=parse_duration(a.duration) if a.duration else cfg.duration_s,
            grace_seconds=cfg.grace_seconds,
            max_restarts=cfg.max_restarts,
            restart_backoff_seconds=cfg.restart_backoff_seconds,
            chains=cfg.chains,
        )
    if a.chain not in cfg.chains:
        raise SystemExit(f"chain {a.chain!r} is not in {a.config}; known: {sorted(cfg.chains)}")
    chain = cfg.chains[a.chain]
    start_ts = datetime.fromisoformat(a.start.replace("Z", "+00:00")).timestamp() if a.start else shell.now()
    run_id = a.run_id or utc(start_ts).strftime("%Y-%m-%d")
    if not re.match(r"^\d{4}-\d{2}-\d{2}[a-z0-9-]*$", run_id):
        raise SystemExit(f"bad run id {run_id!r}")
    if a.skip_run and not a.start:  # rebuilding an earlier run: its day starts it, not the clock
        start_ts = datetime.fromisoformat(run_id[:10] + "T00:00:00+00:00").timestamp()
    return RunPlan(
        chain=chain,
        cfg=cfg,
        run_id=run_id,
        start_ts=start_ts,
        record_root=a.record_root.resolve(),
        build_root=(a.build_root or a.record_root / "build").resolve(),
        engine_sha=resolve_engine_sha(chain, a.engine_sha, shell),
        skip_run=a.skip_run,
        rpc_url=os.environ.get(chain.rpc_env, ""),
        dry_run=a.dry_run,
        force=a.force,
    )


def main(argv: list[str] | None = None, shell: Shell | None = None) -> int:
    a = build_parser().parse_args(argv)
    shell = shell or RealShell()
    plan = make_plan(a, shell)
    preflight(plan, shell, a.skip_run)
    if not a.skip_run:
        run_watch(plan, shell)
    files = evidence_files_for(plan)
    w = resolve_window(plan, files)
    build(plan, files, w)
    record(plan, files)
    publish(plan, shell)
    print(
        json.dumps({"stage": "done", "run": f"{plan.chain.slug}/{plan.run_id}", "branch": plan.branch, "dry_run": plan.dry_run})
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
