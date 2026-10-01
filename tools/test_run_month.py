"""run_month.py against a fake Shell: no process is spawned, no git or gh is touched. The fake
"watch" copies fixture files into place with scripted exit codes. A few tests drive the real
`RealShell.watch` against small child scripts (signals, escalation, cleanup). Needs cow_backtester
for the build stage (skipped without it)."""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import signal
import sys
import textwrap
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

bt = pytest.importorskip("cow_backtester.backtest")

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import run_month  # noqa: E402

FIX = HERE / "fixtures" / "monthly"
RUNS = FIX / "runs" / "arbitrum-one"
FILE1, FILE2 = RUNS / "fixture-a.jsonl", RUNS / "fixture-b.jsonl"
PARAMS = json.loads((FIX / "fixture.json").read_text())
T0 = 1_790_812_800.0  # 2026-10-01T00:00:00Z
GIB = 2**30
TRAILER_WORDS = ("co-authored", "generated", "assistant")  # nothing like a trailer or tool credit in a commit
SECRET = "SECRETKEY123"
TOKEN = "ghp_REPOSCOPEDTOKEN"
READS = (
    ["git", "status"], ["git", "rev-parse"], ["git", "fetch"], ["git", "diff"], ["git", "ls-tree"],
    ["git", "remote"], ["gh", "auth"], ["gh", "pr", "list"],
)  # fmt: skip


def metas(path: Path) -> list[dict[str, Any]]:
    return [json.loads(ln)["_meta"] for ln in path.read_text().splitlines() if '"_meta"' in ln]


@dataclass
class Step:
    """One scripted launch: the file the tool leaves, its exit code, and whether the runner stopped it."""

    src: Path | None
    rc: int
    signalled: bool = False
    stop_reason: str | None = None
    log: str = ""
    low_disk: bool = False  # the disk fills while this launch runs: should_stop reports it


@dataclass
class FakeShell:
    script: list[Step]
    template: Path
    pr_exists: bool = False
    gh_authenticated: bool = True
    probe_status: int | None = 200
    head: int | None = None
    free: int = 500 * GIB
    clock: float = T0
    engine_shas: list[str] = field(default_factory=lambda: ["abc1234"])
    tools_differ: bool = False
    leftover_branch: bool = False
    existing_in_main: str = ""
    calls: list[list[str]] = field(default_factory=list)
    envs: list[dict[str, str] | None] = field(default_factory=list)
    watches: list[list[str]] = field(default_factory=list)
    logs: list[Path] = field(default_factory=list)
    slept: list[float] = field(default_factory=list)
    probes: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    block_labels: list[str] = field(default_factory=list)

    def run(
        self, cmd: list[str], cwd: Path | None = None, timeout: float | None = None, env: dict[str, str] | None = None
    ) -> run_month.Completed:
        self.calls.append(cmd)
        self.envs.append(env)
        if cmd[:2] == ["cow-backtester", "--version"]:
            return run_month.Completed(0, f"cow-backtester {bt.VERSION}\n")
        if cmd[:2] == ["git", "diff"]:
            return run_month.Completed(1 if self.tools_differ else 0)
        if cmd[:2] == ["git", "ls-tree"]:
            return run_month.Completed(0, self.existing_in_main)
        if cmd[:3] == ["git", "rev-parse", "--verify"]:
            return run_month.Completed(0 if self.leftover_branch else 1)
        if cmd[:3] == ["git", "worktree", "add"]:
            shutil.copytree(self.template, Path(cmd[cmd.index("-b") + 2]))
            return run_month.Completed(0)
        if cmd[:3] == ["git", "worktree", "remove"]:
            shutil.rmtree(cmd[-1], ignore_errors=True)
            return run_month.Completed(0)
        if cmd[:3] == ["gh", "auth", "status"]:
            return run_month.Completed(0 if self.gh_authenticated else 1)
        if cmd[:3] == ["gh", "pr", "list"]:
            return run_month.Completed(0, "https://github.com/x/y/pull/7\n" if self.pr_exists else "")
        if cmd[:3] == ["gh", "pr", "create"]:
            return run_month.Completed(0, "https://github.com/x/y/pull/8\n")
        if cmd[0] == "engine-sha":
            n = sum(1 for c in self.calls if c[0] == "engine-sha")
            return run_month.Completed(0, self.engine_shas[min(n - 1, len(self.engine_shas) - 1)] + "\n")
        return run_month.Completed(0, "")

    def watch(
        self, cmd: list[str], run_s: float, grace_s: int, log_path: Path, should_stop: Callable[[], str | None] | None = None
    ) -> run_month.WatchResult:
        self.watches.append(cmd)
        self.logs.append(log_path)
        step = self.script[len(self.watches) - 1]
        out = Path(cmd[cmd.index("--json-out") + 1])
        out.parent.mkdir(parents=True, exist_ok=True)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(step.log)
        if step.src is not None:
            shutil.copyfile(step.src, out)
            archive = Path(cmd[cmd.index("--archive-bodies") + 1])  # the tool writes its manifest there
            archive.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(FIX / "archive" / "bodies-0.11" / "manifest.jsonl", archive / "manifest.jsonl")
        else:
            out.write_bytes(b"")
        self.clock += 3600
        if step.low_disk:
            self.free = GIB
            reason = should_stop() if should_stop else None
            if reason:
                return run_month.WatchResult(0, True, reason, "sigint")
        return run_month.WatchResult(step.rc, step.signalled, step.stop_reason, "sigint" if step.signalled else None)

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.clock += seconds

    def now(self) -> float:
        return self.clock

    def monotonic(self) -> float:
        return self.clock

    def block_number(self, rpc_url: str, label: str) -> int | None:
        self.block_labels.append(label)
        return self.head

    def post_json(self, url: str, body: dict[str, Any], timeout: float) -> tuple[int | None, str]:
        self.probes.append((url, body))
        return self.probe_status, "" if self.probe_status else "URLError"

    def free_bytes(self, path: Path) -> int:
        return self.free

    def writes(self) -> list[list[str]]:
        """Everything that is not a read: what changed the repository or GitHub."""
        return [c for c in self.calls if c[0] in ("git", "gh") and not any(c[: len(r)] == r for r in READS)]


