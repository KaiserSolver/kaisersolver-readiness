#!/usr/bin/env python3
"""The monthly readiness run, end to end, on the engine host.

    python3 tools/run_month.py --chain base                  # build + record + verify locally, print the push commands
    python3 tools/run_month.py --chain base --publish        # ... and push the branch and open the pull request
    python3 tools/run_month.py --chain base --dry-run        # replay and build only; touches no git state
    python3 tools/run_month.py --chain base --skip-run --run-id 2026-10-01   # rebuild from evidence already on disk

Stages, each leaving what it made on disk when the next one fails:
  1. preflight  — config and host settings, tool version, free disk, the engine build sha, the
                  record checkout's `tools/` equal to <remote>/main, the run id free there
  2. probe      — one real, small POST /solve to the replay target; anything but HTTP 200 stops the run
  3. run        — `cow-backtester --readiness --watch …` until the deadline, under `ionice -c3 nice -n 19`
                  in its own session; relaunched after a crash; the output of every launch is kept
  4. build      — tools/build_report.py over every evidence file of the run (in-process)
  5. gate       — bids, transport and deadline-miss share, scan gaps, engine changes, partial runs
  6. record     — in a fresh git worktree cut from <remote>/main: tools/add_run.py, tools/verify.py, one
                  commit as kaisersolver. `--dry-run` stops before this stage.
  7. publish    — only with --publish and a passing gate: push the branch and `gh pr create`, authenticated
                  with the repo-scoped token in the environment variable named by `publish_token_env`.
                  Without --publish the exact commands are printed.

Stopping. At the deadline the runner sends the tool SIGINT and repeats it every 30 s (the tool treats the
first as "finish this cycle with partial data" and only the next one, in its sleep, as "stop"), then
SIGTERM after the grace, then SIGKILL. A stop we sent is a clean stop whatever the exit code (0, 130, -15),
and the cycle that was in flight is dropped from the window. What happened is written to
`<run-id>.plan.json` next to the evidence, which is what `--skip-run` reads.

Host settings (evidence root, replay target, the engine-sha command) live in the gitignored
tools/monthly.local.json, merged over tools/monthly.json; see tools/monthly.local.example.json.
`engine_sha_cmd` is an argv list run without a shell, on the host, and is read only from the overlay.
RPC URLs come from the environment variable `rpc_env` names. They go on the tool's command line (the tool
has no other route), so they are visible in `ps` while it runs; use a key made for this job.
Every external call goes through `Shell`, so the tests drive a fake one.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import rows as rowsmod  # noqa: E402

ENGINE_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
DURATION_RE = re.compile(r"^(\d+)([hms])$")
SOLVER = "kaisersolver"
IDENTITY_NAME = "kaisersolver"
IDENTITY_EMAIL = "kaisersolver@users.noreply.github.com"
EXIT_USAGE, EXIT_FAILED, EXIT_INTERRUPTED, EXIT_GATE, EXIT_BUSY = 2, 1, 130, 3, 75
RELAUNCH_SLACK_BLOCKS = 300  # a relaunch asks for this many blocks more than the gap, so the two files overlap
ARCHIVE_ERROR_MARKERS = ("archive writes failed", "--archive-bodies write failed")
REPORT_SOLVER_URL = "<solver-url>"  # what the public report says the replay target was
PROBE_BODY = {
    "id": "0", "orders": [], "tokens": {}, "liquidity": [], "effectiveGasPrice": "0",
    "surplusCapturingJitOrderOwners": [],
}  # fmt: skip


# ------------------------------------------------------------------ config


@dataclass(frozen=True)
class ChainConfig:
    slug: str
    solver_url: str | None
    own_addresses: list[str]
    rpc_env: str
    engine_sha_cmd: list[str] | None = None
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
    cache: str = "off"  # "off" = the tool's --no-cache; otherwise a directory (never inside the record)
    min_free_gb: float = 30.0
    max_bad_share_pct: float = 5.0  # transport + deadline-miss share of attempted auctions
    publish_token_env: str = "GH_TOKEN"

    @classmethod
    def load(cls, path: Path, overlay: Path | None = None) -> Config:
        raw = read_json(path)
        if any(c.get("engine_sha_cmd") for c in raw.get("chains", {}).values() if isinstance(c, dict)):
            raise SystemExit(f"engine_sha_cmd is a host command: set it in the untracked overlay, not in {path.name}")
        if overlay is not None and overlay.exists():
            raw = deep_merge(raw, read_json(overlay))
        chains = {
            slug: ChainConfig(slug=slug, **{k: normalise_field(k, v) for k, v in c.items() if not k.startswith("_")})
            for slug, c in raw["chains"].items()
        }
        return cls(
            evidence_root=Path(str(raw.get("evidence_root") or "~/readiness-evidence")).expanduser(),
            remote=raw.get("remote", "origin"),
            forum_link=raw["forum_link"],
            duration_s=parse_duration(raw.get("duration", "20h")),
            grace_seconds=int(raw.get("grace_seconds", 600)),
            max_restarts=int(raw.get("max_restarts", 5)),
            restart_backoff_seconds=int(raw.get("restart_backoff_seconds", 60)),
            chains=chains,
            cache=str(raw.get("cache", "off")),
            min_free_gb=float(raw.get("min_free_gb", 30)),
            max_bad_share_pct=float(raw.get("max_bad_share_pct", 5.0)),
            publish_token_env=str(raw.get("publish_token_env", "GH_TOKEN")),
        )


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise SystemExit(f"cannot read config {path}: {e}") from e
    if not isinstance(data, dict):
        raise SystemExit(f"config {path} is not a JSON object")
    return data


def deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in over.items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def normalise_field(key: str, value: Any) -> Any:
    if key == "engine_sha_cmd" and isinstance(value, str):
        return shlex.split(value) or None
    return value


def parse_duration(text: str) -> int:
    m = DURATION_RE.match(text.strip())
    if not m:
        raise SystemExit(f"bad duration {text!r}; use e.g. 20h, 90m, 300s")
    return int(m[1]) * {"h": 3600, "m": 60, "s": 1}[m[2]]


def running_as_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


# ------------------------------------------------------------------ the outside world


@dataclass
class Completed:
    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass
class WatchResult:
    returncode: int
    signalled: bool = False  # we sent the tool a stop signal (deadline, disk, archive errors, runner signal)
    stop_reason: str | None = None
    escalation: str | None = None  # the strongest signal that was needed: sigint | sigterm | sigkill


class Shell(Protocol):
    """Every external effect the runner has. Tests inject a fake."""

    def run(
        self, cmd: list[str], cwd: Path | None = None, timeout: float | None = None, env: dict[str, str] | None = None
    ) -> Completed: ...
    def watch(
        self, cmd: list[str], run_s: float, grace_s: int, log_path: Path, should_stop: Callable[[], str | None] | None = None
    ) -> WatchResult: ...
    def sleep(self, seconds: float) -> None: ...
    def now(self) -> float: ...
    def monotonic(self) -> float: ...
    def block_number(self, rpc_url: str, label: str) -> int | None: ...
    def post_json(self, url: str, body: dict[str, Any], timeout: float) -> tuple[int | None, str]: ...
    def free_bytes(self, path: Path) -> int: ...


def wrap_priority(cmd: list[str]) -> list[str]:
    """The tool runs at idle IO and lowest CPU priority where the host has the commands for it."""
    out = list(cmd)
    if shutil.which("nice"):
        out = ["nice", "-n", "19", *out]
    if shutil.which("ionice"):
        out = ["ionice", "-c3", *out]
    return out


def _child_setup() -> None:
    # a runner started from a background shell inherits SIGINT=SIG_IGN, and so would the tool,
    # which would then never see the clean stop
    signal.signal(signal.SIGINT, signal.SIG_DFL)


def redact(text: str, secret: str) -> str:
    return text.replace(secret, "<secret>") if secret else text


class RealShell:
    def __init__(self, poll_s: float = 5.0, resend_s: float = 30.0, term_wait_s: float = 60.0, kill_wait_s: float = 30.0):
        self.poll_s, self.resend_s, self.term_wait_s, self.kill_wait_s = poll_s, resend_s, term_wait_s, kill_wait_s
        self._runner_signal: int | None = None

    def run(
        self, cmd: list[str], cwd: Path | None = None, timeout: float | None = None, env: dict[str, str] | None = None
    ) -> Completed:
        try:
            p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False, env=env)
        except (OSError, subprocess.TimeoutExpired) as e:
            return Completed(EXIT_FAILED, "", f"{cmd[0]}: {type(e).__name__}")
        return Completed(p.returncode, p.stdout, p.stderr)

    @staticmethod
    def _signal_group(p: subprocess.Popen[bytes], sig: int) -> None:
        try:
            os.killpg(p.pid, sig)  # the tool is its own session leader, so its pgid is its pid
        except (ProcessLookupError, PermissionError):
            pass

    def watch(
        self, cmd: list[str], run_s: float, grace_s: int, log_path: Path, should_stop: Callable[[], str | None] | None = None
    ) -> WatchResult:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(log_path, "ab") as log:
                p = subprocess.Popen(
                    wrap_priority(cmd), stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                    start_new_session=True, preexec_fn=_child_setup,
                )  # fmt: skip
        except OSError as e:
            print(f"run_month: cannot launch {cmd[0]}: {type(e).__name__}", file=sys.stderr)
            return WatchResult(EXIT_USAGE)
        previous = self._install_handlers()
        res = WatchResult(EXIT_FAILED)
        deadline = time.monotonic() + max(0.0, run_s)
        stop_at = last_int = step_at = 0.0
        try:
            while True:
                try:
                    res.returncode = p.wait(timeout=self.poll_s)
                    return res
                except subprocess.TimeoutExpired:
                    pass
                now = time.monotonic()
                if not res.signalled:
                    reason = "runner signal" if self._runner_signal else "deadline" if now >= deadline else None
                    if reason is None and should_stop is not None:
                        reason = should_stop()
                    if reason:
                        res.signalled, res.stop_reason, res.escalation = True, reason, "sigint"
                        stop_at = last_int = step_at = now
                        self._signal_group(p, signal.SIGINT)
                    continue
                if res.escalation == "sigint":
                    if now - stop_at >= grace_s:
                        res.escalation, step_at = "sigterm", now
                        self._signal_group(p, signal.SIGTERM)
                    elif now - last_int >= self.resend_s:
                        last_int = now
                        self._signal_group(p, signal.SIGINT)
                elif res.escalation == "sigterm" and now - step_at >= self.term_wait_s:
                    res.escalation, step_at = "sigkill", now
                    self._signal_group(p, signal.SIGKILL)
                elif res.escalation == "sigkill" and now - step_at >= self.kill_wait_s:
                    print("run_month: the tool did not die after SIGKILL; abandoning it", file=sys.stderr)
                    return res
        finally:
            if p.poll() is None:  # never leave an orphan behind, whatever happened above
                self._signal_group(p, signal.SIGKILL)
                try:
                    p.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
            self._restore_handlers(previous)

    def _install_handlers(self) -> dict[int, Any]:
        self._runner_signal = None
        previous: dict[int, Any] = {}
        try:
            for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                previous[sig] = signal.signal(sig, self._on_signal)
        except ValueError:  # not the main thread
            pass
        return previous

    def _restore_handlers(self, previous: dict[int, Any]) -> None:
        for sig, handler in previous.items():
            signal.signal(sig, handler)

    def _on_signal(self, signum: int, _frame: Any) -> None:
        self._runner_signal = signum

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def now(self) -> float:
        return time.time()

    def monotonic(self) -> float:
        return time.monotonic()

    def block_number(self, rpc_url: str, label: str) -> int | None:
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []}).encode()
        req = urllib.request.Request(rpc_url, data=body, headers={"content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return int(json.load(r)["result"], 16)
        except (OSError, ValueError, KeyError) as e:
            print(
                f"run_month: eth_blockNumber via ${label} failed ({type(e).__name__}); using the default --blocks",
                file=sys.stderr,
            )
            return None

    def post_json(self, url: str, body: dict[str, Any], timeout: float) -> tuple[int | None, str]:
        req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                r.read()
                return r.status, ""
        except urllib.error.HTTPError as e:
            return e.code, ""
        except (OSError, ValueError) as e:
            return None, type(e).__name__

    def free_bytes(self, path: Path) -> int:
        p = path
        while not p.exists() and p != p.parent:
            p = p.parent
        return shutil.disk_usage(p).free


@contextmanager
def run_lock(path: Path, wait_s: float) -> Iterator[None]:
    """One runner at a time on the host: chains never overlap, and nothing else touches the record."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as fh:
        waited = 0.0
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if waited >= wait_s:
                    print("run_month: another readiness run holds the lock; not starting", file=sys.stderr)
                    raise SystemExit(EXIT_BUSY) from None
                step = min(5.0, wait_s - waited)
                time.sleep(step)
                waited += step
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


