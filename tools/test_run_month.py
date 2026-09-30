"""run_month.py against a fake Shell: no process is spawned, no git or gh is touched. The fake
"watch" copies fixture files into place with scripted exit codes. Needs cow_backtester for the
build stage (skipped without it)."""

from __future__ import annotations

import json
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

bt = pytest.importorskip("cow_backtester.backtest")

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import run_month  # noqa: E402

FIX = HERE / "fixtures" / "monthly"
RUNS = FIX / "runs" / "arbitrum-one"
FILE1, FILE2 = RUNS / "2026-10-01T000000Z.jsonl", RUNS / "2026-10-01T090000Z.jsonl"
PARAMS = json.loads((FIX / "fixture.json").read_text())
T0 = 1_790_812_800.0  # 2026-10-01T00:00:00Z


@dataclass
class FakeShell:
    """Scripted outside world. `watch_script` = per launch (source file to copy or None, exit code)."""

    watch_script: list[tuple[Path | None, int]]
    pr_exists: bool = False
    gh_authenticated: bool = True
    head: int | None = None
    clock: float = T0
    calls: list[list[str]] = field(default_factory=list)
    watches: list[list[str]] = field(default_factory=list)
    slept: list[float] = field(default_factory=list)

    def run(self, cmd: list[str], cwd: Path | None = None, timeout: float | None = None) -> run_month.Completed:
        self.calls.append(cmd)
        if cmd[:2] == ["cow-backtester", "--version"]:
            return run_month.Completed(0, f"cow-backtester {bt.VERSION}\n")
        if cmd[:3] == ["git", "status", "--porcelain"]:
            return run_month.Completed(0, "")
        if cmd[:2] == ["git", "rev-parse"]:
            return run_month.Completed(0, "main\n")
        if cmd[:3] == ["gh", "auth", "status"]:
            return run_month.Completed(0 if self.gh_authenticated else 1)
        if cmd[:3] == ["gh", "pr", "list"]:
            return run_month.Completed(0, "https://github.com/x/y/pull/7\n" if self.pr_exists else "")
        if cmd[:3] == ["gh", "pr", "create"]:
            return run_month.Completed(0, "https://github.com/x/y/pull/8\n")
        if cmd[:2] == ["sh", "-c"]:
            return run_month.Completed(0, "abc1234\n")
        return run_month.Completed(0, "")

    def watch(self, cmd: list[str], deadline_ts: float, grace_s: int) -> int:
        self.watches.append(cmd)
        src, rc = self.watch_script[len(self.watches) - 1]
        out = Path(cmd[cmd.index("--json-out") + 1])
        out.parent.mkdir(parents=True, exist_ok=True)
        if src is not None:
            shutil.copyfile(src, out)
            archive = Path(cmd[cmd.index("--archive-bodies") + 1])  # the tool writes its manifest there
            archive.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(FIX / "archive" / "bodies-0.11" / "manifest.jsonl", archive / "manifest.jsonl")
        else:
            out.write_bytes(b"")
        self.clock += 3600
        return rc

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.clock += seconds

    def now(self) -> float:
        return self.clock

    def block_number(self, rpc_url: str) -> int | None:
        return self.head

    def git_and_gh(self) -> list[list[str]]:
        reads = (["git", "status"], ["git", "rev-parse"], ["gh", "auth"], ["gh", "pr", "list"])
        return [c for c in self.calls if c[0] in ("git", "gh") and not any(c[: len(r)] == r for r in reads)]


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    record = tmp_path / "record"
    (record / "tools").mkdir(parents=True)
    (record / "README.md").write_text("# t\n\n<!-- runs:start -->\n<!-- runs:end -->\n")
    evidence = tmp_path / "evidence"
    (evidence / "archive" / f"bodies-{run_month.tool_minor_version()}").mkdir(parents=True)
    shutil.copyfile(
        FIX / "archive" / "bodies-0.11" / "manifest.jsonl",
        evidence / "archive" / f"bodies-{run_month.tool_minor_version()}" / "manifest.jsonl",
    )
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
                "chains": {
                    "arbitrum-one": {
                        "solver_url": PARAMS["solver_url"],
                        "own_addresses": PARAMS["own_addresses"],
                        "rpc_env": "TEST_RPC",
                        "engine_sha_cmd": "echo abc1234",
                        "watch_seconds": 60,
                        "blocks": 2000,
                    }
                },
            }
        )
    )
    monkeypatch.setenv("TEST_RPC", "http://rpc.test")
    return {"record": record, "evidence": evidence, "config": config, "build": tmp_path / "build"}