@pytest.fixture(autouse=True)
def not_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run_month, "running_as_root", lambda: False)


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    template = tmp_path / "template"  # what `git worktree add` would check out of origin/main
    (template / "tools").mkdir(parents=True)
    (template / "README.md").write_text("# t\n\n<!-- runs:start -->\n<!-- runs:end -->\n")
    record = tmp_path / "record"
    record.mkdir()
    evidence = tmp_path / "evidence"
    legacy = evidence / "archive" / f"bodies-{run_month.tool_minor_version()}"
    legacy.mkdir(parents=True)
    shutil.copyfile(FIX / "archive" / "bodies-0.11" / "manifest.jsonl", legacy / "manifest.jsonl")
    config = tmp_path / "monthly.json"
    config.write_text(
        json.dumps(
            {
                "evidence_root": str(evidence),
                "forum_link": "https://forum.cow.fi/t/3572",
                "duration": "20h",
                "grace_seconds": 5,
                "max_restarts": 2,
                "restart_backoff_seconds": 60,
                "max_bad_share_pct": 20,
                "chains": {
                    "arbitrum-one": {
                        "solver_url": None,
                        "own_addresses": PARAMS["own_addresses"],
                        "rpc_env": "TEST_RPC",
                        "engine_sha_cmd": None,
                        "watch_seconds": 60,
                        "blocks": 2000,
                    }
                },
            }
        )
    )
    overlay = tmp_path / "monthly.local.json"
    overlay.write_text(
        json.dumps({"chains": {"arbitrum-one": {"solver_url": PARAMS["solver_url"], "engine_sha_cmd": ["engine-sha"]}}})
    )
    monkeypatch.setenv("TEST_RPC", f"http://rpc.test/{SECRET}")
    monkeypatch.setenv("GH_TOKEN", TOKEN)
    return {
        "template": template, "record": record, "evidence": evidence, "config": config, "overlay": overlay,
        "build": tmp_path / "build",
    }  # fmt: skip


def argv(w: dict[str, Path], *extra: str) -> list[str]:
    return [
        "--chain", "arbitrum-one", "--config", str(w["config"]), "--record-root", str(w["record"]),
        "--build-root", str(w["build"]), "--start", "2026-10-01T00:00:00Z", *extra,
    ]  # fmt: skip


def shell_for(w: dict[str, Path], script: list[Step], **kw: Any) -> FakeShell:
    return FakeShell(script=script, template=w["template"], **kw)


def build_dir(w: dict[str, Path]) -> Path:
    return w["build"] / "arbitrum-one" / "2026-10-01"


def worktree(w: dict[str, Path]) -> Path:
    return w["build"] / "worktrees" / "arbitrum-one-2026-10-01"


def state_file(w: dict[str, Path], run_id: str = "2026-10-01") -> Path:
    return w["evidence"] / "runs" / "arbitrum-one" / f"{run_id}.plan.json"


def run_json(w: dict[str, Path]) -> dict[str, Any]:
    return json.loads((worktree(w) / "runs" / "arbitrum-one" / "2026-10-01" / "run.json").read_text())


def flag(cmd: list[str], name: str) -> str:
    return cmd[cmd.index(name) + 1]


def pushed(shell: FakeShell) -> bool:
    return any(c[:2] == ["git", "push"] for c in shell.calls)


# ------------------------------------------------------------------ the whole run


