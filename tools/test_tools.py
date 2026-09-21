"""Offline tests for the record tooling: `python3 -m pytest -q tools`."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import add_run  # noqa: E402
import record  # noqa: E402
import verify  # noqa: E402

REPORT = """# Readiness report: kaisersolver on base — 2026-10-01

```
====================================================================
  READINESS — kaisersolver   [REVIEW]
  base · prod · blocks 100..200
====================================================================
  budget  : 4.620 s (observed, BUDGETS_S[base].settle)
  [PASS] reached auctions         600 auctions attempted
  [WARN] bid coverage             50% of answered auctions carried >=1 solution
  [PASS] inside the deadline      0 deadline misses

  Reproduce this run:
    cow-backtester --chain base --readiness
    # cow-backtester 0.11.1 · engine build sha: abc1234
```
"""

README = "# t\n\n<!-- runs:start -->\n<!-- runs:end -->\n"


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "tools").mkdir(parents=True)
    (root / "README.md").write_text(README)
    ev = tmp_path / "evidence"
    (ev / "runs" / "base").mkdir(parents=True)
    (ev / "runs" / "base" / "2026-10-01.jsonl").write_text('{"_meta": {}}\n{"auction_id": 1}\n')
    rep = tmp_path / "report.md"
    rep.write_text(REPORT)
    return root, ev, rep


def add(root, ev, rep, *extra):
    return add_run.main(["--root", str(root), "--chain", "base", "--run-id", "2026-10-01", "--report", str(rep),
                         "--evidence-root", str(ev), "--evidence", "runs/base/2026-10-01.jsonl", *extra])


def test_parse_report():
    p = record.parse_report(REPORT)
    assert p["verdict"] == "REVIEW" and p["chain"] == "base" and p["env"] == "prod"
    assert p["window"] == {"from_block": 100, "to_block": 200}
    assert [c["level"] for c in p["checks"]] == ["pass", "warn", "pass"]
    assert p["checks"][1]["label"] == "bid coverage"
    assert p["tool"] == {"name": "cow-backtester", "version": "0.11.1"} and p["engine"] == {"build_sha": "abc1234"}
    assert p["budget"] == {"seconds": 4.62, "source": "observed"}
    assert "insufficient_sample" not in p


def test_add_then_verify(repo):
    root, ev, rep = repo
    assert add(root, ev, rep, "--link", "forum=https://example.org") == 0
    rd = root / "runs" / "base" / "2026-10-01"
    assert (rd / "report.md").read_bytes() == REPORT.encode()
    run = json.loads((rd / "run.json").read_text())
    assert run["verdict"] == "REVIEW" and run["evidence"][0]["lines"] == 2 and run["links"] == {"forum": "https://example.org"}
    assert run["report"]["sha256"] == record.sha256_file(rd / "report.md")
    assert "2026-10-01" in (root / "README.md").read_text()
    assert json.loads((root / "index.json").read_text())["runs"][0]["run_id"] == "2026-10-01"
    names = {n for _, n in record.read_sums(root / "SHA256SUMS")}
    assert "runs/base/2026-10-01/report.md" in names and "SHA256SUMS" not in names
    assert verify.main(["--root", str(root), "--evidence-root", str(ev), "-q"]) == 0


def test_tampered_report_fails(repo):
    root, ev, rep = repo
    add(root, ev, rep)
    p = root / "runs" / "base" / "2026-10-01" / "report.md"
    p.write_text(p.read_text().replace("[REVIEW]", "[READY]"))
    assert verify.main(["--root", str(root), "-q"]) == 1


def test_tampered_evidence_fails_only_with_evidence_root(repo):
    root, ev, rep = repo
    add(root, ev, rep)
    (ev / "runs" / "base" / "2026-10-01.jsonl").write_text('{"auction_id": 2}\n')
    assert verify.main(["--root", str(root), "-q"]) == 0
    assert verify.main(["--root", str(root), "--evidence-root", str(ev), "-q"]) == 1


def test_stale_index_and_stray_file_fail(repo):
    root, ev, rep = repo
    add(root, ev, rep)
    (root / "index.json").write_text('{"schema": 1, "runs": []}\n')
    assert verify.main(["--root", str(root), "-q"]) == 1
    record.regenerate(root)
    assert verify.main(["--root", str(root), "-q"]) == 0
    (root / "stray.txt").write_text("x")
    assert verify.main(["--root", str(root), "-q"]) == 1


def test_guards(repo):
    root, ev, rep = repo
    with pytest.raises(SystemExit):
        add_run.main(["--root", str(root), "--chain", "base", "--run-id", "bad id", "--report", str(rep)])
    with pytest.raises(SystemExit):  # report says base, flag says bnb
        add_run.main(["--root", str(root), "--chain", "bnb", "--run-id", "2026-10-01", "--report", str(rep)])
    assert add(root, ev, rep) == 0
    with pytest.raises(SystemExit):  # exists, no --force
        add(root, ev, rep)
    assert add(root, ev, rep, "--force") == 0