# ------------------------------------------------------------------ the run


@dataclass
class Launch:
    file: Path
    exit_code: int
    signalled: bool = False
    stop_reason: str | None = None
    escalation: str | None = None
    engine_sha: str | None = None
    started_ts: float = 0.0
    ended_ts: float = 0.0
    metas: int = 0


@dataclass
class RunPlan:
    chain: ChainConfig
    cfg: Config
    run_id: str
    start_ts: float
    record_root: Path
    engine_override: str | None
    rpc_url: str
    dry_run: bool
    force: bool
    build_root: Path
    skip_run: bool = False
    publish: bool = False
    allow_root: bool = False
    engine_sha: str = ""
    engine_shas: list[str | None] = field(default_factory=list)
    launches: list[Launch] = field(default_factory=list)
    cut_short: bool = False
    partial_reason: str | None = None
    state_loaded: bool = False
    gate_failures: list[str] = field(default_factory=list)

    @property
    def evidence_dir(self) -> Path:
        return self.cfg.evidence_root / "runs" / self.chain.slug

    @property
    def state_path(self) -> Path:
        return self.evidence_dir / f"{self.run_id}.plan.json"

    @property
    def logs_dir(self) -> Path:
        return self.cfg.evidence_root / "logs" / self.chain.slug

    @property
    def lock_path(self) -> Path:
        return self.cfg.evidence_root / ".run_month.lock"

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
    def work_root(self) -> Path:
        """The per-run git worktree the record is written in; the main checkout is never touched."""
        return self.build_root / "worktrees" / f"{self.chain.slug}-{self.run_id}"

    @property
    def run_dir(self) -> Path:
        return self.work_root / "runs" / self.chain.slug / self.run_id

    @property
    def branch(self) -> str:
        return f"readiness/{self.chain.slug}-{self.run_id}"

    @property
    def base_ref(self) -> str:
        return f"{self.cfg.remote}/main"

    @property
    def engine_changed(self) -> bool:
        return len(set(self.engine_shas)) > 1

    def engine_shas_text(self) -> str:
        return ", ".join(sorted({s or "unreadable" for s in self.engine_shas}))