def test_full_run_with_restart_and_cut_short_last_cycle(world: dict[str, Path], capsys: pytest.CaptureFixture[str]) -> None:
    """Launch 1 dies (exit 1) after writing file 1; launch 2 is interrupted (130). The second launch
    scans from where the first stopped, with slack; the window drops the cut-short cycle; the record
    is committed locally in a worktree as kaisersolver and nothing is pushed."""
    last_to_file1 = max(m["to_block"] for m in metas(FILE1))
    shell = shell_for(world, [Step(FILE1, 1), Step(FILE2, 130)], head=last_to_file1 + 500)
    assert run_month.main(argv(world), shell=shell) == 0
    assert len(shell.watches) == 2 and shell.slept == [60]
    assert flag(shell.watches[0], "--blocks") == "2000"
    assert flag(shell.watches[1], "--blocks") == str(500 + run_month.RELAUNCH_SLACK_BLOCKS)  # overlap, never a gap
    assert shell.block_labels == ["TEST_RPC"]  # the env var name is what gets logged, not the URL
    assert "--no-cache" in shell.watches[0] and "--cache-dir" not in shell.watches[0] and "--quiet" not in shell.watches[0]
    assert flag(shell.watches[0], "--solver-url") == PARAMS["solver_url"]
    assert Path(flag(shell.watches[0], "--archive-bodies")).name == "2026-10-01-arbitrum-one"
    run = run_json(world)
    window = json.loads((build_dir(world) / "window.json").read_text())
    file2_metas = metas(FILE2)
    assert window["last_cycle_dropped"] is True and window["to_block"] == file2_metas[0]["to_block"]
    assert run["window"]["to_block"] == file2_metas[0]["to_block"]
    assert "exit code(s) 1, 130" in run["notes"][0] and "left out of the window" in run["notes"][0]
    assert [e["kind"] for e in run["evidence"]] == ["rows", "rows", "plan-state", "body-archive-manifest"]
    commit = next(c for c in shell.calls if "commit" in c)
    assert commit[:6] == ["git", "-c", "user.name=kaisersolver", "-c", "user.email=kaisersolver@users.noreply.github.com", "-c"]
    assert "--author=kaisersolver <kaisersolver@users.noreply.github.com>" in commit
    assert not any(w in " ".join(commit).lower() for w in TRAILER_WORDS)
    assert not any(c[:2] in (["git", "push"], ["git", "switch"], ["git", "checkout"]) for c in shell.calls)
    assert not any(c[:3] == ["gh", "pr", "create"] for c in shell.calls)
    assert ["git", "fetch", "origin", "main"] in shell.calls
    out = capsys.readouterr().out
    assert "recorded and committed locally" in out and "git push -u origin readiness/arbitrum-one-2026-10-01" in out
    assert "--publish" in out


def test_the_report_does_not_name_the_replay_target(world: dict[str, Path]) -> None:
    shell = shell_for(world, [Step(FILE1, 130)])
    assert run_month.main(argv(world), shell=shell) == 0
    report = (worktree(world) / "runs" / "arbitrum-one" / "2026-10-01" / "report.md").read_text()
    assert PARAMS["solver_url"] not in report and "127.0.0.1" not in report and run_month.REPORT_SOLVER_URL in report


def test_dry_run_replays_and_builds_but_touches_no_git_state(world: dict[str, Path], capsys: pytest.CaptureFixture[str]) -> None:
    shell = shell_for(world, [Step(FILE1, 130)])
    assert run_month.main(argv(world, "--dry-run"), shell=shell) == 0
    assert shell.writes() == []  # no worktree, no add, no commit, no branch
    assert (build_dir(world) / "report.md").exists() and not worktree(world).exists()
    assert "dry-run done" in capsys.readouterr().out