def argv(w: dict[str, Path], *extra: str) -> list[str]:
    return [
        "--chain",
        "arbitrum-one",
        "--config",
        str(w["config"]),
        "--record-root",
        str(w["record"]),
        "--build-root",
        str(w["build"]),
        "--start",
        "2026-10-01T00:00:00Z",
        *extra,
    ]


def test_full_run_with_restart_and_cut_short_last_cycle(world: dict[str, Path], capsys: pytest.CaptureFixture[str]) -> None:
    """Launch 1 dies (exit 1) after writing file 1; launch 2 is cut short by the deadline (130).
    The second launch scans from where the first stopped; the window drops the cut-short cycle."""
    last_to_file1 = max(json.loads(ln)["_meta"]["to_block"] for ln in FILE1.read_text().splitlines() if '"_meta"' in ln)
    shell = FakeShell(watch_script=[(FILE1, 1), (FILE2, 130)], head=last_to_file1 + 500)
    assert run_month.main(argv(world, "--dry-run"), shell=shell) == 0
    assert len(shell.watches) == 2 and shell.slept == [60]
    assert shell.watches[0][shell.watches[0].index("--blocks") + 1] == "2000"
    assert shell.watches[1][shell.watches[1].index("--blocks") + 1] == "500"  # head - last_to, no re-scan
    assert "--archive-bodies" in shell.watches[0]
    archive = Path(shell.watches[0][shell.watches[0].index("--archive-bodies") + 1])
    assert archive.name == "2026-10-01-arbitrum-one"  # one archive per run and chain
    run_dir = world["record"] / "runs" / "arbitrum-one" / "2026-10-01"
    run = json.loads((run_dir / "run.json").read_text())
    window = json.loads((world["build"] / "arbitrum-one" / "2026-10-01" / "window.json").read_text())
    file2_metas = [json.loads(ln)["_meta"] for ln in FILE2.read_text().splitlines() if '"_meta"' in ln]
    assert window["last_cycle_dropped"] is True and window["to_block"] == file2_metas[0]["to_block"]
    assert run["window"]["to_block"] == file2_metas[0]["to_block"]
    assert [e["path"].split("/")[-1] for e in run["evidence"]][:2] == sorted(
        p.name for p in (world["evidence"] / "runs" / "arbitrum-one").glob("*.jsonl")
    )
    assert "exit code(s) 1, 130" in run["notes"][0]
    assert shell.git_and_gh() == []  # dry run: nothing written to git, nothing to gh
    out = capsys.readouterr().out
    assert "git switch -c readiness/arbitrum-one-2026-10-01" in out and "gh pr create" in out


def test_publish_reuses_an_open_pr(world: dict[str, Path], capsys: pytest.CaptureFixture[str]) -> None:
    shell = FakeShell(watch_script=[(FILE1, 0)], pr_exists=True)
    assert run_month.main(argv(world), shell=shell) == 0
    cmds = shell.git_and_gh()
    assert [c[:2] for c in cmds] == [["git", "switch"], ["git", "add"], ["git", "commit"], ["git", "push"]]
    assert "PR already open: https://github.com/x/y/pull/7" in capsys.readouterr().out


