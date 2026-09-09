from __future__ import annotations
import os
import subprocess
from tools._truncate import truncate_with_save
from tools._diagnostics import Diagnostic, append_diagnostic, classify_exception, classify_process_failure, flatten_exception
from tools._sandbox import RealSandboxBackend, build_sandbox_profile
from tools._safe_move_capability import trusted_safe_move_invocation

_SANDBOX_BACKEND = RealSandboxBackend()


def _classify_bash_error(returncode: int, stderr: str, command: str, workspace: str) -> str | None:
    """Backward-compatible hint facade over the shared structured classifier."""
    diagnostic = classify_process_failure(
        returncode, stderr, command=command, workspace=workspace,
    )
    return diagnostic.hint if diagnostic and diagnostic.hint else None


# Backward-compatible import surface for existing Git/tests.
_build_sandbox_profile = build_sandbox_profile

def _bash_impl(command: str, timeout: int = 30) -> str:
    from config import WORKSPACE

    if not command:
        return append_diagnostic(
            "[error] command is required",
            Diagnostic("tool_validation", "tool", False),
        )

    # Block pure-echo progress markers — model uses bash('echo "..."') as step announcements
    # during plan execution. Only block when echo has no redirect / pipe / variable (those are
    # legitimate: echo "x" > file.txt, echo $PATH, echo "x" | grep ...).
    _cmd = command.strip()
    if _cmd.startswith("echo ") and not any(c in _cmd for c in (">", "|", "$", "&", "`")):
        return ""

    try:
        move_capability = trusted_safe_move_invocation(command, WORKSPACE)
        if move_capability is not None:
            argv = list(move_capability.argv)
            profile = _build_sandbox_profile(
                WORKSPACE,
                extra_unlink_paths=move_capability.source_paths,
            )
        else:
            argv = ["bash", "-c", command]
            profile = _build_sandbox_profile(WORKSPACE)

        child_env = os.environ.copy()
        # Keep Python/import bytecode and compile caches out of the user workspace.
        # Python commonly installs .pyc files via atomic rename, which macOS Seatbelt
        # classifies as file-write-unlink just like deletion.
        child_env["PYTHONPYCACHEPREFIX"] = f"/private/tmp/endeavor-hands-pycache-{os.getuid()}"
        try:
            result = _SANDBOX_BACKEND.run(
                argv,
                profile=profile,
                capture_output=True, text=True,
                timeout=timeout, cwd=WORKSPACE, stdin=subprocess.DEVNULL,
                env=child_env,
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
            note = append_diagnostic(
                f"[error] command timed out after {timeout}s",
                Diagnostic("timeout", "process", True),
            )
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
        diagnostic = classify_process_failure(
            result.returncode, result.stderr or "", command=command, workspace=WORKSPACE,
        )
        hint = diagnostic.hint if diagnostic and diagnostic.hint else None
        if hint:
            output += f"\n[hint] {hint}"
        output = append_diagnostic(output, diagnostic)
        # marker_first: tool_loop._bash_each applies its own secondary 2,000-char cut on top
        # of this result — a trailing marker could get sliced off, silently dropping the
        # recovery-file path. A leading marker survives that secondary cut.
        # keep_tail: build/test errors sit at the end of the output — a head-only cut hides
        # exactly the part that matters most.
        output = truncate_with_save(output, 10_000, WORKSPACE, "bash", marker_first=True, keep_tail=True)
        return output
    except FileNotFoundError:
        return append_diagnostic(
            "[error] sandbox-exec not found — macOS only",
            Diagnostic("sandbox_unavailable", "sandbox", False),
        )
    except Exception as e:
        return append_diagnostic(
            f"[error] bash failed: {flatten_exception(e)}",
            classify_exception(e, layer="tool"),
        )
