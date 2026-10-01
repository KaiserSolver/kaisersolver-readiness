"""The `--json-out` boundary: read a run's JSON Lines files, pick the rows of one block
window (one per auction id), and reduce the per-cycle `_meta` lines to what the report needs.

A watch run appends one row per replayed auction and one `_meta` line per cycle. A restart
re-scans `--blocks` from the chain head, so auction ids repeat across files and the first
cycle of the new file overlaps the last cycle of the old one. A run killed mid-cycle leaves
rows with no closing `_meta`. Everything here is stdlib and pure, so it is testable without
the tool installed.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

Row = dict[str, Any]


@dataclass(frozen=True)
class Window:
    """A closed block range [lo, hi]."""

    lo: int
    hi: int

    def __post_init__(self) -> None:
        if self.lo > self.hi:
            raise ValueError(f"window lo {self.lo} > hi {self.hi}")

    def contains(self, block: int) -> bool:
        return self.lo <= block <= self.hi

    def overlaps(self, lo: int, hi: int) -> bool:
        return hi >= self.lo and lo <= self.hi


@dataclass(frozen=True)
class BadLine:
    path: str
    lineno: int
    reason: str


@dataclass
class JsonlRead:
    rows: list[Row] = field(default_factory=list)
    metas: list[dict[str, Any]] = field(default_factory=list)
    metas_by_file: list[list[dict[str, Any]]] = field(default_factory=list)
    bad_lines: list[BadLine] = field(default_factory=list)
    empty_files: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)


def launch_order(paths: Iterable[Path]) -> list[Path]:
    """Files sorted by name: the runner names them by launch time (YYYY-MM-DDTHHMMSSZ.jsonl)."""
    return sorted(paths, key=lambda p: p.name)


def read_jsonl(paths: Sequence[Path]) -> JsonlRead:
    """Every line of every file, in launch order. A line that is not JSON (a partial last line
    left by a killed run) is reported with its path and number, never silently skipped."""
    out = JsonlRead()
    for path in launch_order(paths):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as e:
            raise SystemExit(f"cannot read rows file {path}: {e}") from e
        out.files.append(str(path))
        if not text.strip():
            out.empty_files.append(str(path))
            out.metas_by_file.append([])
            continue
        out.metas_by_file.append(_read_lines(path, text, out))
    return out


def _read_lines(path: Path, text: str, out: JsonlRead) -> list[dict[str, Any]]:
    metas: list[dict[str, Any]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as e:
            out.bad_lines.append(BadLine(str(path), lineno, e.msg))
            continue
        if isinstance(obj, dict) and "_meta" in obj:
            metas.append(obj["_meta"])
            out.metas.append(obj["_meta"])
        elif isinstance(obj, dict) and "auction_id" in obj:
            out.rows.append(obj)
        else:
            out.bad_lines.append(BadLine(str(path), lineno, "neither a row nor a _meta line"))
    return metas


@dataclass
class WindowedRows:
    rows: list[Row]
    duplicates: int
    unplaced: int  # rows with no integer settlement_block; they cannot be windowed


def dedupe_in_window(rows: Iterable[Row], w: Window) -> WindowedRows:
    """One row per auction id whose settlement block lies in the window. The FIRST occurrence
    wins: a restart replays the same auction again, later and with a larger lag, and the first
    replay is what an uninterrupted run would hold."""
    kept: dict[Any, Row] = {}
    duplicates = unplaced = 0
    for r in rows:
        blk = r.get("settlement_block")
        if not isinstance(blk, int):
            unplaced += 1
            continue
        if not w.contains(blk):
            continue
        if r["auction_id"] in kept:
            duplicates += 1
            continue
        kept[r["auction_id"]] = r
    ordered = sorted(kept.values(), key=lambda r: (r["settlement_block"], str(r["auction_id"])))
    return WindowedRows(ordered, duplicates, unplaced)


@dataclass(frozen=True)
class Cycle:
    """One watch cycle's `_meta` line, the fields the report reads."""

    from_block: int
    to_block: int
    settlement_txs_found: int
    auctions_formed: int
    skipped: dict[str, int]
    failed_ranges: tuple[tuple[int, int], ...]
    max_auctions: int
    tool_version: str | None
    chain: str | None

    @classmethod
    def from_meta(cls, m: dict[str, Any]) -> Cycle:
        return cls(
            from_block=int(m["from_block"]),
            to_block=int(m["to_block"]),
            settlement_txs_found=int(m.get("settlement_txs_found") or 0),
            auctions_formed=int(m.get("auctions_formed") or 0),
            skipped={str(k): int(v) for k, v in (m.get("skipped") or {}).items()},
            failed_ranges=tuple((int(a), int(b)) for a, b in (m.get("failed_ranges") or [])),
            max_auctions=int(m.get("max_auctions") or 0),
            tool_version=m.get("v"),
            chain=m.get("chain"),
        )


