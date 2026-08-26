"""python_exec.py — run Python code in the same env as main.py

ทำไมไม่ใช้ bash + python3 -c:
- shell escaping ของ pandas/matplotlib code ฝันร้าย (nested quotes, df.query, regex)
- python3 ระบบไม่มี pandas/matplotlib — ต้องใช้ sys.executable (env mlx)
- pandas group/agg อาจใช้เวลา → timeout สูงกว่า bash

scope: data analysis + matplotlib plots. agent เขียน code เอง (27B ทำได้)
"""
from __future__ import annotations
import os
import re
import sys
import subprocess
import tempfile
import uuid
from pathlib import Path
from tools._progress import progress as _progress
from tools.bash import _build_sandbox_profile
from tools._truncate import truncate_with_save

_TIMEOUT_DEFAULT = 120  # data analysis ใช้เวลานานกว่า bash
_MAX_CHARS_DEFAULT = 10_000

# skills/ คือจุดเดียวที่ python_exec เขียน legitimate นอก workspace (/build skill เขียน skills/<name>.md)
_SKILLS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "skills")


def _classify_error(stderr: str) -> str | None:
    """Match the last traceback line against known exception types and return a short
    (<=200 char) recovery hint, or None when nothing matches — a wrong hint is worse than
    no hint, so each branch is gated on evidence specific to that exception, not just the
    exception name (e.g. KeyError only gets a pandas hint when pandas actually raised it)."""
    if not stderr or not stderr.strip():
        return None
    last_line = stderr.strip().splitlines()[-1]

    m = re.search(r"ModuleNotFoundError: No module named '([^']+)'", last_line)
    if m:
        return (f"'{m.group(1)}' is not installed; available libs: pandas/numpy/matplotlib/"
                f"scipy/sklearn/statsmodels. Do not pip install — retry with an available "
                f"library instead.")
    if "FileNotFoundError" in last_line:
        return "cwd is workspace/ — call bash (for example: rg --files or find) to confirm the exact filename, then retry."
    if "UnicodeDecodeError" in last_line:
        return "retry the read with encoding='utf-8-sig' or encoding='latin-1'."
    if "KeyError" in last_line and "pandas" in stderr:
        return "likely a wrong/misspelled column or index label — print(df.columns.tolist()) first, then retry."
    if "SyntaxError" in last_line:
        return "resend the complete corrected code from scratch — check quotes/indentation, don't patch a snippet."
    if "ParserError" in last_line and "pandas" in stderr:
        return "malformed CSV (ragged rows / wrong delimiter) — retry with on_bad_lines='skip' or check sep=."
    if "MemoryError" in last_line:
        return "data too large for memory — retry with usecols=[...] to load fewer columns, or chunksize= to stream it."
    if "AttributeError" in last_line and ("DataFrame" in stderr or "Series" in stderr):
        return "likely called a method that doesn't exist on this object — print(type(x)) and check df.dtypes/df.columns first."
    return None


def _python_exec_impl(code: str, timeout: int = _TIMEOUT_DEFAULT, max_chars: int = _MAX_CHARS_DEFAULT) -> str:
    from config import WORKSPACE
    if not code or not code.strip():
        return "[error] code is required"
    script = None
    profile_path = None
    try:
        Path(WORKSPACE).mkdir(parents=True, exist_ok=True)
        script = Path(WORKSPACE) / f"._exec_{uuid.uuid4().hex[:8]}.py"
        # OS-level sandbox เหมือน bash — เขียนได้เฉพาะ workspace + /tmp + skills/ (audit P1)
        profile = _build_sandbox_profile(WORKSPACE, extra_write_paths=(_SKILLS_DIR,))
        with tempfile.NamedTemporaryFile(mode="w", suffix=".sb", delete=False) as pf:
            pf.write(profile)
            profile_path = pf.name
        _progress("running code…")
        script.write_text(code, encoding="utf-8")
        try:
            try:
                proc = subprocess.run(
                    # -u: unbuffered stdout — without it, Python block-buffers stdout (~8KB)
                    # when writing to a pipe (not a tty), so a script killed mid-run on
                    # timeout has its already-printed output still sitting in the child's
                    # buffer, never flushed to us. Partial-output-on-timeout below is a
                    # no-op without this flag.
                    ["sandbox-exec", "-f", profile_path, sys.executable, "-u", str(script)],
                    capture_output=True, text=True,
                    timeout=timeout, cwd=WORKSPACE, stdin=subprocess.DEVNULL,
                )
            except subprocess.TimeoutExpired as e:
                # e.stdout/e.stderr come back as bytes here even with text=True (subprocess
                # quirk on TimeoutExpired specifically — see tools/bash.py's identical guard,
                # confirmed on Python 3.11.15) — decode defensively.
                def _decode(x) -> str:
                    if x is None:
                        return ""
                    return x.decode("utf-8", errors="replace") if isinstance(x, bytes) else x
                partial = _decode(e.stdout)
                if e.stderr:
                    partial += f"\n[stderr]\n{_decode(e.stderr)}"
                partial = partial.strip()
                note = f"[error] timed out after {timeout}s"
                if partial:
                    note += " — partial output before timeout:"
                    partial = truncate_with_save(partial, max_chars, WORKSPACE, "python_exec",
                                                  file_prefix="_exec_output",
                                                  marker_first=True, keep_tail=True)
                    return f"{note}\n{partial}"
                return note
        finally:
            try:
                script.unlink()
            except Exception:
                pass
            try:
                os.unlink(profile_path)
            except Exception:
                pass
        out = proc.stdout or ""
        if proc.stderr:
            out += f"\n[stderr]\n{proc.stderr}"
            hint = _classify_error(proc.stderr)
            if hint:
                out += f"\n[hint] {hint}"
        if proc.returncode != 0 and not out.strip():
            return (f"[error] exited {proc.returncode}, no output — add print() statements "
                     f"before the crash point and re-run to see where it failed")
        out = out.strip() or "(no output)"
        # keep_tail ONLY on a failed run: a crash traceback + [hint] sit at the END of
        # stdout+stderr, so a head-only cut would hide exactly the part that explains the
        # failure. A clean successful run stays head-only — the docstring's truncation
        # section (and its BAD/GOOD example) documents head-only behavior specifically for
        # that case, and this must not silently diverge from what it teaches.
        out = truncate_with_save(
            out, max_chars, WORKSPACE, "python_exec",
            file_prefix="_exec_output",
            keep_tail=bool(proc.returncode != 0 or proc.stderr),
            extra_note=(
                "Computations already run before this point (sums, means, groupby, describe) "
                "used the COMPLETE data regardless of this cap — only what's printed below the "
                "cap is affected."
            ),
        )
        return out
    except FileNotFoundError:
        return "[error] sandbox-exec not found — macOS only"
    except Exception as e:
        return f"[error] python_exec failed: {e}"
