from __future__ import annotations

import re
import sys
import tempfile
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import config
from tools import bash_bg
from tools._sandbox import DirectExecTestBackend


class BashBackgroundPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="endeavor-hands-bg-", dir="/private/tmp")
        self.workspace = Path(self.temp.name)
        self.work_dir = self.workspace / "work"
        self._old_workspace = config.WORKSPACE
        self._old_work_dir = bash_bg._WORK_DIR
        self._old_backend = bash_bg._SANDBOX_BACKEND
        config.WORKSPACE = str(self.workspace)
        bash_bg._WORK_DIR = self.work_dir
        bash_bg._SANDBOX_BACKEND = DirectExecTestBackend()

    def tearDown(self) -> None:
        for proc in list(bash_bg._PROCS.values()):
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=1)
                except Exception:
                    proc.kill()
        bash_bg._PROCS.clear()
        bash_bg._PROFILE_PATHS.clear()
        config.WORKSPACE = self._old_workspace
        bash_bg._WORK_DIR = self._old_work_dir
        bash_bg._SANDBOX_BACKEND = self._old_backend
        self.temp.cleanup()

    def test_registry_and_background_log_live_under_work(self) -> None:
        result = bash_bg.bash_bg.func(action="start", command="printf 'hello\\n'; sleep 0.2")
        self.assertNotIn("[error]", result, result)
        match = re.search(r"started job ([0-9a-f]+).*log: (.+)", result)
        self.assertIsNotNone(match, result)
        assert match is not None
        job_id, log_path = match.group(1), Path(match.group(2).strip())
        self.assertEqual(log_path.parent, self.work_dir)
        self.assertEqual(bash_bg._registry_path(), self.work_dir / "bash_jobs.json")
        self.assertTrue((self.work_dir / "bash_jobs.json").exists())

        time.sleep(0.3)
        status = bash_bg.bash_bg.func(action="status", job_id=job_id)
        self.assertIn("hello", status)


if __name__ == "__main__":
    unittest.main()