@dataclass
class CycleSelection:
    kept: list[Cycle]
    overlap_blocks: int  # blocks scanned by more than one kept cycle (a restart's re-scan)
    dropped_contained: int  # cycles wholly inside blocks already kept

    @property
    def settlements_found(self) -> int:
        return sum(c.settlement_txs_found for c in self.kept)

    @property
    def auctions_formed(self) -> int:
        return sum(c.auctions_formed for c in self.kept)

    def skipped(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for c in self.kept:
            for k, v in c.skipped.items():
                out[k] = out.get(k, 0) + v
        return out

    def failed_ranges(self, w: Window) -> list[tuple[int, int]]:
        return clip_ranges((r for c in self.kept for r in c.failed_ranges), w)


def select_cycles(metas: Iterable[dict[str, Any]], w: Window) -> CycleSelection:
    """The cycles that touch the window. A cycle wholly inside blocks an earlier kept cycle
    already scanned is dropped; a partial overlap (a restart's re-scan) is kept and its
    overlapping blocks are counted, because the field counts it carries cannot be split."""
    kept: list[Cycle] = []
    covered: list[tuple[int, int]] = []
    overlap = dropped = 0
    for m in metas:
        c = Cycle.from_meta(m)
        if not w.overlaps(c.from_block, c.to_block):
            continue
        lo, hi = max(c.from_block, w.lo), min(c.to_block, w.hi)
        already = _covered_blocks(covered, lo, hi)
        if already == hi - lo + 1:
            dropped += 1
            continue
        overlap += already
        covered = clip_ranges([*covered, (lo, hi)], w)
        kept.append(c)
    return CycleSelection(kept, overlap, dropped)


def _covered_blocks(covered: list[tuple[int, int]], lo: int, hi: int) -> int:
    return sum(max(0, min(b, hi) - max(a, lo) + 1) for a, b in covered)


def clip_ranges(ranges: Iterable[tuple[int, int]], w: Window) -> list[tuple[int, int]]:
    """Ranges clipped to the window and merged."""
    spans = sorted((max(a, w.lo), min(b, w.hi)) for a, b in ranges if w.overlaps(a, b))
    merged: list[tuple[int, int]] = []
    for a, b in spans:
        if merged and a <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged


def resolve_window(metas_by_file: Sequence[Sequence[dict[str, Any]]], drop_last_cycle: bool = False) -> Window:
    """The run's own window: the first cycle's start to the last cycle's end. With
    `drop_last_cycle` the final cycle is left out (the run was interrupted inside it, so its
    `_meta` names blocks it never finished scanning)."""
    metas = [m for per_file in metas_by_file for m in per_file]
    if drop_last_cycle:
        metas = metas[:-1]
    if not metas:
        raise SystemExit("no complete cycle (_meta line) to derive the window from")
    return Window(int(metas[0]["from_block"]), max(int(m["to_block"]) for m in metas))
