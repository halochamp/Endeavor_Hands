from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools import mcp_client
from tools._sandbox import DirectExecTestBackend


class MCPClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="endeavor-hands-mcp-", dir="/private/tmp")
        self.workspace = Path(self.temp.name)
        self.work_dir = self.workspace / "work"
        self._old_workspace = mcp_client.WORKSPACE
        self._old_work_dir = mcp_client._WORK_DIR
        mcp_client.WORKSPACE = str(self.workspace)
        mcp_client._WORK_DIR = self.work_dir

    def tearDown(self) -> None:
        mcp_client.WORKSPACE = self._old_workspace
        mcp_client._WORK_DIR = self._old_work_dir
        self.temp.cleanup()

    def _fake_stdio_server(self) -> Path:
        script = self.workspace / "fake_mcp_server.py"
        script.write_text(
            "from mcp.server.fastmcp import FastMCP\n"
            "mcp = FastMCP('fake')\n"
            "@mcp.tool()\n"
            "def ping(value: str = '') -> str:\n"
            "    return 'pong:' + value\n"
            "if __name__ == '__main__':\n"
            "    mcp.run()\n",
            encoding="utf-8",
        )
        return script

    def test_run_async_works_inside_an_existing_event_loop(self) -> None:
        async def outer() -> str:
            return mcp_client._run_async(lambda: asyncio.sleep(0, result="ok"))

        self.assertEqual(asyncio.run(outer()), "ok")

    def test_stdio_registration_list_and_call_from_running_loop(self) -> None:
        script = self._fake_stdio_server()

        # This suite itself is often launched through Hands' bash sandbox. macOS
        # refuses applying another sandbox profile from the MCP worker thread, so
        # inject the explicit test backend. Production always uses RealSandboxBackend;
        # there is no environment-variable or auto-detected unsandboxed fallback.
        old_backend = mcp_client._SANDBOX_BACKEND
        test_backend = DirectExecTestBackend()
        mcp_client._SANDBOX_BACKEND = test_backend
        try:
            registered = mcp_client.mcp_add_server.func(
                name="fake",
                command=sys.executable,
                args_json=json.dumps([str(script)]),
                cwd=str(self.workspace),
            )
            self.assertNotIn("[error]", registered, registered)
            self.assertTrue((self.work_dir / "tool_mcp" / "servers.json").exists())

            async def exercise() -> tuple[str, str]:
                listed = mcp_client.mcp_list_tools.func(server="fake")
                called = mcp_client.mcp_call_tool.func(
                    server="fake",
                    tool_name="ping",
                    arguments_json='{"value":"hello"}',
                )
                return listed, called

            listed, called = asyncio.run(exercise())
            self.assertIn("ping", listed)
            self.assertEqual(called, "pong:hello")
        finally:
            mcp_client._SANDBOX_BACKEND = old_backend

        self.assertTrue(test_backend.prepared_argv)
        self.assertFalse(any("sandbox-exec" in arg for call in test_backend.prepared_argv for arg in call))

    def test_stdio_rejects_relative_command_and_cwd_outside_workspace(self) -> None:
        with self.assertRaisesRegex(ValueError, "absolute executable path"):
            mcp_client._normalise_server_config(
                {"transport": "stdio", "command": "python3", "args": [], "cwd": str(self.workspace)}
            )

        outside = Path("/private/tmp")
        with self.assertRaisesRegex(ValueError, "cwd must be inside"):
            mcp_client._normalise_server_config(
                {"transport": "stdio", "command": sys.executable, "args": [], "cwd": str(outside)}
            )

    def test_stdio_args_are_direct_argv_not_shell_text(self) -> None:
        cfg = mcp_client._normalise_server_config(
            {
                "transport": "stdio",
                "command": sys.executable,
                "args": ["-c", "print('x; touch should-not-run')"],
                "cwd": str(self.workspace),
            }
        )
        self.assertEqual(cfg["command"], os.path.realpath(sys.executable))
        self.assertEqual(cfg["args"][1], "print('x; touch should-not-run')")


if __name__ == "__main__":
    unittest.main()
