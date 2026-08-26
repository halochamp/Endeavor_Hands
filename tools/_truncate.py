"""_truncate.py — shared output-cap-with-recovery-file logic for exec tools.

Extracted from python_exec.py (S58) so bash.py can share the same behavior
(save-to-file recovery instead of a blind cut) — see plan agent-bash-binary-balloon
Phase 1. Both tools call the SAME function; no dual-path.
"""
from __future__ import annotations
import uuid
from pathlib import Path


def truncate_with_save(
    out: str,
    max_chars: int,
    workspace: str,
    tool_name: str,
    extra_note: str = "",
    marker_first: bool = False,
    file_prefix: str | None = None,
    keep_tail: bool = False,
) -> str:
    """Cut `out` to `max_chars` and save the full untruncated text to a workspace
    recovery file, noting its path in the marker.

    `marker_first`: put the marker BEFORE the cut content instead of after.
    Use this when a caller may apply its own secondary hard cut on top of this
    result (e.g. tool_loop._bash_each's 2,000-char cap) — a trailing marker can
    get sliced off entirely, silently dropping the recovery-file path; a
    leading marker survives any such secondary cut.

    `file_prefix`: recovery-file basename prefix (default `_<tool_name>_output`).

    `keep_tail`: default False keeps only the head (original behavior). True
    splits the budget 70% head / 30% tail instead — build/test errors are almost
    always at the end of the output, so a head-only cut silently hides the one
    part that matters most while keeping noise from the start.
    """
    if len(out) <= max_chars:
        return out
    full_len = len(out)
    saved_path = ""
    prefix = file_prefix or f"_{tool_name}_output"
    try:
        save_to = Path(workspace) / f"{prefix}_{uuid.uuid4().hex[:8]}.txt"
        save_to.write_text(out, encoding="utf-8")
        saved_path = str(save_to)
    except Exception:
        pass  # best-effort — truncation still proceeds without the recovery file
    if keep_tail:
        head_budget = int(max_chars * 0.7)
        tail_budget = max_chars - head_budget
        head = out[:head_budget]
        head = head[:head.rfind("\n")] if "\n" in head else head
        tail = out[-tail_budget:]
        tail = tail[tail.find("\n") + 1:] if "\n" in tail else tail
        omitted = full_len - len(head) - len(tail)
        cut = f"{head}\n...[omitted {omitted} chars — full output saved separately, see marker]...\n{tail}"
    else:
        cut = out[:max_chars]
        cut = cut[:cut.rfind("\n")] if "\n" in cut else cut
    saved_line = f" Full output saved at: {saved_path}." if saved_path else ""
    marker = (
        f"...[{tool_name}] truncated: showing {len(cut)} of {full_len} chars.{saved_line}"
        + (f" {extra_note}" if extra_note else "")
    )
    return f"{marker}\n{cut}" if marker_first else f"{cut}\n{marker}"
