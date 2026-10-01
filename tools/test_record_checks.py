"""record.parse_report must split every check line the tool prints, including a label that fills
the whole 24-column label field ("winner surplus plausible", cow-backtester 0.11.1+), and must
never join two check lines across a newline. The four published reports must parse as before."""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import record  # noqa: E402

ROOT = HERE.parent

ARTEFACT_DETAIL = (
    "1 auction(s) excluded from the capture verdict as valuation artefact(s) — auction 7 [rule: > 100x the rest, >= 20 attempted]"
)
SCREEN = f"""
====================================================================
  READINESS — kaisersolver   [REVIEW]
  bnb · prod · blocks 100..200
====================================================================
  budget  : 2.350 s (observed, BUDGETS_S[bnb].settle)
  [PASS] reached auctions         40 auctions attempted
  [WARN] prices look plausible    2 auction(s) flagged implausible_surplus
  [WARN] winner surplus plausible {ARTEFACT_DETAIL}
  [PASS] scan coverage            every block in the window was scanned
  [PASS] field coverage           44 settlements, 44 auctions formed, 0 excluded (0% of field; top reasons [])
    # cow-backtester 0.11.3 · engine build sha: abc1234
"""


def test_24_column_label_does_not_swallow_the_next_line() -> None:
    checks = record.parse_report(SCREEN)["checks"]
    assert [c["label"] for c in checks] == [
        "reached auctions",
        "prices look plausible",
        "winner surplus plausible",
        "scan coverage",
        "field coverage",
    ]
    assert checks[2]["detail"].startswith("1 auction(s) excluded") and checks[2]["detail"].endswith(">= 20 attempted]")
    assert checks[3] == {"level": "pass", "label": "scan coverage", "detail": "every block in the window was scanned"}


def test_hand_padded_lines_still_split() -> None:
    text = "[PASS] reached auctions              600 auctions attempted\n[WARN] bid coverage  50% of answered\n[FAIL] x\n"
    checks = record.parse_report(text)["checks"]
    assert checks[0] == {"level": "pass", "label": "reached auctions", "detail": "600 auctions attempted"}
    assert checks[1] == {"level": "warn", "label": "bid coverage", "detail": "50% of answered"}
    assert checks[2] == {"level": "fail", "label": "x", "detail": ""}


def test_published_reports_parse_exactly_as_recorded() -> None:
    for run_json in sorted(ROOT.glob("runs/*/*/run.json")):
        run = json.loads(run_json.read_text())
        parsed = record.parse_report((run_json.parent / "report.md").read_text())
        assert parsed["checks"] == run["checks"], run_json
        assert parsed["verdict"] == run["verdict"] and parsed["window"] == run["window"], run_json


def test_a_label_wider_than_the_field_is_refused_not_guessed() -> None:
    import pytest

    with pytest.raises(ValueError):
        record._split_check("PASS", "winner surplus plausible! detail")
    ok = record._split_check("PASS", "winner surplus plausible " + "detail")
    assert ok["label"] == "winner surplus plausible" and ok["detail"] == "detail"
