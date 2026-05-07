from __future__ import annotations

import sys
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from review_suite_core.axi_output import emit_toon, toon_dumps, write_text


class _FakeBuffer:
    def __init__(self) -> None:
        self.chunks: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.chunks.append(data)


class _FailingStdout:
    def __init__(self, *, encoding: str = "cp1252") -> None:
        self.encoding = encoding
        self.buffer = _FakeBuffer()
        self.text_writes: list[str] = []

    def write(self, value: str) -> int:
        self.text_writes.append(value)
        raise UnicodeEncodeError("charmap", value, 0, 1, "character maps to <undefined>")


def test_emit_toon_falls_back_when_stdout_cannot_encode_unicode(monkeypatch) -> None:
    fake_stdout = _FailingStdout()
    monkeypatch.setattr(sys, "stdout", fake_stdout)

    emit_toon({"status": "ok", "summary": "ℹ️ About Codex in GitHub"})

    assert fake_stdout.text_writes
    rendered = b"".join(fake_stdout.buffer.chunks).decode("ascii")
    assert "status: ok" in rendered
    assert "\\u2139" in rendered or "\\N{INFORMATION SOURCE}" in rendered
    assert rendered.endswith("\n")


def test_emit_toon_separates_consecutive_payloads(monkeypatch) -> None:
    fake_stdout = _FakeBuffer()

    class _Stdout:
        encoding = "utf-8"
        buffer = fake_stdout

        def write(self, value: str) -> int:
            fake_stdout.write(value.encode("utf-8"))
            return len(value)

        def flush(self) -> None:
            return None

    monkeypatch.setattr(sys, "stdout", _Stdout())

    emit_toon({"To-Do": {"grade": "cmd"}})
    emit_toon({"To-Do": {"grade": "cmd"}})

    rendered = b"".join(fake_stdout.chunks).decode("utf-8")
    assert rendered == 'To-Do:\n  grade: cmd\nTo-Do:\n  grade: cmd\n'


def test_write_text_falls_back_when_stdout_cannot_encode_unicode(monkeypatch) -> None:
    fake_stdout = _FailingStdout()
    monkeypatch.setattr(sys, "stdout", fake_stdout)

    write_text("देवनागरी")

    rendered = b"".join(fake_stdout.buffer.chunks).decode("ascii")
    assert "\\u0926" in rendered or "\\N{DEVANAGARI LETTER DA}" in rendered


def test_toon_keeps_hyphenated_agent_facing_keys_readable() -> None:
    rendered = toon_dumps({"To-Do": {"grade": "cmd"}})

    assert rendered == "To-Do:\n  grade: cmd"
