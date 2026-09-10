import argparse
import json
import subprocess
import sys
from pathlib import Path

import pytest


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from review_suite_core.opencode_driver import (
    _assistant_text_candidates,
    _exported_review_text,
    _format_metadata_line,
    _git_diff_command,
    _opencode_run_command,
    _parse_event_stream,
    _usage_from_export,
    build_parser,
    parse_opencode_review_metadata,
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


def _run_args(tmp_path: Path, *extra: str) -> argparse.Namespace:
    return build_parser().parse_args(
        [
            "--model",
            "opencode-go/deepseek-flash",
            "--dir",
            str(tmp_path),
            "--title",
            "review-suite::test",
            *extra,
        ]
    )


def test_run_command_includes_variant_when_set(tmp_path: Path) -> None:
    patch = tmp_path / "target.patch"
    command = _opencode_run_command(
        "opencode", _run_args(tmp_path, "--variant", "high"), tmp_path, patch
    )

    assert command[command.index("--variant") + 1] == "high"
    assert command[-2:] == ["--file", str(patch)]


def test_run_command_omits_variant_when_unset(tmp_path: Path) -> None:
    patch = tmp_path / "target.patch"
    command = _opencode_run_command("opencode", _run_args(tmp_path), tmp_path, patch)

    assert "--variant" not in command


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

    result = _parse_event_stream(stdout)

    assert result["session_id"] == "ses_1"
    assert result["reviewer_output"] == "[P1] Bug\nReview result: findings"


def test_event_stream_rejects_incomplete_assistant_text() -> None:
    stdout = "\n".join(
        [
            '{"type":"step_start","sessionID":"ses_1"}',
            '{"type":"text","sessionID":"ses_1","part":{"text":"compaction summary"}}',
            '{"type":"step_finish","sessionID":"ses_1"}',
        ]
    )

    result = _parse_event_stream(stdout)

    assert result["session_id"] == "ses_1"
    assert result["reviewer_output"] is None


def test_event_stream_aggregates_step_finish_usage_and_cost() -> None:
    stdout = "\n".join(
        [
            json.dumps({"type": "step_start", "sessionID": "ses_1"}),
            json.dumps(
                {
                    "type": "step_finish",
                    "sessionID": "ses_1",
                    "part": {
                        "type": "step-finish",
                        "cost": 0.001,
                        "tokens": {
                            "total": 120309,
                            "input": 1899,
                            "output": 351,
                            "reasoning": 299,
                            "cache": {"read": 117760, "write": 0},
                        },
                    },
                }
            ),
            json.dumps(
                {
                    "type": "step_finish",
                    "sessionID": "ses_1",
                    "part": {
                        "type": "step-finish",
                        "cost": 0.002,
                        "tokens": {
                            "total": 200,
                            "input": 100,
                            "output": 50,
                            "reasoning": 0,
                            "cache": {"read": 0, "write": 50},
                        },
                    },
                }
            ),
        ]
    )

    result = _parse_event_stream(stdout)

    assert result["usage"] == {
        "input_tokens": 119809,
        "cached_input_tokens": 117760,
        "output_tokens": 401,
        "cache_write_tokens": 50,
        "reasoning_output_tokens": 299,
        "total_tokens": 120509,
    }
    assert result["cost_usd"] == pytest.approx(0.003)


def test_usage_from_export_sums_assistant_messages() -> None:
    payload = {
        "info": {"id": "ses_1"},
        "messages": [
            {"info": {"role": "user", "cost": None, "tokens": None}},
            {
                "info": {
                    "role": "assistant",
                    "cost": 0.5,
                    "tokens": {
                        "total": 10,
                        "input": 4,
                        "output": 1,
                        "reasoning": 1,
                        "cache": {"read": 4, "write": 0},
                    },
                }
            },
            {
                "info": {
                    "role": "assistant",
                    "cost": 0.25,
                    "tokens": {
                        "total": 8,
                        "input": 2,
                        "output": 1,
                        "reasoning": 0,
                        "cache": {"read": 5, "write": 0},
                    },
                }
            },
        ],
    }

    usage, cost_usd = _usage_from_export(payload)

    assert usage == {
        "input_tokens": 15,
        "cached_input_tokens": 9,
        "output_tokens": 2,
        "reasoning_output_tokens": 1,
        "total_tokens": 18,
    }
    assert cost_usd == pytest.approx(0.75)


def test_usage_from_export_treats_zero_cost_as_authoritative() -> None:
    payload = {
        "messages": [
            {
                "info": {
                    "role": "assistant",
                    "cost": 0,
                    "tokens": {
                        "total": 0,
                        "input": 0,
                        "output": 0,
                        "reasoning": 0,
                        "cache": {"read": 0, "write": 0},
                    },
                }
            }
        ]
    }

    usage, cost_usd = _usage_from_export(payload)

    assert usage == {
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "output_tokens": 0,
    }
    assert cost_usd == 0.0


def test_event_stream_omits_cost_when_provider_cost_absent() -> None:
    stdout = json.dumps(
        {
            "type": "step_finish",
            "sessionID": "ses_1",
            "part": {
                "type": "step-finish",
                "tokens": {"input": 10, "output": 2},
            },
        }
    )

    result = _parse_event_stream(stdout)

    assert result["usage"] == {
        "input_tokens": 10,
        "cached_input_tokens": 0,
        "output_tokens": 2,
    }
    assert result["cost_usd"] is None


def test_usage_from_export_omits_cost_when_provider_cost_absent() -> None:
    payload = {
        "messages": [
            {
                "info": {
                    "role": "assistant",
                    "tokens": {"input": 5, "output": 1},
                    "cost": None,
                }
            }
        ]
    }

    usage, cost_usd = _usage_from_export(payload)

    assert usage == {
        "input_tokens": 5,
        "cached_input_tokens": 0,
        "output_tokens": 1,
    }
    assert cost_usd is None


def test_usage_from_export_reports_cost_without_tokens() -> None:
    payload = {
        "messages": [{"info": {"role": "assistant", "tokens": None, "cost": 0.25}}]
    }

    usage, cost_usd = _usage_from_export(payload)

    assert usage == {}
    assert cost_usd == pytest.approx(0.25)


def test_metadata_line_roundtrips() -> None:
    line = _format_metadata_line("ses_1", {"input_tokens": 5}, 0.25)

    parsed = parse_opencode_review_metadata(f"noise\n{line}\nmore noise")

    assert parsed == {
        "session_id": "ses_1",
        "usage": {"input_tokens": 5},
        "cost_usd": 0.25,
    }
    assert parse_opencode_review_metadata("no metadata here") == {}


def test_metadata_parser_prefers_final_driver_record() -> None:
    child_line = _format_metadata_line("ses_child", {"input_tokens": 1}, 0.1)
    driver_line = _format_metadata_line("ses_driver", {"input_tokens": 2}, 0.2)

    assert parse_opencode_review_metadata(f"{child_line}{driver_line}") == {
        "session_id": "ses_driver",
        "usage": {"input_tokens": 2},
        "cost_usd": 0.2,
    }


def test_metadata_parser_requires_prefix_at_line_start() -> None:
    indented = "  " + _format_metadata_line("ses_x", {}, None).rstrip("\n")

    assert parse_opencode_review_metadata(f"noise\n{indented}\n") == {}


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


def test_completed_unmarked_response_is_recovered_from_export(monkeypatch) -> None:
    text = "[P1] Rejected reservation changes stock."
    events = [
        {"type": "step_start", "sessionID": "ses_1"},
        {"type": "text", "part": {"text": text}},
        {"type": "step_finish", "part": {"reason": "stop"}},
    ]
    stream = _parse_event_stream("\n".join(map(json.dumps, events)))
    assert stream["session_id"] == "ses_1"
    assert stream["reviewer_output"] is None
    # OpenCode 1.18.30 exports a final assistant response with finish="stop".
    payload = {
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
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, json.dumps(payload)
        ),
    )
    assert _exported_review_text("opencode", "ses_1", Path.cwd()) == text


def test_driver_stdio_roundtrips_unicode(monkeypatch) -> None:
    monkeypatch.setenv("PYTHONIOENCODING", "cp1252")
    text = "Review: café → 修复"
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            f"import sys; expected={ascii(text)}; assert sys.stdin.read() == expected; sys.stdout.write(expected)",
        ],
        input=text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=opencode_review_env(),
        check=True,
    )
    assert proc.stdout == text