def test_publish_opens_a_pr_when_none_exists(world: dict[str, Path], capsys: pytest.CaptureFixture[str]) -> None:
    shell = FakeShell(watch_script=[(FILE1, 0)])
    assert run_month.main(argv(world), shell=shell) == 0
    assert any(c[:3] == ["gh", "pr", "create"] for c in shell.calls)
    assert "PR opened: https://github.com/x/y/pull/8" in capsys.readouterr().out
    assert (
        (world["build"] / "arbitrum-one" / "2026-10-01" / "pr.md")
        .read_text()
        .startswith("Readiness run `arbitrum-one/2026-10-01`")
    )


def test_no_gh_auth_prints_the_command(world: dict[str, Path], capsys: pytest.CaptureFixture[str]) -> None:
    shell = FakeShell(watch_script=[(FILE1, 0)], gh_authenticated=False)
    assert run_month.main(argv(world), shell=shell) == 0
    assert not any(c[:3] == ["gh", "pr", "create"] for c in shell.calls)
    assert "open the PR by hand" in capsys.readouterr().out


def test_tool_that_never_produces_rows_gives_up_after_max_restarts(world: dict[str, Path]) -> None:
    shell = FakeShell(watch_script=[(None, 1), (None, 1), (None, 1)])
    with pytest.raises(SystemExit, match="failed 3 time"):
        run_month.main(argv(world), shell=shell)
    assert len(shell.watches) == 3 and shell.slept == [60, 60]
    assert not (world["record"] / "runs" / "arbitrum-one" / "2026-10-01").exists()


def test_usage_error_aborts_immediately(world: dict[str, Path]) -> None:
    shell = FakeShell(watch_script=[(None, 2)])
    with pytest.raises(SystemExit, match="exit 2"):
        run_month.main(argv(world), shell=shell)
    assert len(shell.watches) == 1


def test_existing_run_is_refused_without_force(world: dict[str, Path]) -> None:
    (world["record"] / "runs" / "arbitrum-one" / "2026-10-01").mkdir(parents=True)
    with pytest.raises(SystemExit, match="already exists"):
        run_month.main(argv(world), shell=FakeShell(watch_script=[]))


def test_skip_run_rebuilds_from_evidence_on_disk(world: dict[str, Path]) -> None:
    dst = world["evidence"] / "runs" / "arbitrum-one"
    dst.mkdir(parents=True)
    shutil.copyfile(FILE1, dst / FILE1.name)
    shutil.copyfile(FILE2, dst / FILE2.name)
    shell = FakeShell(watch_script=[])
    assert (
        run_month.main(
            [
                "--chain",
                "arbitrum-one",
                "--config",
                str(world["config"]),
                "--record-root",
                str(world["record"]),
                "--build-root",
                str(world["build"]),
                "--skip-run",
                "--dry-run",
                "--run-id",
                "2026-10-01",
            ],
            shell=shell,
        )
        == 0
    )
    assert shell.watches == []
    run = json.loads((world["record"] / "runs" / "arbitrum-one" / "2026-10-01" / "run.json").read_text())
    assert run["window"] == {"from_block": PARAMS["window_full"][0], "to_block": PARAMS["window_full"][1]}
    assert run["evidence"][-1]["path"] == f"archive/bodies-{run_month.tool_minor_version()}/manifest.jsonl"  # legacy layout
    assert "watch from 2026-10-01T00:00:00Z" in run["notes"][0]


def test_engine_sha_and_duration_parsing(world: dict[str, Path]) -> None:
    assert run_month.parse_duration("20h") == 72000 and run_month.parse_duration("90m") == 5400
    with pytest.raises(SystemExit, match="bad duration"):
        run_month.parse_duration("2 days")
    chain = run_month.Config.load(world["config"]).chains["arbitrum-one"]
    assert run_month.resolve_engine_sha(chain, None, FakeShell(watch_script=[])) == "abc1234"
    with pytest.raises(SystemExit, match="not 7-40 hex"):
        run_month.resolve_engine_sha(chain, "nope", FakeShell(watch_script=[]))