def test_failed_record_removes_the_worktree_and_branch(world: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(plan: run_month.RunPlan, files: list[Path]) -> None:
        raise SystemExit("verify failed")

    monkeypatch.setattr(run_month, "record", boom)
    shell = shell_for(world, [Step(FILE1, 130)])
    with pytest.raises(SystemExit, match="verify failed"):
        run_month.main(argv(world), shell=shell)
    cmds = [c[:3] for c in shell.calls]
    assert ["git", "worktree", "remove"] in cmds and ["git", "branch", "-D"] in cmds
    assert not worktree(world).exists()
    assert not any(c[:2] in (["git", "switch"], ["git", "checkout"]) for c in shell.calls)  # the main checkout is never moved


def test_existing_run_in_main_is_refused_without_force(world: dict[str, Path]) -> None:
    shell = shell_for(world, [], existing_in_main="runs/arbitrum-one/2026-10-01/run.json\n")
    with pytest.raises(SystemExit, match="already in origin/main"):
        run_month.main(argv(world), shell=shell)
    assert shell.watches == []


def test_tools_drift_and_leftover_branch_are_refused(world: dict[str, Path]) -> None:
    with pytest.raises(SystemExit, match=r"tools/ .* differs from origin/main"):
        run_month.main(argv(world), shell=shell_for(world, [], tools_differ=True))
    with pytest.raises(SystemExit, match="left over"):
        run_month.main(argv(world), shell=shell_for(world, [], leftover_branch=True))


def test_skip_run_without_state_rebuilds_from_the_day_glob(world: dict[str, Path], capsys: pytest.CaptureFixture[str]) -> None:
    dst = world["evidence"] / "runs" / "arbitrum-one"
    dst.mkdir(parents=True)
    shutil.copyfile(FILE1, dst / "2026-10-01T000000Z.jsonl")
    shutil.copyfile(FILE2, dst / "2026-10-01T090000Z.jsonl")
    shell = shell_for(world, [])
    assert run_month.main(argv(world, "--skip-run", "--run-id", "2026-10-01"), shell=shell) == 0
    assert shell.watches == [] and shell.probes == []
    run = run_json(world)
    assert run["window"] == {"from_block": PARAMS["window_full"][0], "to_block": PARAMS["window_full"][1]}
    assert run["evidence"][-1]["path"] == f"archive/bodies-{run_month.tool_minor_version()}/manifest.jsonl"  # legacy layout
    assert "watch from 2026-10-01T00:00:00Z" in run["notes"][0] and "unknown" in run["notes"][0]
    assert "no 2026-10-01.plan.json" in capsys.readouterr().err


# ------------------------------------------------------------------ F2: stopping and plan state


def test_a_stop_we_sent_is_clean_whatever_the_exit_code_and_skip_run_remembers_it(world: dict[str, Path]) -> None:
    """rc -15 after our SIGINT/SIGTERM used to abort the run with nothing built. Now it is a stop:
    the in-flight cycle is dropped, and --skip-run reads that back from the plan state."""
    shell = shell_for(world, [Step(FILE2, -15, signalled=True, stop_reason="deadline")])
    assert run_month.main(argv(world, "--dry-run"), shell=shell) == 0
    window = json.loads((build_dir(world) / "window.json").read_text())
    assert window["last_cycle_dropped"] is True and window["to_block"] == metas(FILE2)[0]["to_block"]
    state = json.loads(state_file(world).read_text())
    launch = state["launches"][0]
    assert state["cut_short"] is True and launch["signalled"] is True and launch["exit_code"] == -15
    assert "/" not in launch["file"]  # the state names files, not host paths
    again = shell_for(world, [])
    assert run_month.main(argv(world, "--skip-run", "--dry-run", "--run-id", "2026-10-01"), shell=again) == 0
    assert json.loads((build_dir(world) / "window.json").read_text()) == window  # same window as the live run


def test_a_run_whose_only_cycle_was_cut_short_has_no_window(world: dict[str, Path]) -> None:
    only = world["evidence"].parent / "one.jsonl"
    lines = FILE1.read_text().splitlines()
    first_meta = next(i for i, ln in enumerate(lines) if '"_meta"' in ln)
    only.write_text("\n".join(lines[: first_meta + 1]) + "\n")
    shell = shell_for(world, [Step(only, 130, signalled=True, stop_reason="deadline")])
    with pytest.raises(SystemExit, match="no complete cycle"):
        run_month.main(argv(world), shell=shell)
    assert state_file(world).exists()  # evidence and state are kept


def test_runner_signal_persists_state_and_exits_130(world: dict[str, Path]) -> None:
    shell = shell_for(world, [Step(FILE1, 130, signalled=True, stop_reason="runner signal")])
    with pytest.raises(SystemExit) as e:
        run_month.main(argv(world), shell=shell)
    assert e.value.code == 130 and state_file(world).exists()


def test_naive_start_is_rejected(world: dict[str, Path]) -> None:
    bad = [a if a != "2026-10-01T00:00:00Z" else "2026-10-01T00:00:00" for a in argv(world)]
    with pytest.raises(SystemExit, match="no timezone"):
        run_month.main(bad, shell=shell_for(world, []))
    assert run_month.parse_start("2026-10-01T03:00:00+03:00") == T0


# ------------------------------------------------------------------ F8 / F10: restarts and gaps


def test_restart_budget_counts_consecutive_failures_and_resets_after_progress(world: dict[str, Path]) -> None:
    """max_restarts is 2: four crashes that each wrote a cycle would have aborted the old cumulative budget."""
    shell = shell_for(world, [Step(FILE1, 1)] * 4 + [Step(FILE2, 130)])
    assert run_month.main(argv(world, "--dry-run"), shell=shell) == 0
    assert len(shell.watches) == 5 and shell.slept == [60] * 4


def test_giving_up_after_progress_builds_a_partial_run_and_refuses_to_publish(
    world: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    shell = shell_for(world, [Step(FILE1, 1), Step(None, 1), Step(None, 1), Step(None, 1)])
    assert run_month.main(argv(world, "--publish"), shell=shell) == run_month.EXIT_GATE
    assert len(shell.watches) == 4
    assert "Partial run" in run_json(world)["notes"][0]
    assert not pushed(shell)
    assert "partial run" in capsys.readouterr().out


def test_tool_that_never_produces_rows_gives_up_after_max_restarts(world: dict[str, Path]) -> None:
    shell = shell_for(world, [Step(None, 1), Step(None, 1), Step(None, 1)])
    with pytest.raises(SystemExit, match="failed 3 time"):
        run_month.main(argv(world), shell=shell)
    assert len(shell.watches) == 3 and shell.slept == [60, 60]
    assert shell.writes() == []


def test_usage_error_aborts_immediately(world: dict[str, Path]) -> None:
    shell = shell_for(world, [Step(None, 2)])
    with pytest.raises(SystemExit, match="exit 2"):
        run_month.main(argv(world), shell=shell)
    assert len(shell.watches) == 1


def test_find_gaps_between_launches() -> None:
    def m(a: int, b: int) -> dict[str, int]:
        return {"from_block": a, "to_block": b}

    assert run_month.find_gaps([[m(1, 10), m(11, 20)], [m(15, 30)]], False) == []  # overlap is not a gap
    assert run_month.find_gaps([[m(1, 10)], [m(15, 30)]], False) == [(11, 14)]
    assert run_month.find_gaps([[m(1, 10)], [m(15, 30), m(31, 40)]], True) == [(11, 14)]
    assert run_month.find_gaps([[m(1, 10)], [m(15, 30)]], True) == []  # the dropped cycle is not counted


def test_a_gap_between_launches_blocks_publishing(world: dict[str, Path], tmp_path: Path) -> None:
    """The head cannot be read, so the relaunch asks for the default and may leave a hole."""
    late = tmp_path / "late.jsonl"
    shift = max(m["to_block"] for m in metas(FILE1)) + 5000 - metas(FILE2)[0]["from_block"]
    out = []
    for ln in FILE2.read_text().splitlines():
        if '"_meta"' in ln:
            d = json.loads(ln)
            d["_meta"]["from_block"] += shift
            d["_meta"]["to_block"] += shift
            ln = json.dumps(d)
        out.append(ln)
    late.write_text("\n".join(out) + "\n")
    shell = shell_for(world, [Step(FILE1, 1), Step(late, 130)], head=None)
    assert run_month.main(argv(world, "--publish"), shell=shell) == run_month.EXIT_GATE
    assert not pushed(shell)


# ------------------------------------------------------------------ F3: probe and gate


def test_probe_must_get_http_200_before_the_long_run(world: dict[str, Path]) -> None:
    shell = shell_for(world, [Step(FILE1, 130)], probe_status=403)
    with pytest.raises(SystemExit, match="probe failed.*HTTP 403"):
        run_month.main(argv(world), shell=shell)
    assert shell.watches == [] and shell.probes[0][0] == PARAMS["solver_url"] + "/solve"
    assert shell.probes[0][1]["orders"] == [] and "deadline" in shell.probes[0][1]
    dead = shell_for(world, [Step(FILE1, 130)], probe_status=None)
    with pytest.raises(SystemExit, match="no response"):
        run_month.main(argv(world), shell=dead)


def write_readiness(plan_build: Path, **kw: Any) -> None:
    plan_build.mkdir(parents=True, exist_ok=True)
    rep = {"auctions_attempted": 100, "bids": 90, "transport": 0, "deadline_miss": 0, **kw}
    (plan_build / "readiness.json").write_text(json.dumps(rep))


def plan_for(world: dict[str, Path]) -> run_month.RunPlan:
    args = run_month.build_parser().parse_args(argv(world))
    plan = run_month.make_plan(args, shell_for(world, []))
    plan.engine_shas = ["abc1234"]
    return plan


def test_gate_refuses_zero_bids_and_a_high_transport_or_deadline_share(world: dict[str, Path]) -> None:
    plan = plan_for(world)
    write_readiness(plan.build_dir)
    assert run_month.evaluate_gate(plan) == []
    write_readiness(plan.build_dir, bids=0)
    assert any("no bids" in r for r in run_month.evaluate_gate(plan))
    write_readiness(plan.build_dir, transport=10, deadline_miss=11)
    assert any("21.0 %" in r and "exceeds 20 %" in r for r in run_month.evaluate_gate(plan))
    write_readiness(plan.build_dir, auctions_attempted=0, bids=0)
    assert run_month.evaluate_gate(plan)
    write_readiness(plan.build_dir)
    plan.engine_shas = ["abc1234", "def5678"]
    assert any("engine build changed" in r for r in run_month.evaluate_gate(plan))


def test_a_failing_gate_still_records_locally_but_never_pushes(
    world: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = json.loads(world["config"].read_text())
    cfg["max_bad_share_pct"] = 1  # the fixture has 3 of 34 transport or deadline misses
    world["config"].write_text(json.dumps(cfg))
    shell = shell_for(world, [Step(FILE1, 130)])
    assert run_month.main(argv(world, "--publish"), shell=shell) == run_month.EXIT_GATE
    assert any(c[:2] == ["git", "add"] for c in shell.calls) and any("commit" in c for c in shell.calls)
    assert not pushed(shell) and not any(c[:2] == ["gh", "pr"] for c in shell.calls)
    assert "publish refused" in capsys.readouterr().out


# ------------------------------------------------------------------ F5: disk, cache, archive errors


def test_disk_floor_is_checked_before_starting(world: dict[str, Path]) -> None:
    shell = shell_for(world, [Step(FILE1, 130)], free=10 * GIB)
    with pytest.raises(SystemExit, match=r"only 10\.0 GiB free .* floor is 30 GiB"):
        run_month.main(argv(world), shell=shell)
    assert shell.watches == []


def test_disk_floor_is_rechecked_between_launches(world: dict[str, Path]) -> None:
    shell = shell_for(world, [Step(FILE1, 1), Step(None, 1)])
    orig_watch = shell.watch

    def watch(*a: Any, **k: Any) -> run_month.WatchResult:
        r = orig_watch(*a, **k)
        shell.free = 5 * GIB  # the disk filled while launch 1 ran, and it crashed
        return r

    shell.watch = watch  # type: ignore[method-assign]
    assert run_month.main(argv(world), shell=shell) == run_month.EXIT_GATE
    assert len(shell.watches) == 1  # no relaunch onto a full disk
    assert "free disk fell below 30 GiB" in run_json(world)["notes"][0]


def test_disk_floor_is_watched_during_a_launch(world: dict[str, Path]) -> None:
    shell = shell_for(world, [Step(FILE1, 0, low_disk=True)])
    assert run_month.main(argv(world), shell=shell) == run_month.EXIT_GATE  # should_stop fired inside the launch
    state = json.loads(state_file(world).read_text())
    assert state["launches"][0]["stop_reason"] == "free disk below the floor" and "stopped early" in state["partial_reason"]


def test_archive_write_errors_fail_the_run(world: dict[str, Path]) -> None:
    log = "      ⚠ --archive-bodies write failed: [Errno 28] No space left on device\n  ⚠ archive writes failed: 3 (see stderr)\n"
    shell = shell_for(world, [Step(FILE1, 130, log=log)])
    with pytest.raises(SystemExit, match="2 archive write error"):
        run_month.main(argv(world), shell=shell)
    assert shell.writes() == []  # nothing was built or recorded


def test_cache_is_off_unless_a_directory_is_configured(world: dict[str, Path]) -> None:
    cfg = json.loads(world["config"].read_text())
    cfg["cache"] = str(world["evidence"].parent / "cache-elsewhere")
    world["config"].write_text(json.dumps(cfg))
    shell = shell_for(world, [Step(FILE1, 130)])
    run_month.main(argv(world, "--dry-run"), shell=shell)
    assert flag(shell.watches[0], "--cache-dir") == cfg["cache"] and "--no-cache" not in shell.watches[0]


def test_logs_of_every_launch_are_kept(world: dict[str, Path]) -> None:
    shell = shell_for(world, [Step(FILE1, 130, log="progress\n")])
    run_month.main(argv(world, "--dry-run"), shell=shell)
    assert shell.logs[0].parent == world["evidence"] / "logs" / "arbitrum-one" and shell.logs[0].read_text() == "progress\n"


# ------------------------------------------------------------------ F6 / F7: publishing


def test_publish_pushes_and_opens_a_pr_with_the_repo_scoped_token(
    world: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    shell = shell_for(world, [Step(FILE1, 130)])
    assert run_month.main(argv(world, "--publish"), shell=shell) == 0
    pushes = [e for c, e in zip(shell.calls, shell.envs, strict=True) if c[:2] == ["git", "push"]]
    assert len(pushes) == 1
    env = pushes[0]
    assert env is not None and env["GH_TOKEN"] == TOKEN and env["GIT_CONFIG_GLOBAL"] == os.devnull
    assert env["GIT_CONFIG_KEY_0"] == "credential.helper" and env["GIT_CONFIG_VALUE_0"] == ""
    assert env["GIT_CONFIG_KEY_1"] == "http.extraheader" and "basic" in env["GIT_CONFIG_VALUE_1"]
    assert env["GH_CONFIG_DIR"].startswith(str(world["build"]))  # not the host's gh login
    assert all(TOKEN not in " ".join(c) for c in shell.calls)  # never on a command line
    order = [c[:3] for c in shell.calls if c[0] == "gh" or c[:2] == ["git", "push"]]
    assert order[0] == ["gh", "auth", "status"] and order[1][:2] == ["git", "push"]  # auth is checked before the push
    assert any(c[:3] == ["gh", "pr", "create"] for c in shell.calls)
    assert "PR opened: https://github.com/x/y/pull/8" in capsys.readouterr().out
    assert not worktree(world).exists()  # a published run's worktree is removed


def test_publish_reuses_an_open_pr(world: dict[str, Path], capsys: pytest.CaptureFixture[str]) -> None:
    shell = shell_for(world, [Step(FILE1, 130)], pr_exists=True)
    assert run_month.main(argv(world, "--publish"), shell=shell) == 0
    assert "PR already open: https://github.com/x/y/pull/7" in capsys.readouterr().out
    assert not any(c[:3] == ["gh", "pr", "create"] for c in shell.calls)


def test_publish_without_gh_auth_stops_before_the_push(world: dict[str, Path]) -> None:
    shell = shell_for(world, [Step(FILE1, 130)], gh_authenticated=False)
    with pytest.raises(SystemExit, match="nothing was pushed"):
        run_month.main(argv(world, "--publish"), shell=shell)
    assert not pushed(shell)


def test_publish_needs_the_token_before_any_long_run(world: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GH_TOKEN")
    shell = shell_for(world, [Step(FILE1, 130)])
    with pytest.raises(SystemExit, match="GH_TOKEN"):
        run_month.main(argv(world, "--publish"), shell=shell)
    assert shell.watches == []
    cfg = json.loads(world["config"].read_text())
    cfg["publish_token_env"] = "READINESS_PUBLISH_TOKEN"  # the variable is named in config
    world["config"].write_text(json.dumps(cfg))
    monkeypatch.setenv("READINESS_PUBLISH_TOKEN", TOKEN)
    assert run_month.main(argv(world, "--publish"), shell=shell_for(world, [Step(FILE1, 130)])) == 0


def test_without_publish_nothing_leaves_the_host(world: dict[str, Path]) -> None:
    shell = shell_for(world, [Step(FILE1, 130)])
    assert run_month.main(argv(world), shell=shell) == 0
    assert not pushed(shell) and not any(c[0] == "gh" for c in shell.calls)
    assert worktree(world).exists()  # kept for review: the printed commands run from there


# ------------------------------------------------------------------ F15 / F17: engine sha


def test_engine_sha_is_read_per_launch_and_a_change_blocks_publishing(world: dict[str, Path]) -> None:
    shell = shell_for(world, [Step(FILE1, 1), Step(FILE2, 130)], engine_shas=["abc1234", "abc1234", "def5678"], head=None)
    assert run_month.main(argv(world, "--publish"), shell=shell) == run_month.EXIT_GATE
    assert sum(1 for c in shell.calls if c[0] == "engine-sha") == 3  # preflight, relaunch, end of run
    state = json.loads(state_file(world).read_text())
    assert [launch["engine_sha"] for launch in state["launches"]] == ["abc1234", "abc1234"]
    assert state["engine_shas"][-1] == "def5678"
    note = run_json(world)["notes"][0]
    assert "engine build changed" in note and "abc1234, def5678" in note
    assert run_json(world)["engine"] == {"build_sha": "abc1234"}  # the first is what the report names
    assert not pushed(shell)


def test_engine_sha_cmd_is_an_argv_run_without_a_shell(world: dict[str, Path]) -> None:
    shell = shell_for(world, [Step(FILE1, 130)])
    run_month.main(argv(world, "--dry-run"), shell=shell)
    assert ["engine-sha"] in shell.calls and not any(c[:2] == ["sh", "-c"] for c in shell.calls)
    chain = run_month.Config.load(world["config"], world["overlay"]).chains["arbitrum-one"]
    assert chain.engine_sha_cmd == ["engine-sha"]
    assert run_month.normalise_field("engine_sha_cmd", "print-sha --flag 'a b'") == ["print-sha", "--flag", "a b"]
    with pytest.raises(SystemExit, match="not 7-40 hex"):
        run_month.resolve_engine_sha(chain, "nope", shell)
    assert run_month.parse_duration("20h") == 72000 and run_month.parse_duration("90m") == 5400
    with pytest.raises(SystemExit, match="bad duration"):
        run_month.parse_duration("2 days")


def test_engine_sha_cmd_refused_as_root_and_in_the_tracked_config(
    world: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(run_month, "running_as_root", lambda: True)
    with pytest.raises(SystemExit, match="--allow-root"):
        run_month.main(argv(world), shell=shell_for(world, [Step(FILE1, 130)]))
    assert run_month.main(argv(world, "--allow-root"), shell=shell_for(world, [Step(FILE1, 130)])) == 0
    chain = run_month.Config.load(world["config"], world["overlay"]).chains["arbitrum-one"]
    assert run_month.resolve_engine_sha(chain, "abc1234", shell_for(world, [])) == "abc1234"  # an override runs nothing
    tracked = json.loads(world["config"].read_text())
    tracked["chains"]["arbitrum-one"]["engine_sha_cmd"] = ["/bin/true"]
    world["config"].write_text(json.dumps(tracked))
    with pytest.raises(SystemExit, match="untracked overlay"):
        run_month.Config.load(world["config"], world["overlay"])


def test_shipped_config_is_free_of_host_details() -> None:
    text = (HERE / "monthly.json").read_text() + (HERE / "monthly.local.example.json").read_text()
    for needle in ("127.0.0.1", "11090", "/var/", "/opt/", "/root"):
        assert needle not in text, needle
    cfg = run_month.Config.load(HERE / "monthly.json")
    assert all(c.solver_url is None and c.engine_sha_cmd is None for c in cfg.chains.values())
    assert "monthly.local.json" in (HERE.parent / ".gitignore").read_text()


# ------------------------------------------------------------------ host settings and lock


def test_live_run_refuses_missing_host_settings_and_dry_run_tolerates_them(
    world: dict[str, Path], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world["overlay"].unlink()
    monkeypatch.delenv("TEST_RPC")
    with pytest.raises(SystemExit) as e:
        run_month.main(argv(world), shell=shell_for(world, []))
    msg = str(e.value)
    assert "solver_url" in msg and "TEST_RPC" in msg and "engine_sha_cmd" in msg and "monthly.local.example.json" in msg
    shell = shell_for(world, [])
    assert run_month.main(argv(world, "--dry-run"), shell=shell) == 0
    assert "nothing was run" in capsys.readouterr().out and shell.watches == [] and shell.calls == []


def test_a_second_runner_does_not_start_while_the_lock_is_held(world: dict[str, Path]) -> None:
    world["evidence"].mkdir(exist_ok=True)
    with open(world["evidence"] / ".run_month.lock", "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        shell = shell_for(world, [Step(FILE1, 130)])
        with pytest.raises(SystemExit) as e:
            run_month.main(argv(world), shell=shell)
    assert e.value.code == run_month.EXIT_BUSY and shell.calls == []
    assert run_month.main(argv(world, "--dry-run"), shell=shell_for(world, [Step(FILE1, 130)])) == 0  # released


# ------------------------------------------------------------------ F13: the RPC URL stays private


def test_rpc_url_never_reaches_output_state_or_published_files(
    world: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    shell = shell_for(world, [Step(FILE1, 1), Step(FILE2, 130)], head=max(m["to_block"] for m in metas(FILE1)) + 50)
    assert run_month.main(argv(world), shell=shell) == 0
    cap = capsys.readouterr()
    assert SECRET not in cap.out + cap.err
    for root in (world["build"], world["evidence"]):
        for p in root.rglob("*"):
            if p.is_file():
                assert SECRET.encode() not in p.read_bytes(), p
    assert flag(shell.watches[0], "--rpc-url").endswith(SECRET)  # it does reach the tool, and only the tool


def test_failed_block_number_logs_the_env_var_name_not_the_url(capsys: pytest.CaptureFixture[str]) -> None:
    url = f"http://127.0.0.1:9/{SECRET}"  # nothing listens on the discard port
    assert run_month.RealShell().block_number(url, "READINESS_RPC_BASE") is None
    err = capsys.readouterr().err
    assert "$READINESS_RPC_BASE" in err and SECRET not in err and "127.0.0.1" not in err


# ------------------------------------------------------------------ F4 / F14: the real child handling


def child(tmp_path: Path, body: str) -> list[str]:
    script = tmp_path / "child.py"
    script.write_text("import os, signal, sys, time\nopen(sys.argv[1], 'w').write(str(os.getpid()))\n" + textwrap.dedent(body))
    return [sys.executable, str(script), str(tmp_path / "child.pid")]


@pytest.fixture
def plain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run_month.shutil, "which", lambda name: None)  # no nice/ionice wrapper in these tests


def quick() -> run_month.RealShell:
    return run_month.RealShell(poll_s=0.05, resend_s=0.3, term_wait_s=0.5, kill_wait_s=2)


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def child_pid(tmp_path: Path) -> int:
    return int((tmp_path / "child.pid").read_text())


def test_the_stop_signal_is_resent_until_the_tool_stops(tmp_path: Path, plain: None) -> None:
    """The tool ends a cycle on the first SIGINT and only stops on a later one."""
    cmd = child(
        tmp_path,
        """
        seen = []
        def on_int(*_):
            seen.append(1)
            if len(seen) >= 2:
                sys.exit(130)
        signal.signal(signal.SIGINT, on_int)
        while True:
            time.sleep(0.05)
    """,
    )
    res = quick().watch(cmd, 1.0, 10, tmp_path / "log" / "t.log")
    assert res.returncode == 130 and res.signalled and res.stop_reason == "deadline" and res.escalation == "sigint"


def test_a_tool_that_ignores_sigint_gets_sigterm_then_sigkill(tmp_path: Path, plain: None) -> None:
    cmd = child(
        tmp_path,
        """
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        while True:
            time.sleep(0.05)
    """,
    )
    t0 = time.monotonic()
    res = quick().watch(cmd, 1.0, 1, tmp_path / "t.log")
    assert res.returncode == -signal.SIGKILL and res.escalation == "sigkill" and res.signalled
    assert time.monotonic() - t0 < 10 and not alive(child_pid(tmp_path))


def test_runner_signal_stops_the_tool_and_is_reported(tmp_path: Path, plain: None) -> None:
    cmd = child(
        tmp_path,
        """
        signal.signal(signal.SIGINT, lambda *_: sys.exit(130))
        while True:
            time.sleep(0.05)
    """,
    )
    threading.Timer(0.8, lambda: os.kill(os.getpid(), signal.SIGTERM)).start()
    res = quick().watch(cmd, 30, 5, tmp_path / "t.log")
    assert res.stop_reason == "runner signal" and res.signalled and res.returncode == 130
    assert signal.getsignal(signal.SIGTERM) == signal.SIG_DFL  # the runner's handlers are put back


def test_the_tool_never_outlives_the_runner_even_on_an_error(tmp_path: Path, plain: None) -> None:
    cmd = child(tmp_path, "time.sleep(60)\n")

    def explode() -> str | None:
        if (tmp_path / "child.pid").exists():
            raise RuntimeError("boom")
        return None

    with pytest.raises(RuntimeError):
        quick().watch(cmd, 30, 5, tmp_path / "t.log", should_stop=explode)
    pid = child_pid(tmp_path)
    for _ in range(40):
        if not alive(pid):
            break
        time.sleep(0.05)
    assert not alive(pid)


def test_the_tool_gets_its_own_session_and_a_default_sigint_even_when_the_runner_ignores_it(tmp_path: Path, plain: None) -> None:
    cmd = child(
        tmp_path,
        """
        print("sid_is_pid", os.getsid(0) == os.getpid())
        print("sigint_default", signal.getsignal(signal.SIGINT) is signal.default_int_handler)
    """,
    )
    old = signal.signal(signal.SIGINT, signal.SIG_IGN)  # what a background shell hands the runner
    try:
        res = quick().watch(cmd, 30, 5, tmp_path / "t.log")
    finally:
        signal.signal(signal.SIGINT, old)
    log = (tmp_path / "t.log").read_text()
    assert res.returncode == 0 and "sid_is_pid True" in log and "sigint_default True" in log


def test_the_tool_runs_under_idle_priority_where_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run_month.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert run_month.wrap_priority(["tool", "--x"]) == ["ionice", "-c3", "nice", "-n", "19", "tool", "--x"]
    monkeypatch.setattr(run_month.shutil, "which", lambda name: None)
    assert run_month.wrap_priority(["tool"]) == ["tool"]
