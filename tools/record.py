"""Shared code for the kaisersolver readiness record.

A run lives at runs/<chain>/<run-id>/ and holds:
  report.md        the public readiness report, byte-for-byte as published
  run.json         the structured record: window, verdict, checks, tool and
                   engine versions, provenance, and the evidence fingerprints
  SHA256SUMS       sha256 of report.md and run.json (`sha256sum -c SHA256SUMS`)
  EVIDENCE.sha256  sha256 / size / line count of the evidence files the report
                   was built from; those files are not in this repository

Stdlib only; Python 3.10+.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = 1
RUN_ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[a-z0-9-]*$")
CHAIN_RE = re.compile(r"^[a-z0-9-]+$")
GENERATED_START = "<!-- runs:start -->"
GENERATED_END = "<!-- runs:end -->"
# files the root SHA256SUMS never covers: itself, and git's own metadata
ROOT_MANIFEST_SKIP = {"SHA256SUMS"}

_VERDICT_RE = re.compile(r"READINESS\s+[—-]\s+(?P<solver>\S+)\s+\[(?P<verdict>READY|REVIEW|NOT READY)\]")
_WINDOW_RE = re.compile(r"^\s*(?P<chain>[a-z0-9-]+) · (?P<env>[a-z]+) · blocks (?P<lo>\d+)\.\.(?P<hi>\d+)\s*$", re.MULTILINE)
_CHECK_RE = re.compile(r"^\s*\[(?P<level>PASS|WARN|FAIL)\] (?P<label>.+?)\s{2,}(?P<detail>.+?)\s*$", re.MULTILINE)
_TOOL_RE = re.compile(r"#\s*cow-backtester (?P<version>\d[\w.]*)(?: · engine build sha: (?P<sha>[0-9a-f]{7,40}))?")
_BUDGET_RE = re.compile(r"^\s*budget\s+:\s+(?P<s>[\d.]+) s \((?P<source>\w+)", re.MULTILINE)
_SAMPLE_RE = re.compile(r"^\s*INSUFFICIENT SAMPLE", re.MULTILINE)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def count_lines(path: Path) -> int:
    n = 0
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            n += chunk.count(b"\n")
    return n


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_report(text: str) -> dict:
    """Pull the facts the tool prints on its readiness screen out of a report.

    Works on any report that embeds the `cow-backtester --readiness` screen
    (every report in this record does). Missing pieces are left out rather
    than guessed; the caller can supply them explicitly.
    """
    out: dict = {}
    m = _VERDICT_RE.search(text)
    if m:
        out["solver"] = m["solver"]
        out["verdict"] = m["verdict"]
    m = _WINDOW_RE.search(text)
    if m:
        out["chain"] = m["chain"]
        out["env"] = m["env"]
        out["window"] = {"from_block": int(m["lo"]), "to_block": int(m["hi"])}
    checks = [{"level": c["level"].lower(), "label": c["label"].strip(), "detail": c["detail"].strip()}
              for c in _CHECK_RE.finditer(text)]
    if checks:
        out["checks"] = checks
    m = _TOOL_RE.search(text)
    if m:
        out["tool"] = {"name": "cow-backtester", "version": m["version"]}
        if m["sha"]:
            out["engine"] = {"build_sha": m["sha"]}
    m = _BUDGET_RE.search(text)
    if m:
        out["budget"] = {"seconds": float(m["s"]), "source": m["source"]}
    if _SAMPLE_RE.search(text):
        out["insufficient_sample"] = True
    return out


def run_dir(root: Path, chain: str, run_id: str) -> Path:
    return root / "runs" / chain / run_id


def iter_runs(root: Path):
    runs = root / "runs"
    if not runs.is_dir():
        return
    for chain_dir in sorted(p for p in runs.iterdir() if p.is_dir()):
        for rd in sorted(p for p in chain_dir.iterdir() if p.is_dir()):
            if (rd / "run.json").is_file():
                yield rd


def load_run(rd: Path) -> dict:
    return json.loads((rd / "run.json").read_text())


def write_sums(path: Path, entries: list[tuple[str, str]]) -> None:
    """`sha256sum` format: two spaces between the digest and the name."""
    path.write_text("".join(f"{digest}  {name}\n" for digest, name in entries))


def read_sums(path: Path) -> list[tuple[str, str]]:
    out = []
    for ln in path.read_text().splitlines():
        ln = ln.rstrip("\n")
        if not ln.strip() or ln.startswith("#"):
            continue
        digest, name = ln.split("  ", 1)
        out.append((digest.strip(), name))
    return out


def evidence_lines(entries: list[dict]) -> str:
    """EVIDENCE.sha256: digest, then a comment with size and line count.

    Kept `sha256sum -c`-compatible: run it from the evidence root and the
    comment lines are ignored."""
    lines = ["# evidence files this run was built from; paths are relative to an evidence root",
             "# (they are not in this repository). Check with: sha256sum -c EVIDENCE.sha256"]
    for e in entries:
        lines.append(f"# {e['path']}: {e['bytes']} bytes, {e['lines']} lines, {e['kind']}")
        lines.append(f"{e['sha256']}  {e['path']}")
    return "\n".join(lines) + "\n"


def build_index(root: Path) -> list[dict]:
    rows = []
    for rd in iter_runs(root):
        r = load_run(rd)
        rows.append({
            "chain": r["chain"], "run_id": r["run_id"], "path": rd.relative_to(root).as_posix(),
            "kind": r.get("kind", "readiness"), "status": r.get("status", "current"),
            "verdict": r.get("verdict"), "env": r.get("env"),
            "window": r.get("window"), "tool": r.get("tool"), "engine": r.get("engine"),
            "warns": [c["label"] for c in r.get("checks", []) if c["level"] == "warn"],
            "fails": [c["label"] for c in r.get("checks", []) if c["level"] == "fail"],
            "report_sha256": r["report"]["sha256"],
            "recorded_at": r.get("recorded_at"),
        })
    return rows


def render_runs_table(rows: list[dict]) -> str:
    if not rows:
        return "_No runs recorded yet._\n"
    head = ("| Chain | Run | Window (blocks) | Verdict | Warns | Tool | Engine | Report sha256 | Status |\n"
            "|---|---|---|---|---|---|---|---|---|\n")
    body = ""
    for r in rows:
        w = r.get("window") or {}
        win = f"{w.get('from_block', '?')}–{w.get('to_block', '?')}"
        tool = (r.get("tool") or {}).get("version", "?")
        eng = (r.get("engine") or {}).get("build_sha", "?")
        warns = ", ".join(r["warns"]) if r["warns"] else "—"
        fails = ", ".join(r["fails"])
        verdict = r["verdict"] or "?"
        if fails:
            verdict += f" (fail: {fails})"
        body += (f"| {r['chain']} | [{r['run_id']}]({r['path']}/report.md) | {win} | {verdict} | {warns} | {tool} | `{eng}` | "
                 f"`{r['report_sha256'][:12]}` | {r['status']} |\n")
    return head + body


def splice_generated(readme: str, block: str) -> str:
    if GENERATED_START not in readme or GENERATED_END not in readme:
        raise SystemExit(f"README.md is missing the {GENERATED_START} / {GENERATED_END} markers")
    pre, rest = readme.split(GENERATED_START, 1)
    _, post = rest.split(GENERATED_END, 1)
    return f"{pre}{GENERATED_START}\n{block}{GENERATED_END}{post}"


def tracked_files(root: Path) -> list[str]:
    """Every file the root manifest covers: tracked by git when this is a
    checkout, else everything on disk. `.git/` and the manifest are skipped."""
    import subprocess
    try:
        out = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                             check=True, capture_output=True).stdout
        files = [f for f in out.decode().split("\0") if f]
    except (subprocess.CalledProcessError, FileNotFoundError):
        files = [p.relative_to(root).as_posix() for p in root.rglob("*")
                 if p.is_file() and ".git" not in p.relative_to(root).parts]
    return sorted(f for f in files if f not in ROOT_MANIFEST_SKIP and (root / f).is_file())


def build_root_manifest(root: Path) -> list[tuple[str, str]]:
    return [(sha256_file(root / f), f) for f in tracked_files(root)]


def regenerate(root: Path) -> None:
    """index.json, the README runs table and the root SHA256SUMS, from run.json files."""
    rows = build_index(root)
    (root / "index.json").write_text(json.dumps({"schema": SCHEMA, "runs": rows}, indent=1) + "\n")
    readme = root / "README.md"
    readme.write_text(splice_generated(readme.read_text(), render_runs_table(rows)))
    write_sums(root / "SHA256SUMS", build_root_manifest(root))
