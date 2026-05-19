from __future__ import annotations

import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import review_deslop


def test_emit_output_only_reports_timeout_without_returncode(capsys) -> None:
    exit_code = review_deslop.emit_output_only(
        tool_name="review-deslop",
        result={"final_message": "", "returncode": None, "timed_out": True},
    )

    assert exit_code == 1
    assert capsys.readouterr().out == "review-deslop run timed out\n"
