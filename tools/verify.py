#!/usr/bin/env python3
"""Verify the record end to end. Exit 0 only when every check holds.

  python3 tools/verify.py                       # everything committed here
  python3 tools/verify.py --evidence-root DIR   # also the evidence files (ours)

Checks, per run: SHA256SUMS matches report.md and run.json; run.json's own
report digest matches; the run's chain and id match its directory; the facts
the report screen prints agree with run.json. Then: index.json and the README
table are exactly what the run.json files regenerate to, and the root
SHA256SUMS covers every file in the repository with the right digest.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import record


class Report:
    def __init__(self):
        self.failures: list[str] = []
        self.checked = 0

    def check(self, ok: bool, what: str) -> None:
        self.checked += 1
        if not ok:
            self.failures.append(what)


def verify_run(rd: Path, root: Path, rep: Report, evidence_root: Path | None) -> None:
    rel = rd.relative_to(root).as_posix()
    run = record.load_run(rd)
    rep.check(run.get("chain") == rd.parent.name and run.get("run_id") == rd.name, f"{rel}: chain/run_id disagree with the path")
    rep.check(run.get("schema") == record.SCHEMA, f"{rel}: unknown schema {run.get('schema')!r}")
    sums = {n: d for d, n in record.read_sums(rd / "SHA256SUMS")}
    for name in ("report.md", "run.json"):
        rep.check(name in sums, f"{rel}: SHA256SUMS lacks {name}")
        if name in sums:
            rep.check(record.sha256_file(rd / name) == sums[name], f"{rel}: {name} does not match SHA256SUMS")
    rep.check(run["report"]["sha256"] == record.sha256_file(rd / "report.md"), f"{rel}: run.json report digest is stale")
    parsed = record.parse_report((rd / "report.md").read_text())
    for k in ("verdict", "window", "checks"):
        if k in parsed and k in run:
            rep.check(parsed[k] == run[k], f"{rel}: report {k} differs from run.json")
    ev = run.get("evidence", [])
    ev_file = rd / "EVIDENCE.sha256"
    rep.check(bool(ev) == ev_file.is_file(), f"{rel}: EVIDENCE.sha256 present/absent disagrees with run.json")
    if ev:
        listed = {n: d for d, n in record.read_sums(ev_file)}
        rep.check(listed == {e["path"]: e["sha256"] for e in ev}, f"{rel}: EVIDENCE.sha256 differs from run.json")
        if evidence_root is not None:
            for e in ev:
                p = evidence_root / e["path"]
                if not p.is_file():
                    rep.check(False, f"{rel}: evidence missing at {p}")
                    continue
                rep.check(p.stat().st_size == e["bytes"] and record.sha256_file(p) == e["sha256"]
                          and record.count_lines(p) == e["lines"], f"{rel}: evidence {e['path']} does not match")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    ap.add_argument("--evidence-root", type=Path)
    ap.add_argument("-q", "--quiet", action="store_true")
    a = ap.parse_args(argv)
    root = a.root.resolve()
    rep = Report()
    n_runs = 0
    for rd in record.iter_runs(root):
        n_runs += 1
        verify_run(rd, root, rep, a.evidence_root)
    rows = record.build_index(root)
    idx = root / "index.json"
    rep.check(idx.is_file() and json.loads(idx.read_text()) == {"schema": record.SCHEMA, "runs": rows}, "index.json is stale")
    readme = root / "README.md"
    if readme.is_file():
        want = record.splice_generated(readme.read_text(), record.render_runs_table(rows))
        rep.check(want == readme.read_text(), "README.md runs table is stale")
    manifest = root / "SHA256SUMS"
    if manifest.is_file():
        have = {n: d for d, n in record.read_sums(manifest)}
        want_m = {n: d for d, n in record.build_root_manifest(root)}
        missing = sorted(set(want_m) - set(have))
        extra = sorted(set(have) - set(want_m))
        bad = sorted(n for n in set(have) & set(want_m) if have[n] != want_m[n])
        rep.check(not missing, f"root SHA256SUMS lacks: {', '.join(missing)}")
        rep.check(not extra, f"root SHA256SUMS lists files that are gone: {', '.join(extra)}")
        rep.check(not bad, f"root SHA256SUMS digest mismatch: {', '.join(bad)}")
    else:
        rep.check(False, "root SHA256SUMS missing")
    if not a.quiet:
        print(f"{n_runs} run(s), {rep.checked} checks, {len(rep.failures)} failure(s)"
              + (f"; evidence verified under {a.evidence_root}" if a.evidence_root else ""))
    for f in rep.failures:
        print("FAIL:", f)
    return 1 if rep.failures else 0


if __name__ == "__main__":
    sys.exit(main())
