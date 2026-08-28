from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import config
from tools.bash import _build_sandbox_profile
from tools.git import _git_impl


class GuardedGitToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_workspace = config.WORKSPACE
        self.temp = tempfile.TemporaryDirectory(prefix="endeavor-hands-git-", dir="/private/tmp")
        self.workspace = Path(self.temp.name)
        self.repo = self.workspace / "repo"
        self.repo.mkdir()
        config.WORKSPACE = str(self.workspace)

        subprocess.run(["git", "init", str(self.repo)], check=True, capture_output=True, text=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "config", "user.name", "Endeavor Hands Test"],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "config", "user.email", "test@example.invalid"],
            check=True,
            capture_output=True,
            text=True,
        )

    def tearDown(self) -> None:
        config.WORKSPACE = self._old_workspace
        self.temp.cleanup()

    def test_profile_keeps_source_unlink_denied_but_can_scope_git_metadata(self) -> None:
        git_dir = self.repo / ".git"
        profile = _build_sandbox_profile(
            str(self.workspace),
            extra_unlink_paths=(str(git_dir),),
        )
        self.assertIn(f'(deny file-write-unlink (subpath "{self.workspace}"))', profile)
        real_git_dir = os.path.realpath(git_dir)
        self.assertIn(f'(allow file-write-unlink (subpath "{real_git_dir}"))', profile)

        source = self.repo / "keep.txt"
        source.write_text("keep\n", encoding="utf-8")
        with tempfile.NamedTemporaryFile(mode="w", suffix=".sb", delete=False) as handle:
            handle.write(profile)
            profile_path = handle.name
        try:
            result = subprocess.run(
                ["sandbox-exec", "-f", profile_path, "rm", str(source)],
                capture_output=True,
                text=True,
            )
        finally:
            os.unlink(profile_path)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(source.exists())

    def test_add_and_commit_can_update_git_metadata(self) -> None:
        source = self.repo / "tracked.txt"
        source.write_text("hello\n", encoding="utf-8")

        add_result = _git_impl("add", repo=str(self.repo), paths=["tracked.txt"])
        self.assertNotIn("[error]", add_result, add_result)

        commit_result = _git_impl("commit", repo=str(self.repo), message="test guarded commit")
        self.assertNotIn("[error]", commit_result, commit_result)

        head = subprocess.run(
            ["git", "-C", str(self.repo), "log", "-1", "--format=%s"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.assertEqual(head, "test guarded commit")

    def test_zero_byte_stale_index_lock_is_moved_not_deleted(self) -> None:
        source = self.repo / "tracked.txt"
        source.write_text("v1\n", encoding="utf-8")
        self.assertNotIn("[error]", _git_impl("add", repo=str(self.repo), paths=["tracked.txt"]))
        self.assertNotIn("[error]", _git_impl("commit", repo=str(self.repo), message="initial"))

        source.write_text("v2\n", encoding="utf-8")
        lock = self.repo / ".git" / "index.lock"
        lock.write_bytes(b"")
        old = time.time() - 60
        os.utime(lock, (old, old))

        result = _git_impl("add", repo=str(self.repo), paths=["tracked.txt"])
        self.assertNotIn("[error]", result, result)
        self.assertIn("moved stale index lock", result)
        self.assertFalse(lock.exists())
        backups = list((self.repo / ".git").glob("index.lock.stale-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), b"")

    def test_add_rejects_implicit_whole_repository_staging(self) -> None:
        result = _git_impl("add", repo=str(self.repo), paths=[])
        self.assertIn("requires explicit paths", result)

    def test_repo_outside_workspace_is_rejected(self) -> None:
        result = _git_impl("status", repo="/tmp")
        self.assertIn("repository must be inside the approved workspace", result)


if __name__ == "__main__":
    unittest.main()
