"""Stage reliability-closure DEV-2: OpenSandbox stream reassembly.

The defect: ``OpenSandboxProvider._execution_text`` joined ``OutputMessage`` fragments with ``""``,
so every command's stdout arrived as one collapsed line. Stage P1b hit it live — the Main itself
reported "The `cat` output has lost newlines in some places".

The contract these tests hold the adapter to is the installed SDK's, not an invented one:
``opensandbox==1.2.0`` models each stream as ``list[OutputMessage]`` where ``text`` is a fragment
without a line terminator, and its own ``_format_output_messages`` inserts a separator only when
neither side of a boundary already carries one. ``test_matches_the_sdk_formatter`` runs the SDK's
function over the same fragments, so a future SDK change that alters the rule fails here instead of
silently disagreeing.
"""

from __future__ import annotations

from typing import Any

import pytest

from atlasos.sandbox.opensandbox_provider import _join_log_messages

SDK_MESSAGES = pytest.importorskip("opensandbox.models.execd")


class _Message:
    """Stand-in for ``opensandbox.models.execd.OutputMessage`` (only ``text`` is read)."""

    def __init__(self, text: str) -> None:
        self.text = text


def _join(*fragments: str) -> str:
    return _join_log_messages([_Message(item) for item in fragments])


# ---------------------------------------------------------------- structure


def test_single_line_output() -> None:
    assert _join("hello") == "hello"


def test_multi_line_output_fragments_become_lines() -> None:
    assert _join("README.md", "app/calc.py", "tests/test_public.py") == (
        "README.md\napp/calc.py\ntests/test_public.py"
    )


def test_fragment_that_already_ends_with_newline_is_not_doubled() -> None:
    assert _join("first\n", "second") == "first\nsecond"


def test_fragment_that_already_starts_with_newline_is_not_prefixed() -> None:
    assert _join("first", "\nsecond") == "first\nsecond"


def test_blank_fragment_does_not_add_an_extra_separator() -> None:
    # A trailing empty message is what the server sends for a blank final line.
    assert _join("a", "", "b") == "a\nb"


def test_empty_output() -> None:
    assert _join_log_messages([]) == ""


def test_stderr_stream_uses_the_same_rule() -> None:
    assert _join("boom: missing config", "Traceback (most recent call last):") == (
        "boom: missing config\nTraceback (most recent call last):"
    )


def test_python_traceback_keeps_its_lines() -> None:
    traceback = [
        "Traceback (most recent call last):",
        '  File "app.py", line 3, in <module>',
        "ValueError: bad input",
    ]
    assert _join(*traceback) == "\n".join(traceback)


def test_git_diff_keeps_its_lines() -> None:
    diff = [
        "diff --git a/app/calc.py b/app/calc.py",
        "index 1234567..89abcde 100644",
        "--- a/app/calc.py",
        "+++ b/app/calc.py",
        "@@ -1,2 +1,5 @@",
        " def add(a, b):",
        "+    return a - b",
    ]
    assert _join(*diff) == "\n".join(diff)


def test_carriage_return_only_progress_is_normalized_like_the_sdk() -> None:
    # ``\r``-driven progress is normalized to lines, because that is exactly what the SDK's own
    # renderer does and Atlas keeps one rule rather than two.
    assert _join("10%\r20%\r100%") == "10%\n20%\n100%"


def test_crlf_is_normalized_to_lf() -> None:
    assert _join("first\r\n", "second") == "first\nsecond"


def test_trailing_newline_is_preserved_when_the_server_sends_one() -> None:
    # Atlas keeps command output as data (an exported patch), so it does not strip it the way the
    # SDK's display renderer does.
    assert _join("hello\n") == "hello\n"


# ------------------------------------------------- SDK contract conformance


def test_matches_the_sdk_formatter() -> None:
    """The same fragment list, the same separators the SDK's own renderer produces."""

    formatter = getattr(SDK_MESSAGES, "_format_output_messages", None)
    if formatter is None:  # pragma: no cover - only if the SDK removes its private helper
        pytest.skip("installed opensandbox exposes no _format_output_messages to compare against")

    cases: list[list[str]] = [
        ["hello"],
        ["one", "two", "three"],
        ["one\n", "two"],
        ["one", "\ntwo"],
        ["a", "", "b"],
        ["x\r", "y"],
        ["x\r\n", "y"],
    ]
    for fragments in cases:
        messages = [SDK_MESSAGES.OutputMessage(text=text, timestamp=0) for text in fragments]
        reference = formatter(messages)
        # The SDK renderer rstrips the trailing newline; Atlas deliberately does not (documented).
        assert _join(*fragments).rstrip("\n") == reference, fragments


# ---------------------------------------------------------------- adapter wiring


class _Logs:
    def __init__(self, stdout: list[str], stderr: list[str]) -> None:
        self.stdout = [_Message(item) for item in stdout]
        self.stderr = [_Message(item) for item in stderr]


class _Execution:
    def __init__(self, stdout: list[str], stderr: list[str]) -> None:
        self.logs = _Logs(stdout, stderr)
        self.exit_code = 0
        self.error: Any = None


def test_execution_text_reads_the_real_log_shape() -> None:
    from atlasos.sandbox.opensandbox_provider import OpenSandboxProvider

    execution = _Execution(["one", "two"], ["warn"])
    assert OpenSandboxProvider._execution_text(execution, "stdout") == "one\ntwo"
    assert OpenSandboxProvider._execution_text(execution, "stderr") == "warn"


def test_execution_text_tolerates_a_missing_logs_object() -> None:
    from atlasos.sandbox.opensandbox_provider import OpenSandboxProvider

    class _Bare:
        exit_code = 0
        error = None

    assert OpenSandboxProvider._execution_text(_Bare(), "stdout") == ""
