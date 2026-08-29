from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from unittest import mock
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools._sandbox import DirectExecTestBackend, RealSandboxBackend, build_sandbox_profile


class SandboxBackendTests(unittest.TestCase):
    def test_production_backend_constructs_real_sandbox_exec_argv(self) -> None:
        backend = RealSandboxBackend("/usr/bin/sandbox-exec")
        captured: dict[str, object] = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = argv
            captured["kwargs"] = kwargs
            return subprocess.CompletedProcess(argv, 0, "ok", "")

        with mock.patch("tools._sandbox.subprocess.run", side_effect=fake_run):
            result = backend.run(
                ["/bin/echo", "hello"],
                profile="(version 1)\n(allow default)\n",
                capture_output=True,
                text=True,
            )

        self.assertEqual(result.returncode, 0)
        argv = captured["argv"]
        self.assertEqual(argv[0], "/usr/bin/sandbox-exec")
        self.assertEqual(argv[1], "-f")
        self.assertTrue(str(argv[2]).endswith(".sb"))
        self.assertEqual(argv[3:], ["/bin/echo", "hello"])
        self.assertFalse(Path(argv[2]).exists())

    def test_direct_test_backend_never_inserts_sandbox_exec(self) -> None:
        backend = DirectExecTestBackend()
        captured: dict[str, object] = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = argv
            return subprocess.CompletedProcess(argv, 0, "ok", "")

        with mock.patch("tools._sandbox.subprocess.run", side_effect=fake_run):
            backend.run(
                ["/bin/echo", "hello"],
                profile="ignored",
                capture_output=True,
                text=True,
            )

        self.assertEqual(captured["argv"], ["/bin/echo", "hello"])
        self.assertEqual(backend.prepared_argv, [("/bin/echo", "hello")])

    def test_production_backend_does_not_fall_back_to_direct_exec(self) -> None:
        backend = RealSandboxBackend("/definitely/missing/sandbox-exec")
        with self.assertRaises(FileNotFoundError):
            backend.run(
                ["/bin/echo", "must-not-run-directly"],
                profile="(version 1)\n(allow default)\n",
                capture_output=True,
                text=True,
            )

    def test_profile_preserves_workspace_unlink_guard_and_scoped_override(self) -> None:
        profile = build_sandbox_profile(
            "/tmp/workspace",
            extra_unlink_paths=("/tmp/workspace/repo/.git",),
        )
        self.assertIn('(deny file-write-unlink (subpath "/tmp/workspace"))', profile)
        self.assertIn('(allow file-write-unlink (subpath "/private/tmp/workspace/repo/.git"))', profile)


if __name__ == "__main__":
    unittest.main()
