"""Offline tests for rows.py against tools/fixtures/monthly (synthetic tool output, see make_fixture.py)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import rows  # noqa: E402

FIX = HERE / "fixtures" / "monthly"
RUNS = FIX / "runs" / "arbitrum-one"
FILE1, FILE2, EMPTY = RUNS / "fixture-a.jsonl", RUNS / "fixture-b.jsonl", RUNS / "empty.jsonl"
PARAMS = json.loads((FIX / "fixture.json").read_text())


@pytest.fixture(scope="module")
def both() -> rows.JsonlRead:
    return rows.read_jsonl([FILE2, FILE1])  # deliberately out of order


def test_files_are_read_in_launch_order(both: rows.JsonlRead) -> None:
    assert [Path(f).name for f in both.files] == [FILE1.name, FILE2.name]
    assert [len(m) for m in both.metas_by_file] == [3, 2]
    assert both.bad_lines == [] and both.empty_files == []


def test_window_restriction_is_inclusive(both: rows.JsonlRead) -> None:
    blocks = sorted({r["settlement_block"] for r in both.rows})
    w = rows.Window(blocks[1], blocks[3])
    kept = rows.dedupe_in_window(both.rows, w).rows
    assert [r["settlement_block"] for r in kept] == blocks[1:4]
    assert all(w.contains(r["settlement_block"]) for r in kept)


def test_dedupe_keeps_first_occurrence_and_counts_duplicates(both: rows.JsonlRead) -> None:
    lo, hi = PARAMS["window_full"]
    res = rows.dedupe_in_window(both.rows, rows.Window(lo, hi))
    ids = [r["auction_id"] for r in res.rows]
    assert len(ids) == len(set(ids)) == 40
    assert res.duplicates == 4 and res.unplaced == 0
    dup_id = next(r["auction_id"] for r in both.rows if sum(1 for x in both.rows if x["auction_id"] == r["auction_id"]) > 1)
    first = next(r for r in both.rows if r["auction_id"] == dup_id)
    kept = next(r for r in res.rows if r["auction_id"] == dup_id)
    assert kept is first  # the earlier file's replay, not the re-scan's


def test_killed_tail_rows_fall_outside_the_window(both: rows.JsonlRead) -> None:
    lo, hi = PARAMS["window_full"]
    tail = [r for r in both.rows if r["settlement_block"] > hi]
    assert len(tail) == PARAMS["killed_tail_rows"] == 3
    assert not any(
        r["auction_id"] in {t["auction_id"] for t in tail} for r in rows.dedupe_in_window(both.rows, rows.Window(lo, hi)).rows
    )


def test_rows_without_settlement_block_are_reported_not_dropped_silently() -> None:
    res = rows.dedupe_in_window([{"auction_id": 1}, {"auction_id": 2, "settlement_block": 5}], rows.Window(1, 10))
    assert res.unplaced == 1 and [r["auction_id"] for r in res.rows] == [2]


def test_empty_file_contributes_nothing_and_is_listed() -> None:
    res = rows.read_jsonl([EMPTY, FILE1])
    assert res.empty_files == [str(EMPTY)]
    assert res.metas_by_file[0] == [] and len(res.metas_by_file[1]) == 3  # sorted by name: empty.jsonl first
    assert len(res.rows) == 34


def test_missing_file_names_the_path() -> None:
    with pytest.raises(SystemExit, match="does-not-exist.jsonl"):
        rows.read_jsonl([RUNS / "does-not-exist.jsonl"])


def test_partial_last_line_is_a_bad_line(tmp_path: Path) -> None:
    p = tmp_path / "2026-10-01T120000Z.jsonl"
    p.write_text(FILE1.read_text().splitlines()[0] + "\n" + '{"auction_id": 5, "settle')
    res = rows.read_jsonl([p])
    assert res.bad_lines == [rows.BadLine(str(p), 2, res.bad_lines[0].reason)]
    assert len(res.rows) == 1


def test_cycle_aggregation_and_restart_overlap(both: rows.JsonlRead) -> None:
    lo, hi = PARAMS["window_full"]
    sel = rows.select_cycles(both.metas, rows.Window(lo, hi))
    assert len(sel.kept) == 5 and sel.dropped_contained == 0
    assert sel.settlements_found == sum(m["settlement_txs_found"] for m in both.metas)
    assert sel.auctions_formed == sum(m["auctions_formed"] for m in both.metas)
    file1_last, file2_first = both.metas_by_file[0][-1], both.metas_by_file[1][0]
    assert sel.overlap_blocks == file1_last["to_block"] - file2_first["from_block"] + 1 > 0
    assert sel.skipped() == {} and sel.failed_ranges(rows.Window(lo, hi)) == []


def test_contained_cycle_is_dropped() -> None:
    metas = [
        {"from_block": 100, "to_block": 200, "settlement_txs_found": 5, "auctions_formed": 5},
        {"from_block": 120, "to_block": 150, "settlement_txs_found": 2, "auctions_formed": 2},
    ]
    sel = rows.select_cycles(metas, rows.Window(1, 1000))
    assert len(sel.kept) == 1 and sel.dropped_contained == 1 and sel.settlements_found == 5


def test_clip_ranges_merges_and_clips() -> None:
    w = rows.Window(10, 100)
    assert rows.clip_ranges([(1, 12), (11, 20), (50, 60), (95, 200)], w) == [(10, 20), (50, 60), (95, 100)]


def test_resolve_window_and_drop_last_cycle(both: rows.JsonlRead) -> None:
    full = rows.resolve_window(both.metas_by_file)
    assert (full.lo, full.hi) == tuple(PARAMS["window_full"])
    dropped = rows.resolve_window(both.metas_by_file, drop_last_cycle=True)
    assert dropped.hi == both.metas_by_file[1][0]["to_block"]
    with pytest.raises(SystemExit, match="no complete cycle"):
        rows.resolve_window([[]])


def test_window_rejects_inverted_range() -> None:
    with pytest.raises(ValueError):
        rows.Window(5, 4)
