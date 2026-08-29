from __future__ import annotations

import unittest
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools._diagnostics import (
    append_diagnostic,
    classify_exception,
    classify_process_failure,
    flatten_exception,
    metadata_from_result,
    redact_args,
)


class DiagnosticsTests(unittest.TestCase):
    def test_nested_sandbox_is_not_reported_as_workspace_write_denial(self) -> None:
        diagnostic = classify_process_failure(
            1,
            "sandbox-exec: sandbox_apply: Operation not permitted",
            command="sandbox-exec -f nested.sb true",
            workspace="/Users/example/Desktop",
        )
        self.assertIsNotNone(diagnostic)
        assert diagnostic is not None
        self.assertEqual(diagnostic.code, "sandbox_nested")
        self.assertEqual(diagnostic.layer, "sandbox")
        self.assertFalse(diagnostic.retryable)
        rendered = append_diagnostic("[error] failed", diagnostic)
        self.assertIn("code=sandbox_nested", rendered)
        self.assertIn("nested sandboxing", rendered)

    def test_generic_sandbox_denial_stays_distinct(self) -> None:
        diagnostic = classify_process_failure(
            1,
            "mv: /protected/path: Operation not permitted",
            workspace="/workspace",
        )
        self.assertIsNotNone(diagnostic)
        assert diagnostic is not None
        self.assertEqual(diagnostic.code, "sandbox_policy_denied")

    def test_explicit_sandbox_exec_permission_denial_is_policy_denied(self) -> None:
        diagnostic = classify_process_failure(1, "sandbox-exec: deny: Permission denied")
        self.assertEqual(diagnostic.code, "sandbox_policy_denied")  # type: ignore[union-attr]
        self.assertEqual(diagnostic.layer, "sandbox")  # type: ignore[union-attr]

    def test_os_permission_denied_is_not_labeled_sandbox_policy(self) -> None:
        diagnostic = classify_process_failure(1, "tool: file: Permission denied")
        self.assertEqual(diagnostic.code, "os_permission_denied")  # type: ignore[union-attr]
        self.assertEqual(diagnostic.layer, "process")  # type: ignore[union-attr]

    def test_command_not_found_is_machine_readable(self) -> None:
        diagnostic = classify_process_failure(127, "bash: nope: command not found")
        self.assertEqual(diagnostic.code, "command_not_found")  # type: ignore[union-attr]
        self.assertEqual(diagnostic.exit_code, 127)  # type: ignore[union-attr]

    def test_exception_group_flattens_to_leaf_causes(self) -> None:
        exc = ExceptionGroup(
            "unhandled errors in a TaskGroup",
            [ValueError("bad child config"), RuntimeError("child transport closed")],
        )
        text = flatten_exception(exc)
        self.assertIn("ValueError: bad child config", text)
        self.assertIn("RuntimeError: child transport closed", text)
        self.assertNotIn("TaskGroup", text)

    def test_result_metadata_preserves_artifact_and_exit_code(self) -> None:
        text = (
            "...[bash] truncated: showing 10 of 20 chars. Full output saved at: /tmp/out.txt.\n"
            "[error] exited 7, no output\n"
            "[diagnostic] code=process_exit_nonzero layer=process retryable=false exit_code=7"
        )
        meta = metadata_from_result(text)
        self.assertEqual(meta["status"], "error")
        self.assertEqual(meta["diagnostic_code"], "process_exit_nonzero")
        self.assertEqual(meta["exit_code"], 7)
        self.assertEqual(meta["artifact_path"], "/tmp/out.txt")

    def test_existing_permission_gate_gets_metadata_without_text_change(self) -> None:
        meta = metadata_from_result("[permission_required] nonce=abc")
        self.assertEqual(meta["diagnostic_code"], "permission_gate")
        self.assertFalse(meta["retryable"])

    def test_background_job_metadata_extracts_job_id_and_exit_code(self) -> None:
        meta = metadata_from_result(
            "job deadbeef [exited] exit_code=3\n"
            "[diagnostic] code=process_exit_nonzero layer=process retryable=false exit_code=3"
        )
        self.assertEqual(meta["job_id"], "deadbeef")
        self.assertEqual(meta["exit_code"], 3)
        self.assertEqual(meta["diagnostic_code"], "process_exit_nonzero")

    def test_sensitive_logging_args_are_redacted(self) -> None:
        safe = redact_args(
            {
                "headers_json": '{"Authorization":"Bearer secret-token"}',
                "command": "API_KEY=very-secret run --flag",
                "arguments_json": '{"token":"child-secret","query":"hello"}',
            }
        )
        self.assertEqual(safe["headers_json"], "<redacted>")
        self.assertNotIn("very-secret", safe["command"])
        self.assertNotIn("child-secret", safe["arguments_json"])
        self.assertIn("hello", safe["arguments_json"])

    def test_timeout_exception_is_retryable(self) -> None:
        diagnostic = classify_exception(TimeoutError("slow child"), layer="mcp")
        self.assertEqual(diagnostic.code, "timeout")
        self.assertTrue(diagnostic.retryable)


if __name__ == "__main__":
    unittest.main()
