import argparse
import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from review_suite_core.opencode_driver import (
    _assistant_text_candidates,
    _git_diff_command,
    _parse_event_stream,
)


def _args(**overrides: str | None) -> argparse.Namespace:
    values = {"base": None, "commit": None, "commit_end": None}
    values.update(overrides)
    return argparse.Namespace(**values)


def test_git_diff_command_for_branch_review() -> None:
    assert _git_diff_command(_args(base="main")) == [
        "git",
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        "main...HEAD",
    ]


def test_git_diff_command_for_commit_range() -> None:
    assert _git_diff_command(_args(base="abc", commit_end="def")) == [
        "git",
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        "abc..def",
    ]


def test_event_stream_prefers_final_terminal_review_message() -> None:
    stdout = "\n".join(
        [
            '{"type":"step_start","sessionID":"ses_1"}',
            '{"type":"text","sessionID":"ses_1","part":{"text":"compaction summary"}}',
            '{"type":"step_finish","sessionID":"ses_1"}',
            '{"type":"step_start","sessionID":"ses_1"}',
            '{"type":"text","sessionID":"ses_1","part":{"text":"[P1] Bug\\nReview result: findings"}}',
            '{"type":"step_finish","sessionID":"ses_1"}',
        ]
    )

    session_id, text = _parse_event_stream(stdout)

    assert session_id == "ses_1"
    assert text == "[P1] Bug\nReview result: findings"


def test_export_parser_collects_assistant_message_parts() -> None:
    payload = {
        "messages": [
            {
                "info": {"role": "assistant"},
                "parts": [
                    {"type": "text", "text": "Finding one"},
                    {"type": "text", "text": "Review result: findings"},
                ],
            }
        ]
    }

    assert _assistant_text_candidates(payload) == [
        "Finding one\nReview result: findings"
    ]
