"""regenerate.py heals a record whose root manifest no longer matches its files."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import add_run  # noqa: E402
import regenerate  # noqa: E402
import verify  # noqa: E402
from test_tools import REPORT  # noqa: E402  (the minimal screen add_run parses)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "tools").mkdir(parents=True)
    (root / "README.md").write_text("# t\n\n<!-- runs:start -->\n<!-- runs:end -->\n")
    rep = tmp_path / "report.md"
    rep.write_text(REPORT)
    add_run.main(["--root", str(root), "--chain", "base", "--run-id", "2026-10-01", "--report", str(rep)])
    assert verify.main(["--root", str(root)]) == 0
    return root


def test_a_new_tools_file_breaks_verify_and_regenerate_heals_it(repo: Path) -> None:
    (repo / "tools" / "new_tool.py").write_text("print('hi')\n")
    assert verify.main(["--root", str(repo)]) == 1
    assert regenerate.main(["--root", str(repo)]) == 0
    assert verify.main(["--root", str(repo)]) == 0
    assert "tools/new_tool.py" in (repo / "SHA256SUMS").read_text()


def test_missing_root_is_reported(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="cannot regenerate"):
        regenerate.main(["--root", str(tmp_path / "nowhere")])
