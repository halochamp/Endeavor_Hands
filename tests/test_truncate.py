from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools import _truncate


class TruncateRecoveryPathTests(unittest.TestCase):
    def test_default_recovery_dir_is_project_root_work(self) -> None:
        self.assertEqual(_truncate._WORK_DIR, PROJECT_ROOT / "work")

    def test_truncated_output_uses_work_dir_not_workspace(self) -> None:
        with tempfile.TemporaryDirectory(prefix="endeavor-truncate-") as temp:
            root = Path(temp)
            fake_workspace = root / "workspace"
            fake_workspace.mkdir()
            expected_work = root / "work"

            old_work_dir = _truncate._WORK_DIR
            _truncate._WORK_DIR = expected_work
            try:
                result = _truncate.truncate_with_save(
                    "line\n" * 100,
                    80,
                    str(fake_workspace),
                    "bash",
                    marker_first=True,
                )
            finally:
                _truncate._WORK_DIR = old_work_dir

            files = list(expected_work.glob("_bash_output_*.txt"))
            self.assertEqual(len(files), 1)
            self.assertFalse(list(fake_workspace.glob("_bash_output_*.txt")))
            self.assertIn(str(files[0]), result)
            self.assertEqual(files[0].read_text(encoding="utf-8"), "line\n" * 100)


if __name__ == "__main__":
    unittest.main()
