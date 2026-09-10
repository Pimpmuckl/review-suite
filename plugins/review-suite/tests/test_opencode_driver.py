import argparse
import json
import subprocess
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
from review_suite_core.opencode_runtime import opencode_review_env


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


def test_git_show_uses_first_parent_for_single_commit_review() -> None:
    assert _git_diff_command(_args(commit="abc")) == [
        "git",
        "show",
        "--first-parent",
        "--format=fuller",
        "--no-ext-diff",
        "--no-textconv",
        "abc",
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


def test_event_stream_rejects_incomplete_assistant_text() -> None:
    stdout = "\n".join(
        [
            '{"type":"step_start","sessionID":"ses_1"}',
            '{"type":"text","sessionID":"ses_1","part":{"text":"compaction summary"}}',
            '{"type":"step_finish","sessionID":"ses_1"}',
        ]
    )

    session_id, text = _parse_event_stream(stdout)

    assert session_id == "ses_1"
    assert text is None


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


def test_completed_unmarked_response_is_preserved_for_classification() -> None:
    text = "[P1] Rejected reservation changes stock."
    events = [
        {"type": "step_start", "sessionID": "ses_1"},
        {"type": "text", "part": {"text": text}},
        {"type": "step_finish", "part": {"reason": "stop"}},
    ]
    assert _parse_event_stream("\n".join(map(json.dumps, events))) == ("ses_1", text)
    assert _assistant_text_candidates(
        {
            "messages": [
                {
                    "info": {"role": "assistant", "finish": "stop", "summary": True},
                    "parts": [{"type": "text", "text": "compaction summary"}],
                },
                {
                    "info": {"role": "assistant", "finish": "stop"},
                    "parts": [{"type": "text", "text": text}],
                },
            ]
        }
    ) == [text]


def test_driver_stdio_roundtrips_unicode(monkeypatch) -> None:
    monkeypatch.setenv("PYTHONIOENCODING", "cp1252")
    text = "Review: café → 修复"
    proc = subprocess.run(
        [sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read())"],
        input=text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=opencode_review_env(),
        check=True,
    )
    assert proc.stdout == text
