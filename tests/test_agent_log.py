from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent_log import AgentLogger


class AgentLoggerTests(unittest.TestCase):
    def test_tool_call_redacts_credentials_and_result_metadata_is_additive(self) -> None:
        with tempfile.TemporaryDirectory(prefix="endeavor-log-", dir="/private/tmp") as tmp:
            path = Path(tmp) / "agent_activity.jsonl"
            logger = AgentLogger(path=str(path), maxlen=20)
            turn_id = "testturn"
            logger.tool_call(
                "mcp_add_server",
                {
                    "headers_json": '{"Authorization":"Bearer super-secret"}',
                    "command": "TOKEN=also-secret run",
                },
                turn_id,
            )
            logger.tool_result(
                "bash",
                "[error] sample Authorization: Bearer output-secret",
                turn_id,
                metadata={
                    "status": "error",
                    "duration_ms": 17,
                    "diagnostic_code": "process_exit_nonzero",
                    "retryable": False,
                    "exit_code": 2,
                },
            )
            entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

        call = entries[0]
        self.assertEqual(call["event"], "tool_call")
        self.assertEqual(call["args"]["headers_json"], "<redacted>")
        self.assertNotIn("also-secret", call["args"]["command"])

        result = entries[1]
        self.assertEqual(result["event"], "tool_result")
        self.assertEqual(result["name"], "bash")
        self.assertIn("[error] sample", result["content"])
        self.assertNotIn("output-secret", result["content"])
        self.assertIn("<redacted>", result["content"])
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["duration_ms"], 17)
        self.assertEqual(result["diagnostic_code"], "process_exit_nonzero")
        self.assertEqual(result["exit_code"], 2)


if __name__ == "__main__":
    unittest.main()