def tool_minor_version() -> str:
    from cow_backtester import backtest as bt

    return ".".join(bt.VERSION.split(".")[:2])


def utc(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, timezone.utc)


def iso(ts: float) -> str:
    return utc(ts).isoformat(timespec="seconds").replace("+00:00", "Z")


def resolve_engine_sha(chain: ChainConfig, override: str | None, shell: Shell, allow_root: bool = False) -> str:
    if override:
        sha = override.strip()
    elif chain.engine_sha_cmd:
        if running_as_root() and not allow_root:
            raise SystemExit("engine_sha_cmd runs a host command and this is root: pass --allow-root to say you reviewed it")
        r = shell.run(list(chain.engine_sha_cmd), timeout=30)  # argv, no shell
        if r.returncode != 0:
            raise SystemExit(f"engine_sha_cmd failed ({r.returncode}): {r.stderr.strip()}")
        sha = r.stdout.strip()
    else:
        raise SystemExit(f"no engine build sha for {chain.slug}: set engine_sha_cmd in monthly.local.json or pass --engine-sha")
    if not ENGINE_SHA_RE.match(sha):
        raise SystemExit(f"engine build sha {sha!r} is not 7-40 hex chars")
    return sha


def current_engine_sha(plan: RunPlan, shell: Shell) -> str | None:
    """Re-read mid-run; an unreadable value is recorded as such and counts as a change."""
    try:
        return resolve_engine_sha(plan.chain, plan.engine_override, shell, plan.allow_root)
    except SystemExit:
        return None


