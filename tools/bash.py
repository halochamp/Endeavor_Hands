from __future__ import annotations
import os
import subprocess
import tempfile
from tools._truncate import truncate_with_save

_SANDBOX_DENY_MARKERS = ("Operation not permitted", "Permission denied", "Read-only file system")


def _classify_bash_error(returncode: int, stderr: str, command: str, workspace: str) -> str | None:
    """Evidence-gated recovery hint for a failed bash call — mirrors python_exec's
    _classify_error (a wrong hint is worse than no hint, so each branch checks the
    actual signal, not just a guess from the command text)."""
    if returncode == 127:
        return "command not found — check spelling, or use an absolute path / `which <name>` first."
    if returncode == 126:
        return "permission denied executing that file — check it's executable (chmod +x) or not a directory."
    if "-10004" in stderr or ("osascript" in command and "System Events" in command):
        return ("GUI scripting via osascript is blocked by this tool's sandbox — use the `computer` "
                "tool for clicking/typing/keystrokes instead.")
    if returncode != 0 and any(m in stderr for m in _SANDBOX_DENY_MARKERS):
        return (f"write/access denied by the sandbox — only workspace/ ({workspace}) and /tmp are "
                f"writable; save output there instead of ~/Desktop, ~/Documents, etc.")
    return None


def _build_sandbox_profile(
    workspace: str,
    extra_write_paths: tuple[str, ...] = (),
    extra_read_paths: tuple[str, ...] = (),
    extra_unlink_paths: tuple[str, ...] = (),
) -> str:
    """สร้าง macOS sandbox-exec profile — allow default, deny writes นอก workspace

    extra_write_paths: subpath เพิ่มที่อนุญาตให้เขียน (เช่น skills/ สำหรับ python_exec)
    — append หลัง deny block → last-match wins → override deny Desktop
    """
    home = os.path.expanduser("~")
    extra = "".join(f' (subpath "{os.path.realpath(p)}")' for p in extra_write_paths)
    extra_read = "".join(f' (subpath "{os.path.realpath(p)}")' for p in extra_read_paths)
    extra_unlink = "".join(f' (subpath "{os.path.realpath(p)}")' for p in extra_unlink_paths)
    return f"""(version 1)
(allow default)

; deny writes ไปยัง paths อันตราย
(deny file-write*
  (subpath "/etc") (subpath "/private/etc")
  (subpath "/usr") (subpath "/bin") (subpath "/sbin")
  (subpath "/System") (subpath "/Library") (subpath "/Applications")
  (subpath "{home}/Desktop")
  (subpath "{home}/Documents") (subpath "{home}/Downloads")
  (subpath "{home}/Movies") (subpath "{home}/Music") (subpath "{home}/Pictures")
  (subpath "{home}/.ssh") (subpath "{home}/.aws")
  (subpath "{home}/.config") (subpath "{home}/.gnupg")
  (subpath "{home}/Library")
)

; deny read credentials + session history
(deny file-read*
  (subpath "{home}/.ssh")
  (subpath "{home}/.aws")
  (subpath "{home}/.gnupg")
  (subpath "{home}/.claude")
  (subpath "{home}/.config")
)

; workspace + /tmp + extra — allow ทีหลัง (last-match wins) override deny Desktop
(allow file-write* (subpath "{workspace}") (subpath "/private/tmp"){extra})
(allow file-read*  (subpath "{workspace}"){extra_read})

; The workspace may be edited, but files must never be removed by shell/Python tools.
; Keep this after the workspace allow rule so it takes precedence there too.
(deny file-write-unlink (subpath "{workspace}"))

; Guarded capabilities may opt into unlink/rename for narrowly-scoped metadata paths.
{f'(allow file-write-unlink{extra_unlink})' if extra_unlink else ''}
"""


def _bash_impl(command: str, timeout: int = 30) -> str:
    from config import WORKSPACE

    if not command:
        return "[error] command is required"

    # Block pure-echo progress markers — model uses bash('echo "..."') as step announcements
    # during plan execution. Only block when echo has no redirect / pipe / variable (those are
    # legitimate: echo "x" > file.txt, echo $PATH, echo "x" | grep ...).
    _cmd = command.strip()
    if _cmd.startswith("echo ") and not any(c in _cmd for c in (">", "|", "$", "&", "`")):
        return ""

    profile_path = None
    try:
        profile = _build_sandbox_profile(WORKSPACE)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".sb", delete=False) as f:
            f.write(profile)
            profile_path = f.name
        try:
            result = subprocess.run(
                ["sandbox-exec", "-f", profile_path, "bash", "-c", command],
                capture_output=True, text=True,
                timeout=timeout, cwd=WORKSPACE, stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired as e:
            # Return what ran before the timeout instead of discarding it — a command that
            # got most of the way there shouldn't force a blind full re-run from scratch.
            # e.stdout/e.stderr come back as bytes here even with text=True (subprocess
            # quirk on TimeoutExpired specifically, verified on Python 3.11.15) — decode
            # defensively rather than assuming either type.
            def _decode(x) -> str:
                if x is None:
                    return ""
                return x.decode("utf-8", errors="replace") if isinstance(x, bytes) else x
            partial = _decode(e.stdout)
            if e.stderr:
                partial += f"\n[stderr]\n{_decode(e.stderr)}"
            partial = partial.strip()
            note = f"[error] command timed out after {timeout}s"
            if partial:
                note += " — partial output before timeout:"
                # marker_first: same reason as the normal-path call below — tool_loop._bash_each
                # applies its own secondary cut on top of this result.
                partial = truncate_with_save(partial, 10_000, WORKSPACE, "bash",
                                              marker_first=True, keep_tail=True)
                return f"{note}\n{partial}"
            return note

        output = result.stdout or ""
        if result.stderr:
            output += f"\n[stderr]\n{result.stderr}"
        output = output.strip() or "(no output)"
        hint = _classify_bash_error(result.returncode, result.stderr or "", command, WORKSPACE)
        if hint:
            output += f"\n[hint] {hint}"
        # marker_first: tool_loop._bash_each applies its own secondary 2,000-char cut on top
        # of this result — a trailing marker could get sliced off, silently dropping the
        # recovery-file path. A leading marker survives that secondary cut.
        # keep_tail: build/test errors sit at the end of the output — a head-only cut hides
        # exactly the part that matters most.
        output = truncate_with_save(output, 10_000, WORKSPACE, "bash", marker_first=True, keep_tail=True)
        return output
    except FileNotFoundError:
        return "[error] sandbox-exec not found — macOS only"
    except Exception as e:
        return f"[error] bash failed: {e}"
    finally:
        if profile_path:
            try:
                os.unlink(profile_path)
            except Exception:
                pass
