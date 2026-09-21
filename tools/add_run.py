#!/usr/bin/env python3
"""Record a run: copy its report in unchanged, fingerprint the evidence it was
built from, write run.json + SHA256SUMS + EVIDENCE.sha256, then regenerate
index.json, the README table and the root SHA256SUMS.

    tools/add_run.py --chain base --run-id 2026-09-14 --report /path/to/report.md \
        --evidence-root /path/to/evidence --evidence runs/base/2026-09-14.jsonl \
        --evidence archive/bodies-0.11/manifest.jsonl:body-archive-manifest \
        --provenance KaiserSolver/cow-backtester@e7b3b73:docs/readiness/kaisersolver-base-2026-09-14.md \
        --figures figures.json --figures-key base

Everything the readiness screen prints (verdict, window, checks, tool and
engine versions, budget) is parsed from the report; pass --tool-version /
--engine-sha / --verdict to supply or override. --meta merges an arbitrary
JSON object into run.json for anything else.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import record


def parse_provenance(s: str) -> dict:
    # REPO@COMMIT:PATH
    try:
        repo, rest = s.split("@", 1)
        commit, path = rest.split(":", 1)
    except ValueError:
        raise SystemExit(f"--provenance must be REPO@COMMIT:PATH, got {s!r}") from None
    return {"repository": repo, "commit": commit, "path": path}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent, help="repository root")
    ap.add_argument("--chain", required=True)
    ap.add_argument("--run-id", required=True, help="YYYY-MM-DD the run started (UTC), optional suffix")
    ap.add_argument("--report", required=True, type=Path, help="the public report; copied in byte-for-byte")
    ap.add_argument("--kind", default="readiness")
    ap.add_argument("--solver", default="kaisersolver")
    ap.add_argument("--status", default="current", choices=["current", "superseded"])
    ap.add_argument("--superseded-by", help="chain/run-id of the run that replaces this one")
    ap.add_argument("--tool-version")
    ap.add_argument("--engine-sha")
    ap.add_argument("--verdict", choices=["READY", "REVIEW", "NOT READY"])
    ap.add_argument("--evidence-root", type=Path, help="directory the --evidence paths are relative to")
    ap.add_argument("--evidence", action="append", default=[], metavar="REL[:KIND]",
                    help="an evidence file, relative to --evidence-root; KIND defaults to 'rows'")
    ap.add_argument("--provenance", help="where the report was first published: REPO@COMMIT:PATH")
    ap.add_argument("--figures", type=Path, help="JSON of figures emitted by the report generator")
    ap.add_argument("--figures-key", help="pick this key out of --figures (e.g. the chain)")
    ap.add_argument("--meta", type=Path, help="JSON object merged into run.json (top level)")
    ap.add_argument("--note", action="append", default=[], help="free-text note (repeatable)")
    ap.add_argument("--link", action="append", default=[], metavar="NAME=URL")
    ap.add_argument("--force", action="store_true", help="overwrite an existing run directory")
    a = ap.parse_args(argv)

    if not record.CHAIN_RE.match(a.chain):
        raise SystemExit(f"bad chain {a.chain!r}")
    if not record.RUN_ID_RE.match(a.run_id):
        raise SystemExit(f"bad run id {a.run_id!r} (YYYY-MM-DD plus an optional lowercase suffix)")
    if a.evidence and not a.evidence_root:
        raise SystemExit("--evidence needs --evidence-root")
    if a.status == "superseded" and not a.superseded_by:
        raise SystemExit("--status superseded needs --superseded-by")

    rd = record.run_dir(a.root, a.chain, a.run_id)
    if rd.exists() and not a.force:
        raise SystemExit(f"{rd} exists (use --force to overwrite)")
    text = a.report.read_text()
    parsed = record.parse_report(text)
    if parsed.get("chain") not in (None, a.chain):
        raise SystemExit(f"report says chain {parsed['chain']!r}, --chain says {a.chain!r}")

    run = {"schema": record.SCHEMA, "kind": a.kind, "solver": parsed.get("solver", a.solver),
           "chain": a.chain, "run_id": a.run_id, "status": a.status}
    if a.superseded_by:
        run["superseded_by"] = a.superseded_by
    for k in ("env", "verdict", "window", "budget", "checks", "tool", "engine", "insufficient_sample"):
        if k in parsed:
            run[k] = parsed[k]
    if a.tool_version:
        run["tool"] = {"name": "cow-backtester", "version": a.tool_version}
    if a.engine_sha:
        run["engine"] = {"build_sha": a.engine_sha}
    if a.verdict:
        run["verdict"] = a.verdict
    if "verdict" not in run:
        raise SystemExit("no verdict found in the report; pass --verdict")
    if a.figures:
        fig = json.loads(a.figures.read_text())
        if a.figures_key:
            fig = fig[a.figures_key]
        run["figures"] = fig
    if a.meta:
        run.update(json.loads(a.meta.read_text()))
    if a.provenance:
        run["provenance"] = parse_provenance(a.provenance)
    if a.note:
        run["notes"] = a.note
    if a.link:
        run["links"] = dict(kv.split("=", 1) for kv in a.link)

    evidence = []
    for item in a.evidence:
        rel, _, kind = item.partition(":")
        path = a.evidence_root / rel
        if not path.is_file():
            raise SystemExit(f"evidence file missing: {path}")
        evidence.append({"path": Path(rel).as_posix(), "kind": kind or "rows", "sha256": record.sha256_file(path),
                         "bytes": path.stat().st_size, "lines": record.count_lines(path)})
    run["evidence"] = evidence

    if rd.exists():
        shutil.rmtree(rd)
    rd.mkdir(parents=True)
    shutil.copyfile(a.report, rd / "report.md")
    report_sha = record.sha256_file(rd / "report.md")
    run["report"] = {"path": "report.md", "sha256": report_sha, "bytes": (rd / "report.md").stat().st_size}
    run["recorded_at"] = record.utc_now()
    (rd / "run.json").write_text(json.dumps(run, indent=1, ensure_ascii=False) + "\n")
    record.write_sums(rd / "SHA256SUMS", [(report_sha, "report.md"), (record.sha256_file(rd / "run.json"), "run.json")])
    if evidence:
        (rd / "EVIDENCE.sha256").write_text(record.evidence_lines(evidence))
    record.regenerate(a.root)
    print(f"recorded {rd.relative_to(a.root)}  verdict={run['verdict']}  "
          f"report sha256={report_sha[:12]}…  evidence={len(evidence)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