def missing_host_settings(plan: RunPlan) -> list[str]:
    missing = []
    if not plan.skip_run:
        if not plan.chain.solver_url:
            missing.append(f"chains.{plan.chain.slug}.solver_url (the replay target)")
        if not plan.rpc_url:
            missing.append(f"environment variable {plan.chain.rpc_env} (the RPC URL)")
    if not (plan.engine_override or plan.chain.engine_sha_cmd or plan.engine_shas):
        missing.append(f"chains.{plan.chain.slug}.engine_sha_cmd or --engine-sha")
    if plan.publish and not os.environ.get(plan.cfg.publish_token_env):
        missing.append(f"environment variable {plan.cfg.publish_token_env} (repo-scoped token for --publish)")
    return missing


def git(plan: RunPlan, shell: Shell, *args: str, cwd: Path | None = None, env: dict[str, str] | None = None) -> Completed:
    return shell.run(["git", *args], cwd=cwd or plan.record_root, timeout=300, env=env)


def preflight(plan: RunPlan, shell: Shell) -> None:
    from cow_backtester import backtest as bt

    ver = shell.run(["cow-backtester", "--version"], timeout=30)
    if ver.returncode != 0 or bt.VERSION not in ver.stdout:
        raise SystemExit(
            f"cow-backtester on PATH is {ver.stdout.strip() or ver.stderr.strip()!r}, the importable one is {bt.VERSION}"
        )
    check_disk(plan, shell, "start")
    fetched = git(plan, shell, "fetch", plan.cfg.remote, "main")
    if fetched.returncode != 0:
        raise SystemExit(f"git fetch {plan.cfg.remote} main failed: {fetched.stderr.strip()}")
    if git(plan, shell, "diff", "--quiet", plan.base_ref, "--", "tools").returncode != 0 and not plan.force:
        raise SystemExit(
            f"tools/ in {plan.record_root} differs from {plan.base_ref}: the code that runs must be the code that is "
            "recorded; update the checkout to the reviewed main (or --force)"
        )
    existing = git(plan, shell, "ls-tree", "-r", "--name-only", plan.base_ref, "--", f"runs/{plan.chain.slug}/{plan.run_id}")
    if existing.stdout.strip() and not plan.force:
        raise SystemExit(f"runs/{plan.chain.slug}/{plan.run_id} is already in {plan.base_ref} (use --force to re-record)")
    leftovers = git(plan, shell, "rev-parse", "--verify", "--quiet", f"refs/heads/{plan.branch}").returncode == 0
    if (leftovers or plan.work_root.exists()) and not plan.force:
        raise SystemExit(f"{plan.branch} / {plan.work_root} are left over from an earlier attempt (inspect, then --force)")
    if plan.publish:
        remote = git(plan, shell, "remote", "get-url", plan.cfg.remote).stdout.strip()
        if remote.startswith(("git@", "ssh://")):
            raise SystemExit("--publish authenticates with a token over https; the remote is an ssh URL")
    for d in (plan.evidence_dir, plan.logs_dir, plan.archive_dir, plan.build_dir):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise SystemExit(f"cannot create {d}: {e}") from e


def disk_floor(plan: RunPlan) -> int:
    return int(plan.cfg.min_free_gb * 2**30)


def check_disk(plan: RunPlan, shell: Shell, when: str) -> None:
    free = shell.free_bytes(plan.cfg.evidence_root)
    if free < disk_floor(plan):
        raise SystemExit(
            f"only {free / 2**30:.1f} GiB free under {plan.cfg.evidence_root} at {when}; "
            f"the floor is {plan.cfg.min_free_gb:g} GiB (a month archives about 6 GiB)"
        )


def probe_replay_target(plan: RunPlan, shell: Shell) -> None:
    """One real, small POST /solve before 20 h are spent: the target must answer 200, not just answer."""
    body = {**PROBE_BODY, "deadline": iso(shell.now() + 5).replace("Z", ".000000Z")}
    status, err = shell.post_json(f"{(plan.chain.solver_url or '').rstrip('/')}/solve", body, 15)
    if status != 200:
        raise SystemExit(
            f"replay target probe failed for {plan.chain.slug}: "
            f"{'HTTP ' + str(status) if status else 'no response (' + err + ')'}; need HTTP 200 from POST /solve. "
            "Replays aimed at an allowlisted ingress are refused (403) and would score as transport errors."
        )


def tool_command(plan: RunPlan, out_file: Path, blocks: int) -> list[str]:
    c = plan.chain
    cache = ["--no-cache"] if plan.cfg.cache == "off" else ["--cache-dir", str(Path(plan.cfg.cache).expanduser())]
    return [
        "cow-backtester", "--chain", c.slug, "--env", "prod", "--readiness", "--compete",
        "--watch", str(c.watch_seconds), "--blocks", str(blocks), "--rpc-url", plan.rpc_url,
        "--solver-url", c.solver_url or "", "--solver-name", SOLVER,
        "--json-out", str(out_file), "--archive-bodies", str(plan.archive_dir), *cache,
    ]  # fmt: skip


def count_metas(files: list[Path]) -> int:
    return len(rowsmod.read_jsonl(files).metas)


def last_to_block(files: list[Path]) -> int | None:
    read = rowsmod.read_jsonl(files)
    return max((int(m["to_block"]) for m in read.metas), default=None)


def blocks_for_relaunch(plan: RunPlan, shell: Shell) -> int:
    """A relaunch scans from the previous cycle's end: the tool re-reads the chain head itself a few
    blocks later, so the request is padded by RELAUNCH_SLACK_BLOCKS. The two files then overlap (which
    the report counts and discloses) instead of leaving blocks nobody scanned. If the head cannot be
    read the default applies, and a gap, if one results, is detected after the run."""
    last_to = last_to_block([launch.file for launch in plan.launches])
    head = shell.block_number(plan.rpc_url, plan.chain.rpc_env) if last_to is not None else None
    if last_to is None or head is None or head <= last_to:
        return plan.chain.blocks
    return head - last_to + RELAUNCH_SLACK_BLOCKS


