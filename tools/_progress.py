"""_progress.py — central progress reporting for long-running tools

Tools (web_search, browse_url, browser_use, _summarize, recall_web) emit progress
events through `progress(msg)`. If a callback is registered (e.g. by main.py
to update the Spinner sub-status), it receives the message. Otherwise the
message is written to stderr — useful when running scripts directly without UI.

Usage in tools:
    from tools._progress import progress
    progress("summarizing 1552 chars…")

Usage in UI layer (e.g. main.py):
    from tools._progress import set_callback
    set_callback(lambda msg: spinner.update_sub(msg))
"""
from __future__ import annotations
import sys
from contextvars import ContextVar
from typing import Callable


class ToolCancelled(BaseException):
    """Raised inside progress() when the user cancels a turn mid-tool.
    Extends BaseException (not Exception) so a tool's own `except Exception`
    cannot swallow it — it propagates up through ToolNode to _run_agent_sync.
    This is the cancel checkpoint for blocking tools (web_search, browse_url,
    summarize) that emit progress but never yield to a LangChain callback."""


# Module-level globals — used by the REST /chat path and CLI entry points.
_sub_callback: Callable[[str], None] | None = None
_phase_callback: Callable[[str], None] | None = None
_plan_callback: Callable[[str], None] | None = None

# Per-run context vars — set inside each agent thread so concurrent WS runs
# never cross-contaminate each other's progress callbacks.
# ThreadPoolExecutor.submit copies the submitter's context, so tools running
# in LangGraph's ToolNode inherit these from the _run_agent_sync thread.
_cv_sub: ContextVar[Callable[[str], None] | None] = ContextVar('_cv_sub', default=None)
_cv_phase: ContextVar[Callable[[str], None] | None] = ContextVar('_cv_phase', default=None)
_cv_plan: ContextVar[Callable[[str], None] | None] = ContextVar('_cv_plan', default=None)
# Per-run cancel predicate — returns True once the user cancels the turn.
# Checked at every progress() call so blocking tools interrupt at the next emit.
_cv_cancel: ContextVar[Callable[[], bool] | None] = ContextVar('_cv_cancel', default=None)


def set_run_callbacks(
    sub: Callable[[str], None] | None,
    phase_fn: Callable[[str], None] | None,
    plan: Callable[[str], None] | None,
    cancel_check: Callable[[], bool] | None = None,
) -> None:
    """Set per-run callbacks in the current thread's context.
    Call this at the top of each agent worker thread; LangGraph's ToolNode
    (ThreadPoolExecutor) copies context from the submitter, so all parallel
    tool calls inherit the same per-run slots without touching the globals.
    `cancel_check` (optional) is a predicate polled by progress() so blocking
    tools cancel at their next emit; omitted by REST/CLI paths (no cancel)."""
    _cv_sub.set(sub)
    _cv_phase.set(phase_fn)
    _cv_plan.set(plan)
    _cv_cancel.set(cancel_check)


def set_callback(fn: Callable[[str], None] | None) -> None:
    """Register global sub-status callback. Used by REST /chat and CLI paths."""
    global _sub_callback
    _sub_callback = fn


def set_phase_callback(fn: Callable[[str], None] | None) -> None:
    """Register global phase callback. Used by REST /chat and CLI paths."""
    global _phase_callback
    _phase_callback = fn


def set_plan_callback(fn: Callable[[str], None] | None) -> None:
    """Register global plan callback. Used by REST /chat and CLI paths."""
    global _plan_callback
    _plan_callback = fn


def get_callbacks() -> tuple:
    """Snapshot current global (sub, phase, plan) callbacks for save/restore."""
    return _sub_callback, _phase_callback, _plan_callback


def check_cancel() -> None:
    """Cancel checkpoint for tools. Raises ToolCancelled if the user cancelled.
    Call inside any tight loop that does NOT emit progress (progress() already
    calls this)."""
    chk = _cv_cancel.get()
    if chk is not None and chk():
        raise ToolCancelled("turn cancelled by user")


def progress(msg: str) -> None:
    """Emit sub-status. Thread-local ContextVar wins over global; stderr fallback.
    Doubles as a cancel checkpoint — raises ToolCancelled if the turn was
    cancelled, so blocking tools interrupt at their next progress() call."""
    check_cancel()
    cb = _cv_sub.get() or _sub_callback
    if cb is not None:
        try:
            cb(msg)
            return
        except Exception:
            pass
    sys.stderr.write(f"   \033[2m⎿  {msg}\033[0m\n")
    sys.stderr.flush()


def emit_plan(plan_text: str) -> None:
    """Emit plan text. Thread-local ContextVar wins over global."""
    cb = _cv_plan.get() or _plan_callback
    if cb is not None:
        try:
            cb(plan_text)
        except Exception:
            pass


def phase(label: str) -> None:
    """Switch main phase label. Thread-local ContextVar wins over global. No fallback."""
    cb = _cv_phase.get() or _phase_callback
    if cb is not None:
        try:
            cb(label)
        except Exception:
            pass
