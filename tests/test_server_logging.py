from __future__ import annotations

import contextlib
import io
import unittest
from pathlib import Path
from unittest import mock
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import server


class _FakeLogger:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def new_turn_id(self) -> str:
        return "turn-test"

    def tool_call(self, name, args, turn_id) -> None:
        self.calls.append(("call", name, args, turn_id))

    def tool_result(self, name, content, turn_id, metadata=None) -> None:
        self.calls.append(("result", name, content, turn_id, metadata or {}))

    def tool_error(self, name, exc, turn_id, metadata=None) -> None:
        self.calls.append(("error", name, str(exc), turn_id, metadata or {}))


class ServerLoggingTests(unittest.TestCase):
    def test_logged_wrapper_adds_metadata_and_redacts_live_args(self) -> None:
        fake = _FakeLogger()

        @server._logged("demo")
        def demo(**kwargs):
            return (
                "[error] failed Authorization: Bearer result-secret\n"
                "[diagnostic] code=command_not_found layer=process retryable=false exit_code=127"
            )

        stderr = io.StringIO()
        with mock.patch.object(server, "_logger", fake), contextlib.redirect_stderr(stderr):
            result = demo(headers_json='{"Authorization":"Bearer very-secret"}')

        self.assertIn("code=command_not_found", result)
        call = next(item for item in fake.calls if item[0] == "call")
        self.assertEqual(call[2]["headers_json"], "<redacted>")
        logged = next(item for item in fake.calls if item[0] == "result")
        metadata = logged[4]
        self.assertEqual(metadata["status"], "error")
        self.assertEqual(metadata["diagnostic_code"], "command_not_found")
        self.assertEqual(metadata["exit_code"], 127)
        self.assertIn("duration_ms", metadata)
        self.assertNotIn("very-secret", stderr.getvalue())
        self.assertNotIn("result-secret", stderr.getvalue())
        self.assertIn("<redacted>", stderr.getvalue())

    def test_logged_wrapper_classifies_exception_leaf(self) -> None:
        fake = _FakeLogger()

        @server._logged("demo")
        def demo(**kwargs):
            raise ExceptionGroup("task group", [TimeoutError("child timed out")])

        stderr = io.StringIO()
        with mock.patch.object(server, "_logger", fake), contextlib.redirect_stderr(stderr):
            with self.assertRaises(ExceptionGroup):
                demo()

        logged = next(item for item in fake.calls if item[0] == "error")
        metadata = logged[4]
        self.assertEqual(metadata["diagnostic_code"], "timeout")
        self.assertTrue(metadata["retryable"])
        self.assertIn("TimeoutError: child timed out", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