class LogScanner:
    """Follows a launch's output for the tool's archive-write warnings (the tool only warns)."""

    def __init__(self, path: Path):
        self.path, self.offset, self.errors, self.warnings = path, 0, 0, 0

    def poll(self) -> int:
        try:
            with open(self.path, "rb") as f:
                f.seek(self.offset)
                chunk = f.read()
        except OSError:
            return self.errors
        self.offset += len(chunk)
        for line in chunk.decode("utf-8", "replace").splitlines():
            if "⚠" in line:
                self.warnings += 1
            if any(m in line for m in ARCHIVE_ERROR_MARKERS):
                self.errors += 1
        return self.errors


def save_state(plan: RunPlan) -> None:
    plan.state_path.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "version": 1, "chain": plan.chain.slug, "run_id": plan.run_id, "start_ts": plan.start_ts,
        "duration_s": plan.cfg.duration_s, "cut_short": plan.cut_short, "partial_reason": plan.partial_reason,
        "engine_shas": plan.engine_shas,
        "launches": [{**asdict(launch), "file": launch.file.name} for launch in plan.launches],
    }  # fmt: skip
    tmp = plan.state_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1) + "\n")
    tmp.replace(plan.state_path)


def load_state(plan: RunPlan) -> bool:
    try:
        s = json.loads(plan.state_path.read_text())
        plan.launches = [Launch(**{**launch, "file": plan.evidence_dir / launch["file"]}) for launch in s["launches"]]
        plan.start_ts = float(s["start_ts"])
        plan.cut_short = bool(s.get("cut_short"))
        plan.partial_reason = s.get("partial_reason")
        plan.engine_shas = list(s.get("engine_shas") or [])
    except (OSError, ValueError, KeyError, TypeError):
        return False
    plan.state_loaded = True
    return True


def run_watch(plan: RunPlan, shell: Shell) -> None:
    mono_deadline = shell.monotonic() + max(0.0, plan.start_ts + plan.cfg.duration_s - shell.now())
    failures = 0
    while True:
        remaining = mono_deadline - shell.monotonic()
        if plan.launches and remaining <= 0:
            break
        if shell.free_bytes(plan.cfg.evidence_root) < disk_floor(plan):
            if has_cycles(plan):
                plan.partial_reason = f"free disk fell below {plan.cfg.min_free_gb:g} GiB; the run stopped early"
                break
            check_disk(plan, shell, "relaunch")
        if plan.launches:
            sha = current_engine_sha(plan, shell)
            plan.engine_shas.append(sha)
        else:
            sha = plan.engine_shas[0]
        started = shell.now()
        out_file = plan.evidence_dir / (utc(started).strftime("%Y-%m-%dT%H%M%SZ") + ".jsonl")
        log_path = plan.logs_dir / (out_file.stem + ".log")
        blocks = plan.chain.blocks if not plan.launches else blocks_for_relaunch(plan, shell)
        scanner = LogScanner(log_path)

        def should_stop(scanner: LogScanner = scanner) -> str | None:
            if shell.free_bytes(plan.cfg.evidence_root) < disk_floor(plan):
                return "free disk below the floor"
            return "archive write errors" if scanner.poll() else None

        res = shell.watch(
            tool_command(plan, out_file, blocks), max(0.0, remaining), plan.cfg.grace_seconds, log_path, should_stop
        )
        scanner.poll()
        launch = Launch(
            out_file, res.returncode, res.signalled, res.stop_reason, res.escalation, sha, started, shell.now(),
            count_metas([out_file]) if out_file.exists() else 0,
        )  # fmt: skip
        plan.launches.append(launch)
        print(json.dumps({"stage": "run", "launch": len(plan.launches), "file": out_file.name, "exit": res.returncode,
                          "signalled": res.signalled, "stop_reason": res.stop_reason, "cycles": launch.metas,
                          "archive_errors": scanner.errors, "tool_warnings": scanner.warnings}))  # fmt: skip
        save_state(plan)
        if scanner.errors:
            raise SystemExit(
                f"the tool reported {scanner.errors} archive write error(s); the body archive is incomplete, nothing built "
                f"(see {log_path.name} under {plan.logs_dir}; evidence kept)"
            )
        if res.stop_reason == "runner signal":
            raise SystemExit(EXIT_INTERRUPTED)
        if res.returncode == EXIT_USAGE and not res.signalled:
            raise SystemExit(f"cow-backtester refused the arguments (exit 2); see {log_path}")
        if res.signalled or res.returncode in (0, EXIT_INTERRUPTED):
            # a stop, ours or the operator's: whatever the exit code, the cycle in flight was cut short
            plan.cut_short = launch.metas > 0
            if res.stop_reason not in (None, "deadline"):
                plan.partial_reason = f"stopped early: {res.stop_reason}"
            break
        failures = 0 if launch.metas else failures + 1
        if failures > plan.cfg.max_restarts or shell.monotonic() + plan.cfg.restart_backoff_seconds >= mono_deadline:
            if not has_cycles(plan):
                raise SystemExit(f"cow-backtester failed {failures} time(s) in a row; evidence kept under {plan.evidence_dir}")
            plan.partial_reason = f"the tool failed {failures} time(s) in a row; the run was cut off"
            break
        shell.sleep(plan.cfg.restart_backoff_seconds)
    plan.engine_shas.append(current_engine_sha(plan, shell))
    save_state(plan)


def has_cycles(plan: RunPlan) -> bool:
    files = [launch.file for launch in plan.launches if launch.file.exists()]
    return bool(files) and count_metas(files) > 0


