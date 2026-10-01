"""End-to-end tests for build_report.py: the tool's own readiness over the rebuilt state, the
report the record accepts, the guards. Needs cow_backtester (skipped without it)."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

if os.environ.get("CI"):  # CI installs the tool: a missing or broken import must fail, not skip
    import cow_backtester.backtest as bt
else:
    bt = pytest.importorskip("cow_backtester.backtest")

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import add_run  # noqa: E402
import build_report  # noqa: E402
import record  # noqa: E402
import verify  # noqa: E402

FIX = HERE / "fixtures" / "monthly"
RUNS = FIX / "runs" / "arbitrum-one"
FILE1, FILE2, EMPTY = RUNS / "fixture-a.jsonl", RUNS / "fixture-b.jsonl", RUNS / "empty.jsonl"
MANIFEST = FIX / "archive" / f"bodies-{'.'.join(bt.VERSION.split('.')[:2])}" / "manifest.jsonl"
PARAMS = json.loads((FIX / "fixture.json").read_text())
EXPECTED = json.loads((FIX / "expected_full.json").read_text())["readiness"]
ENGINE_SHA = "deadbeef1"


def argv(out: Path, *extra: str, files: list[Path] | None = None, window: list[int] | None = None) -> list[str]:
    lo, hi = window or PARAMS["window_full"]
    args = [
        "--chain",
        "arbitrum-one",
        "--from-block",
        str(lo),
        "--to-block",
        str(hi),
        "--compete",
        "--solver-url",
        PARAMS["solver_url"],
        "--engine-sha",
        ENGINE_SHA,
        "--manifest",
        str(MANIFEST),
        "--regenerated-at",
        "2026-10-02T06:00:00Z",
        "--record-root",
        str(HERE.parent),
        "--out-dir",
        str(out),
    ]
    for a in PARAMS["own_addresses"]:
        args += ["--own-address", a]
    for f in files or [FILE1, FILE2, EMPTY]:
        args += ["--rows", str(f)]
    return args + list(extra)


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("build")
    assert build_report.build(argv(out)) == 0
    return out


def test_fixture_was_made_with_the_installed_tool() -> None:
    assert PARAMS["tool_version"] == bt.VERSION, "regenerate tools/fixtures/monthly with make_fixture.py"


def test_outputs_exist(built: Path) -> None:
    assert {p.name for p in built.iterdir()} == {"report.md", "figures.json", "readiness.json", "meta.json"}


def test_verdict_matches_the_tools_one_shot_reference(built: Path) -> None:
    rep = json.loads((built / "readiness.json").read_text())
    # the field-coverage detail carries the settlement/auction counts, which include the restart's
    # re-scan (test_rebuild_state covers that); every other check is byte-identical to the reference
    ours = [c for c in rep["checks"] if c["label"] != "field coverage"]
    theirs = [c for c in EXPECTED["checks"] if c["label"] != "field coverage"]
    assert ours == theirs and len(ours) == len(rep["checks"]) - 1
    assert [(c["level"], c["label"]) for c in rep["checks"]] == [(c["level"], c["label"]) for c in EXPECTED["checks"]]
    for key in (
        "verdict",
        "capture_pct",
        "capture_ex_artefact_pct",
        "per_bid_median_ratio",
        "per_bid_ratio_n",
        "auctions_attempted",
        "answered",
        "bids",
        "valid",
        "p50_ms",
        "p95_ms",
        "max_ms",
        "errors",
        "basis_mix",
        "fairness_evaluated",
        "fairness_filtered",
        "implausible",
        "validity_basis",
        "original_budget_upper_s",
    ):
        assert rep[key] == EXPECTED[key], key
    assert [a["auction_id"] for a in rep["artefact_auctions"]] == [a["auction_id"] for a in EXPECTED["artefact_auctions"]]
    assert rep["reproduce"].endswith(f"engine build sha: {ENGINE_SHA}") and "--bodies-dir <archive>" in rep["reproduce"]


def test_report_screen_is_what_the_record_parses(built: Path) -> None:
    rep = json.loads((built / "readiness.json").read_text())
    parsed = record.parse_report((built / "report.md").read_text())
    assert parsed["verdict"] == rep["verdict"] and parsed["chain"] == "arbitrum-one" and parsed["env"] == "prod"
    assert parsed["window"] == {"from_block": rep["window"]["from_block"], "to_block": rep["window"]["to_block"]}
    assert [(c["level"], c["label"]) for c in parsed["checks"]] == [
        ("pass" if c["level"] == "ok" else c["level"], c["label"]) for c in rep["checks"]
    ]
    assert parsed["tool"] == {"name": "cow-backtester", "version": bt.VERSION}
    assert parsed["engine"] == {"build_sha": ENGINE_SHA}
    assert parsed["budget"] == {"seconds": rep["budget_s"], "source": "observed"}
    assert parsed["insufficient_sample"] is True  # 40 < 500, and no "(override" marker by default
    assert "(override" not in (built / "report.md").read_text()


def test_round_trip_through_add_run_and_verify(built: Path, tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "tools").mkdir(parents=True)
    (root / "README.md").write_text("# t\n\n<!-- runs:start -->\n<!-- runs:end -->\n")
    rc = add_run.main(
        [
            "--root",
            str(root),
            "--chain",
            "arbitrum-one",
            "--run-id",
            "2026-10-01",
            "--report",
            str(built / "report.md"),
            "--evidence-root",
            str(FIX),
            "--evidence",
            "runs/arbitrum-one/fixture-a.jsonl",
            "--evidence",
            "runs/arbitrum-one/fixture-b.jsonl",
            "--evidence",
            str(MANIFEST.relative_to(FIX)) + ":body-archive-manifest",
            "--figures",
            str(built / "figures.json"),
            "--meta",
            str(built / "meta.json"),
            "--engine-sha",
            ENGINE_SHA,
            "--link",
            "forum=https://forum.cow.fi/t/3572",
        ]
    )
    assert rc in (0, None)
    assert verify.main(["--root", str(root), "--evidence-root", str(FIX)]) == 0
    run = json.loads((root / "runs" / "arbitrum-one" / "2026-10-01" / "run.json").read_text())
    assert run["figures"] == json.loads((built / "figures.json").read_text())
    assert run["generator"]["sha256"] == hashlib.sha256((HERE / "build_report.py").read_bytes()).hexdigest()
    assert run["generator"]["path"] == "tools/build_report.py" and run["rows_tool_version"] == bt.VERSION
    assert run["build"]["duplicates"] == 4 and run["build"]["launches"] == 2 and run["build"]["empty_files"] == ["empty.jsonl"]
    assert run["body_archive_manifest"]["lines"] == MANIFEST.read_bytes().count(b"\n")


def test_meta_and_summary(built: Path, capsys: pytest.CaptureFixture[str]) -> None:
    meta = json.loads((built / "meta.json").read_text())
    assert meta["build"]["rows_read"] == 47 and meta["build"]["rows_in_window"] == 40 and meta["build"]["cycles"] == 5
    assert meta["build"]["overlap_blocks"] > 0 and meta["build"]["bad_lines"] == []
    assert meta["generator"]["commit"] is None or len(meta["generator"]["commit"]) == 40


def rewrite_rows(src: Path, dst: Path, **changes: Any) -> Path:
    lines = []
    for line in src.read_text().splitlines():
        obj = json.loads(line)
        target = obj["_meta"] if "_meta" in obj else obj
        target.update(changes)
        lines.append(json.dumps(obj))
    dst.write_text("\n".join(lines) + "\n")
    return dst


def test_version_mismatch_is_refused_unless_allowed(tmp_path: Path) -> None:
    other = rewrite_rows(FILE1, tmp_path / "fixture-a.jsonl", v="0.9.9")
    with pytest.raises(SystemExit, match="produced by cow-backtester 0.9.9"):
        build_report.build(argv(tmp_path / "out", files=[other], window=PARAMS["window_file1"]))
    assert (
        build_report.build(argv(tmp_path / "out", "--allow-version-mismatch", files=[other], window=PARAMS["window_file1"])) == 0
    )
    text = (tmp_path / "out" / "report.md").read_text()
    assert f"# cow-backtester {bt.VERSION} · engine build sha" in text  # the screen keeps the installed version
    assert "- The rows were produced by cow-backtester 0.9.9" in text
    assert json.loads((tmp_path / "out" / "meta.json").read_text())["rows_tool_version"] == "0.9.9"


def test_mixed_versions_wrong_chain_bad_sha_are_refused(tmp_path: Path) -> None:
    mixed = rewrite_rows(FILE2, tmp_path / "fixture-b.jsonl", v="0.9.9")
    with pytest.raises(SystemExit, match="several tool versions"):
        build_report.build(argv(tmp_path / "o1", files=[FILE1, mixed]))
    with pytest.raises(SystemExit, match="not --chain base"):
        build_report.build(argv(tmp_path / "o2")[:1] + ["base"] + argv(tmp_path / "o2")[2:])
    with pytest.raises(SystemExit, match="not 7-40 hex"):
        build_report.build([a if a != ENGINE_SHA else "ZZZ" for a in argv(tmp_path / "o3")])
    with pytest.raises(SystemExit, match="from-block must be"):
        build_report.build(argv(tmp_path / "o4", window=[10, 5]))


def test_min_evidence_override_is_marked(tmp_path: Path) -> None:
    assert build_report.build(argv(tmp_path / "out", "--min-evidence", "40")) == 0
    text = (tmp_path / "out" / "report.md").read_text()
    assert "min_evidence 40 (override: min_evidence)" in text
    assert record.parse_report(text).get("insufficient_sample") is None  # 40 attempted meets the floor


def test_built_run_dir_carries_no_absolute_paths(built: Path) -> None:
    def strings(o: Any) -> Any:
        if isinstance(o, str):
            yield o
        elif isinstance(o, dict):
            for v in o.values():
                yield from strings(v)
        elif isinstance(o, list):
            for v in o:
                yield from strings(v)

    meta = json.loads((built / "meta.json").read_text())
    assert not [s for s in strings(meta) if s.startswith("/")]
    assert meta["bottom_line"] == "generated"


def test_add_run_takes_only_whitelisted_meta_and_marks_edited_bottom_line(built: Path, tmp_path: Path) -> None:
    import add_run

    meta = json.loads((built / "meta.json").read_text())
    meta["evidence_root"] = "/srv/private"
    (tmp_path / "meta.json").write_text(json.dumps(meta))
    report = (built / "report.md").read_text()

    def record(text: str, name: str) -> dict:
        rep = tmp_path / f"{name}.md"
        rep.write_text(text)
        root = tmp_path / name
        (root / "tools").mkdir(parents=True)
        (root / "README.md").write_text("# t\n\n<!-- runs:start -->\n<!-- runs:end -->\n")
        add_run.main(["--root", str(root), "--chain", "arbitrum-one", "--run-id", "2026-10-01", "--report", str(rep),
                      "--meta", str(tmp_path / "meta.json"), "--engine-sha", ENGINE_SHA])
        return json.loads((root / "runs" / "arbitrum-one" / "2026-10-01" / "run.json").read_text())

    run = record(report, "gen")
    assert run["bottom_line"] == "generated" and "evidence_root" not in run
    edited = record(report.replace("NOT READY:", "Reviewed:", 1), "ed")
    assert edited["bottom_line"] == "edited"
