from __future__ import annotations

import ast
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
COMPUTER_HELPERS = (
    ROOT / "tools" / "_mac_input.py",
    ROOT / "tools" / "_ocr.py",
    ROOT / "tools" / "_accessibility.py",
)


def _is_subprocess_run(node: ast.Call) -> bool:
    return (
        isinstance(node.func, ast.Attribute)
        and node.func.attr == "run"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "subprocess"
    )


def _is_subprocess_devnull(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "DEVNULL"
        and isinstance(node.value, ast.Name)
        and node.value.id == "subprocess"
    )


class ComputerSubprocessStdinTests(unittest.TestCase):
    def test_all_computer_helper_subprocesses_isolate_mcp_stdin(self) -> None:
        total_calls = 0
        for path in COMPUTER_HELPERS:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and _is_subprocess_run(node)]
            self.assertGreater(len(calls), 0, f"expected subprocess.run calls in {path.name}")
            total_calls += len(calls)
            for call in calls:
                stdin_kw = next((kw for kw in call.keywords if kw.arg == "stdin"), None)
                self.assertIsNotNone(
                    stdin_kw,
                    f"{path.name}:{call.lineno} subprocess.run inherits MCP stdin",
                )
                self.assertTrue(
                    _is_subprocess_devnull(stdin_kw.value),
                    f"{path.name}:{call.lineno} subprocess.run stdin must be subprocess.DEVNULL",
                )

        self.assertGreaterEqual(total_calls, 11)


if __name__ == "__main__":
    unittest.main()