def evidence_files_for(plan: RunPlan) -> list[Path]:
    """The launches of this run: from the plan state when there is one, else the files dated its run-id day."""
    if plan.launches:
        return [launch.file for launch in plan.launches]
    files = sorted(plan.evidence_dir.glob(f"{plan.run_id}T*.jsonl"))
    if not files:
        raise SystemExit(f"no evidence files {plan.run_id}T*.jsonl under {plan.evidence_dir}")
    print(
        f"run_month: no {plan.state_path.name}; assuming every {plan.run_id}T*.jsonl belongs to this run "
        "and that its last cycle was not cut short",
        file=sys.stderr,
    )
    return files


def find_gaps(metas_by_file: list[list[dict[str, Any]]], drop_last_cycle: bool) -> list[tuple[int, int]]:
    """Blocks no kept cycle scanned between the first cycle's start and the last cycle's end."""
    metas = [m for per_file in metas_by_file for m in per_file]
    if drop_last_cycle:
        metas = metas[:-1]
    gaps: list[tuple[int, int]] = []
    top: int | None = None
    for m in metas:
        lo, hi = int(m["from_block"]), int(m["to_block"])
        if top is not None and lo > top + 1:
            gaps.append((top + 1, lo - 1))
        top = hi if top is None else max(top, hi)
    return gaps


def resolve_window(plan: RunPlan, files: list[Path]) -> rowsmod.Window:
    read = rowsmod.read_jsonl(files)
    w = rowsmod.resolve_window(read.metas_by_file, drop_last_cycle=plan.cut_short)
    gaps = find_gaps(list(read.metas_by_file), plan.cut_short)
    if gaps:
        plan.gate_failures.append("unscanned gap between launches: " + ", ".join(f"{a}..{b}" for a, b in gaps))
    plan.build_dir.mkdir(parents=True, exist_ok=True)
    (plan.build_dir / "window.json").write_text(
        json.dumps(
            {"from_block": w.lo, "to_block": w.hi, "last_cycle_dropped": plan.cut_short, "gaps": gaps,
             "files": [f.name for f in files]},
            indent=1,
        )
        + "\n"
    )  # fmt: skip
    return w


def build(plan: RunPlan, files: list[Path], w: rowsmod.Window) -> None:
    import build_report

    argv = [
        "--chain", plan.chain.slug, "--from-block", str(w.lo), "--to-block", str(w.hi), "--compete",
        "--solver-url", REPORT_SOLVER_URL, "--engine-sha", plan.engine_sha,
        "--manifest", str(plan.archive_dir / "manifest.jsonl"), "--watch-seconds", str(plan.chain.watch_seconds),
        "--record-root", str(plan.record_root), "--out-dir", str(plan.build_dir),
    ]  # fmt: skip
    for a in plan.chain.own_addresses:
        argv += ["--own-address", a]
    for f in files:
        argv += ["--rows", str(f)]
    if build_report.build(argv) != 0:
        raise SystemExit("build_report failed")


def evaluate_gate(plan: RunPlan) -> list[str]:
    """Why this run must not be published (it is still recorded locally)."""
    rep = json.loads((plan.build_dir / "readiness.json").read_text())
    reasons = list(plan.gate_failures)
    attempted = int(rep.get("auctions_attempted") or 0)
    if not int(rep.get("bids") or 0):
        reasons.append("no bids were returned on any replayed auction")
    if attempted:
        bad = (int(rep.get("transport") or 0) + int(rep.get("deadline_miss") or 0)) * 100.0 / attempted
        if bad > plan.cfg.max_bad_share_pct:
            reasons.append(
                f"transport + deadline-miss share {bad:.1f} % of {attempted} attempted exceeds {plan.cfg.max_bad_share_pct:g} %"
            )
    if plan.engine_changed:
        reasons.append(f"the engine build changed during the run ({plan.engine_shas_text()})")
    if plan.partial_reason:
        reasons.append(f"partial run: {plan.partial_reason}")
    return reasons


def create_worktree(plan: RunPlan, shell: Shell) -> None:
    if plan.force:
        git(plan, shell, "worktree", "remove", "--force", str(plan.work_root))
        git(plan, shell, "branch", "-D", plan.branch)
    plan.work_root.parent.mkdir(parents=True, exist_ok=True)
    r = git(plan, shell, "worktree", "add", "-b", plan.branch, str(plan.work_root), plan.base_ref)
    if r.returncode != 0:
        raise SystemExit(f"git worktree add failed: {r.stderr.strip()}")


def remove_worktree(plan: RunPlan, shell: Shell, branch_too: bool) -> None:
    git(plan, shell, "worktree", "remove", "--force", str(plan.work_root))
    if branch_too:
        git(plan, shell, "branch", "-D", plan.branch)


