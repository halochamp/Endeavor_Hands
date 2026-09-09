from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools._sandbox import DirectExecTestBackend, RealSandboxBackend, build_sandbox_profile
from tools._safe_move_capability import trusted_safe_move_invocation


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
        self.assertIn('(allow file-write-unlink (subpath "/tmp/workspace/repo/.git"))', profile)

    def test_safe_move_classifier_allows_only_in_workspace_no_clobber_mv(self) -> None:
        with tempfile.TemporaryDirectory(prefix="endeavor-safe-move-", dir="/private/tmp") as td:
            workspace = Path(td)
            source = workspace / "source.txt"
            source.write_text("keep\n", encoding="utf-8")

            capability = trusted_safe_move_invocation("mv source.txt renamed.txt", td)
            self.assertIsNotNone(capability)
            assert capability is not None
            self.assertEqual(capability.source_paths, (str(source),))
            self.assertEqual(capability.argv[:2], ("/bin/mv", "-n"))
            self.assertEqual(capability.argv[-1], str(workspace / "renamed.txt"))

            existing = workspace / "existing.txt"
            existing.write_text("do not replace\n", encoding="utf-8")
            self.assertIsNone(trusted_safe_move_invocation("mv source.txt existing.txt", td))
            self.assertIsNone(trusted_safe_move_invocation("mv -f source.txt renamed.txt", td))
            self.assertIsNone(trusted_safe_move_invocation("mv source.txt ../escape.txt", td))
            self.assertIsNone(trusted_safe_move_invocation("mv source.txt renamed.txt; echo nope", td))

    def test_safe_move_profile_keeps_general_workspace_unlink_denied(self) -> None:
        with tempfile.TemporaryDirectory(prefix="endeavor-safe-move-profile-", dir="/private/tmp") as td:
            source = Path(td) / "source.txt"
            source.write_text("keep\n", encoding="utf-8")
            capability = trusted_safe_move_invocation("mv source.txt renamed.txt", td)
            self.assertIsNotNone(capability)
            assert capability is not None
            profile = build_sandbox_profile(td, extra_unlink_paths=capability.source_paths)
            self.assertIn(f'(deny file-write-unlink (subpath "{td}"))', profile)
            self.assertIn(f'(allow file-write-unlink (subpath "{source}"))', profile)
            self.assertNotIn(f'(allow file-write-unlink (subpath "{td}"))', profile)

    def test_bash_safe_move_uses_scoped_unlink_and_temp_python_cache(self) -> None:
        import config
        from tools import bash

        with tempfile.TemporaryDirectory(prefix="endeavor-safe-move-bash-", dir="/private/tmp") as td:
            source = Path(td) / "source.txt"
            source.write_text("keep\n", encoding="utf-8")
            captured: dict[str, object] = {}

            def fake_run(argv, **kwargs):
                captured["argv"] = tuple(argv)
                captured["profile"] = kwargs["profile"]
                captured["env"] = kwargs["env"]
                return subprocess.CompletedProcess(argv, 0, "ok", "")

            old_workspace = config.WORKSPACE
            config.WORKSPACE = td
            try:
                with mock.patch.object(bash._SANDBOX_BACKEND, "run", side_effect=fake_run):
                    result = bash._bash_impl("mv source.txt renamed.txt")
            finally:
                config.WORKSPACE = old_workspace

            self.assertEqual(result, "ok")
            self.assertEqual(captured["argv"][:2], ("/bin/mv", "-n"))
            self.assertIn(f'(deny file-write-unlink (subpath "{td}"))', captured["profile"])
            self.assertIn(f'(allow file-write-unlink (subpath "{source}"))', captured["profile"])
            self.assertTrue(
                str(captured["env"]["PYTHONPYCACHEPREFIX"]).startswith("/private/tmp/endeavor-hands-pycache-")
            )

    def test_python_exec_redirects_bytecode_cache_without_unlink_override(self) -> None:
        import config
        from tools import python_exec

        with tempfile.TemporaryDirectory(prefix="endeavor-python-env-", dir="/private/tmp") as td:
            captured: dict[str, object] = {}

            def fake_run(argv, **kwargs):
                captured["profile"] = kwargs["profile"]
                captured["env"] = kwargs["env"]
                return subprocess.CompletedProcess(argv, 0, "ok\n", "")

            old_workspace = config.WORKSPACE
            config.WORKSPACE = td
            try:
                with mock.patch.object(python_exec._SANDBOX_BACKEND, "run", side_effect=fake_run):
                    result = python_exec._python_exec_impl("print('ok')")
            finally:
                config.WORKSPACE = old_workspace

            self.assertEqual(result, "ok")
            self.assertIn(f'(deny file-write-unlink (subpath "{td}"))', captured["profile"])
            self.assertNotIn(f'(allow file-write-unlink (subpath "{td}"))', captured["profile"])
            self.assertTrue(
                str(captured["env"]["PYTHONPYCACHEPREFIX"]).startswith("/private/tmp/endeavor-hands-pycache-")
            )


if __name__ == "__main__":
    unittest.main()
