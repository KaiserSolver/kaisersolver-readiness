#!/usr/bin/env python3
"""Mutation smoke: apply one small wrong edit at a time to a scratch copy of tools/ and check that
the test suite fails. A mutation that leaves the suite green is a gap in the tests.

Not run in CI (each mutation is a full test run). Run by hand after changing render_report.py,
rebuild_state.py or their tests:

    PYTHONPATH=<cow-backtester checkout> python3 tools/mutation_smoke.py [--only SUBSTRING]

Exit 0 when every mutation was caught, 1 when one survived, 2 when a pattern no longer matches
the source (update the table below).
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent

# (file, exact text in the source, wrong replacement)
MUTATIONS: list[tuple[str, str, str]] = [
    ("render_report.py", "p95=rep[\"p95_ms\"]", "p95=rep[\"p50_ms\"]"),
    ("render_report.py", "capture=rep[\"capture_pct\"]", "capture=rep[\"capture_ex_artefact_pct\"]"),
    ("render_report.py", "valid_pct=rep[\"valid_rate_pct\"]", "valid_pct=rep[\"bid_rate_pct\"]"),
    ("render_report.py", "rate=rep[\"auctions_per_hour\"]", "rate=rep[\"answer_rate_pct\"]"),
    ("render_report.py", "found=rep[\"counts\"][\"settlements_found\"]", "found=rep[\"answered\"]"),
    ("render_report.py", "[:5]", "[:4]"),
    ("render_report.py", "* 60 for r in rows if r.get(", "* 24 for r in rows if r.get("),
    ("render_report.py", "/ 3600 if len(ts) > 1", "/ 60 if len(ts) > 1"),
    ("render_report.py", "{a.lower() for a in own_addresses}", "{a for a in own_addresses}"),
    ("render_report.py", "max(ratios) if ratios else None", "min(ratios) if ratios else None"),
    ("render_report.py", "w < win_med / 10", "w < win_med / 2"),
    ("render_report.py", "return \"n/a\" if x is None else f\"{x:.{nd}f} %\"", "return \"n/a\" if x is None else f\"{x:.0f} %\""),
    ("rebuild_state.py", "self.fairness_filtered += int(vs.get(\"fairness_filtered\") or 0)", "pass"),
    ("rebuild_state.py", "self.udcp_violations += int(vs.get(\"udcp_violations\") or 0)", "pass"),
    ("rebuild_state.py", "self.valid_zero_surplus += int(vs.get(\"valid_zero_surplus\") or 0)", "pass"),
    ("rebuild_state.py", "return \"bad_solver_response\" if reason.startswith(\"bad_solver_response (\") else reason",
     "return reason.split(\" (\", 1)[0]"),
    ("rebuild_state.py", "sum(int(r.get(\"validto_clamped\") or 0) for r in ours)", "0"),
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--only", default="", help="run only mutations whose replacement or file contains this text")
    only = ap.parse_args().only
    survived, stale = [], []
    for fname, old, new in MUTATIONS:
        if only and only not in fname + old + new:
            continue
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "repo"
            shutil.copytree(HERE.parent, work, ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache"))
            src = work / "tools" / fname
            text = src.read_text()
            if text.count(old) != 1:
                stale.append((fname, old, text.count(old)))
                continue
            src.write_text(text.replace(old, new))
            cmd = ["ionice", "-c3", "nice", "-n", "15", sys.executable, "-m", "pytest", "tools", "-q", "-x"]
            cmd += ["-p", "no:cacheprovider"]
            env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
            r = subprocess.run(cmd, cwd=work, capture_output=True, text=True, env=env)
            caught = r.returncode != 0
            print(f"{'caught  ' if caught else 'SURVIVED'}  {fname}: {old[:60]!r} -> {new[:40]!r}")
            if not caught:
                survived.append((fname, old))
    for fname, old, n in stale:
        print(f"STALE ({n} matches)  {fname}: {old[:70]!r}")
    return 2 if stale else (1 if survived else 0)


if __name__ == "__main__":
    sys.exit(main())