def record(plan: RunPlan, files: list[Path]) -> None:
    import add_run
    import verify

    argv = [
        "--root", str(plan.work_root), "--chain", plan.chain.slug, "--run-id", plan.run_id,
        "--report", str(plan.build_dir / "report.md"), "--evidence-root", str(plan.cfg.evidence_root),
        "--figures", str(plan.build_dir / "figures.json"), "--meta", str(plan.build_dir / "meta.json"),
        "--engine-sha", plan.engine_sha, "--link", f"forum={plan.cfg.forum_link}", "--note", run_note(plan),
    ]  # fmt: skip
    for f in files:
        argv += ["--evidence", f"runs/{plan.chain.slug}/{f.name}:rows"]
    if plan.state_path.exists():
        argv += ["--evidence", f"runs/{plan.chain.slug}/{plan.state_path.name}:plan-state"]
    argv += ["--evidence", f"{plan.archive_rel.as_posix()}/manifest.jsonl:body-archive-manifest"]
    if plan.force:
        argv.append("--force")
    add_run.main(argv)
    # the record's own integrity over every run, then THIS run's evidence against the files on disk;
    # `verify --evidence-root` would also demand the earlier runs' private evidence under the same root
    if verify.main(["--root", str(plan.work_root)]) != 0:
        raise SystemExit("verify failed after recording; nothing was committed")
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
    exits = ", ".join(str(launch.exit_code) for launch in plan.launches) or "n/a (--skip-run)"
    extra = ""
    if plan.engine_changed:
        extra += f" The engine build changed during the run ({plan.engine_shas_text()}); this report names the first."
    if plan.cut_short:
        extra += " The cycle in flight at the stop was left out of the window."
    if plan.partial_reason:
        extra += f" Partial run: {plan.partial_reason}."
    if not plan.state_loaded and not plan.launches:
        extra += " No plan state was found, so whether the last cycle was cut short is unknown."
    return (
        f"Monthly run: watch from {iso(plan.start_ts)} for {plan.cfg.duration_s // 3600} h, "
        f"{len(plan.launches) or 'existing'} launch(es), exit code(s) {exits}; built by tools/run_month.py." + extra
    )


def commit_args(plan: RunPlan) -> list[str]:
    verdict = json.loads((plan.build_dir / "readiness.json").read_text())["verdict"]
    return [
        "-c", f"user.name={IDENTITY_NAME}", "-c", f"user.email={IDENTITY_EMAIL}", "-c", "commit.gpgsign=false",
        "commit", f"--author={IDENTITY_NAME} <{IDENTITY_EMAIL}>",
        "-m", f"readiness: {plan.chain.slug} {plan.run_id} ({verdict})",
        "-m", f"Run {plan.chain.slug}/{plan.run_id}, window as recorded in run.json, built by tools/run_month.py.",
    ]  # fmt: skip


def commit_run(plan: RunPlan, shell: Shell) -> None:
    paths = [f"runs/{plan.chain.slug}/{plan.run_id}", "index.json", "README.md", "SHA256SUMS"]
    add = git(plan, shell, "add", *paths, cwd=plan.work_root)
    if add.returncode != 0:
        raise SystemExit(f"git add failed: {add.stderr.strip()}")
    c = git(plan, shell, *commit_args(plan), cwd=plan.work_root)
    if c.returncode != 0:
        raise SystemExit(f"git commit failed: {c.stderr.strip()}")


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


def publish_env(plan: RunPlan, token: str) -> dict[str, str]:
    """git and gh authenticate with the repo-scoped token only, never the host's global login."""
    cred = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    gh_dir = plan.build_root / "gh-config"
    gh_dir.mkdir(parents=True, exist_ok=True)
    return {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": "credential.helper", "GIT_CONFIG_VALUE_0": "",
        "GIT_CONFIG_KEY_1": "http.extraheader", "GIT_CONFIG_VALUE_1": f"Authorization: basic {cred}",
        "GH_TOKEN": token, "GH_CONFIG_DIR": str(gh_dir), "GH_PROMPT_DISABLED": "1",
    }  # fmt: skip


def push_and_pr(plan: RunPlan, shell: Shell, pr_body: Path) -> None:
    token = os.environ.get(plan.cfg.publish_token_env, "")
    if not token:
        raise SystemExit(f"--publish needs the repo-scoped token in ${plan.cfg.publish_token_env}")
    env = publish_env(plan, token)
    title = f"readiness: {plan.chain.slug} {plan.run_id}"
    if shell.run(["gh", "auth", "status"], cwd=plan.work_root, timeout=60, env=env).returncode != 0:
        raise SystemExit("gh does not accept the token in the environment; nothing was pushed")
    push = shell.run(["git", "push", "-u", plan.cfg.remote, plan.branch], cwd=plan.work_root, timeout=300, env=env)
    if push.returncode != 0:
        raise SystemExit(f"git push failed ({push.returncode}): {redact(push.stderr.strip(), token)}")
    existing = shell.run(
        ["gh", "pr", "list", "--head", plan.branch, "--json", "url", "--jq", ".[0].url"],
        cwd=plan.work_root, timeout=60, env=env,
    )  # fmt: skip
    if existing.returncode == 0 and existing.stdout.strip():
        print(f"PR already open: {existing.stdout.strip()}")
        return
    create = ["gh", "pr", "create", "--title", title, "--body-file", str(pr_body), "--head", plan.branch]
    created = shell.run(create, cwd=plan.work_root, timeout=120, env=env)
    if created.returncode != 0:
        raise SystemExit(f"gh pr create failed ({created.returncode}): {redact(created.stderr.strip(), token)}")
    print(f"PR opened: {created.stdout.strip()}")


def manual_commands(plan: RunPlan, pr_body: Path) -> list[str]:
    title = f"readiness: {plan.chain.slug} {plan.run_id}"
    return [
        f"cd {shlex.quote(str(plan.work_root))}",
        f"git push -u {plan.cfg.remote} {plan.branch}",
        f"gh pr create --title {shlex.quote(title)} --body-file {shlex.quote(str(pr_body))} --head {plan.branch}",
    ]


# ------------------------------------------------------------------ CLI


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--chain", required=True)
    ap.add_argument("--config", type=Path, default=HERE / "monthly.json")
    ap.add_argument("--local-config", type=Path, default=None, help="host overlay (default: monthly.local.json next to --config)")
    ap.add_argument("--record-root", type=Path, default=HERE.parent)
    ap.add_argument(
        "--build-root", type=Path, default=None, help="scratch outputs and worktrees (default: <record-root>/build, git-ignored)"
    )
    ap.add_argument("--evidence-root", type=Path, default=None, help="override the configured evidence root")
    ap.add_argument(
        "--start",
        default=None,
        help="ISO start with a timezone, e.g. 2026-10-01T00:00:00Z (default: now); the run id is its UTC date",
    )
    ap.add_argument("--run-id", default=None, help="override the run id (YYYY-MM-DD[suffix])")
    ap.add_argument("--duration", default=None, help="override the configured duration, e.g. 20h")
    ap.add_argument("--engine-sha", default=None)
    ap.add_argument(
        "--skip-run", action="store_true", help="build from the evidence of --run-id already on disk (reads its plan state)"
    )
    ap.add_argument("--dry-run", action="store_true", help="replay and build only; no worktree, no commit, no push")
    ap.add_argument(
        "--publish", action="store_true", help="push the branch and open the pull request (needs the repo-scoped token)"
    )
    ap.add_argument("--force", action="store_true", help="re-record an existing run id / ignore tools/ drift / replace leftovers")
    ap.add_argument("--allow-root", action="store_true", help="allow engine_sha_cmd to run as root")
    ap.add_argument("--stop-resend-seconds", type=float, default=30.0, help="how often the stop signal is re-sent to the tool")
    ap.add_argument(
        "--wait-lock", type=float, default=0.0, help="seconds to wait for another run to finish (default: fail at once)"
    )
    return ap


def parse_start(text: str) -> float:
    try:
        dt = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError as e:
        raise SystemExit(f"bad --start {text!r}: {e}") from e
    if dt.tzinfo is None:
        raise SystemExit(f"--start {text!r} has no timezone; write it as 2026-10-01T00:00:00Z")
    return dt.timestamp()


def make_plan(a: argparse.Namespace, shell: Shell) -> RunPlan:
    overlay = a.local_config or a.config.with_name(a.config.stem + ".local" + a.config.suffix)
    cfg = Config.load(a.config, overlay)
    if a.evidence_root or a.duration:
        cfg = Config(**{
            **cfg.__dict__,
            "evidence_root": a.evidence_root or cfg.evidence_root,
            "duration_s": parse_duration(a.duration) if a.duration else cfg.duration_s,
        })  # fmt: skip
    if a.chain not in cfg.chains:
        raise SystemExit(f"chain {a.chain!r} is not in {a.config}; known: {sorted(cfg.chains)}")
    chain = cfg.chains[a.chain]
    start_ts = parse_start(a.start) if a.start else shell.now()
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
        engine_override=a.engine_sha,
        skip_run=a.skip_run,
        publish=a.publish,
        allow_root=a.allow_root,
        rpc_url=os.environ.get(chain.rpc_env, ""),
        dry_run=a.dry_run,
        force=a.force,
    )


def main(argv: list[str] | None = None, shell: Shell | None = None) -> int:
    a = build_parser().parse_args(argv)
    shell = shell or RealShell(resend_s=a.stop_resend_seconds)
    plan = make_plan(a, shell)
    plan.cfg.evidence_root.mkdir(parents=True, exist_ok=True)
    with run_lock(plan.lock_path, a.wait_lock):
        return run(plan, shell)


def settle_engine_sha(plan: RunPlan, shell: Shell) -> None:
    if plan.skip_run and plan.engine_shas and not plan.engine_override:
        plan.engine_sha = next((s for s in plan.engine_shas if s), "")  # the build the run was made against
        return
    plan.engine_sha = resolve_engine_sha(plan.chain, plan.engine_override, shell, plan.allow_root)
    plan.engine_shas = [plan.engine_sha]


def run(plan: RunPlan, shell: Shell) -> int:
    if plan.skip_run:
        load_state(plan)
    missing = missing_host_settings(plan)
    if missing:
        msg = "host settings missing (see tools/monthly.local.example.json):\n  " + "\n  ".join(missing)
        if plan.dry_run:
            print("dry run: " + msg + "\nnothing was run")
            return 0
        raise SystemExit(msg)
    preflight(plan, shell)
    settle_engine_sha(plan, shell)
    if not plan.skip_run:
        probe_replay_target(plan, shell)
        run_watch(plan, shell)
    files = evidence_files_for(plan)
    w = resolve_window(plan, files)
    build(plan, files, w)
    reasons = evaluate_gate(plan)
    if plan.dry_run:
        print(json.dumps({"stage": "dry-run done", "build": str(plan.build_dir), "publish_refused": reasons}))
        return EXIT_GATE if reasons else 0
    create_worktree(plan, shell)
    try:
        record(plan, files)
        commit_run(plan, shell)
    except BaseException:
        remove_worktree(plan, shell, branch_too=True)
        raise
    pr_body = write_pr_body(plan)
    done = {"stage": "done", "run": f"{plan.chain.slug}/{plan.run_id}", "branch": plan.branch, "worktree": str(plan.work_root)}
    if reasons:
        print("publish refused (the run is recorded and committed locally, nothing was pushed):\n  " + "\n  ".join(reasons))
        print(json.dumps({**done, "published": False, "refused": reasons}))
        return EXIT_GATE
    if not plan.publish:
        print(
            "recorded and committed locally; to publish, review it and run:\n  " + "\n  ".join(manual_commands(plan, pr_body))
            + "\nor re-run with --publish. Authenticate with a token limited to this repository, not the host's gh login."
        )  # fmt: skip
        print(json.dumps({**done, "published": False}))
        return 0
    push_and_pr(plan, shell, pr_body)
    remove_worktree(plan, shell, branch_too=False)
    print(json.dumps({**done, "published": True}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
