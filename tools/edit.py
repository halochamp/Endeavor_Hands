from __future__ import annotations
import json
import os
import shutil
from pathlib import Path
from langchain_core.tools import tool
from ._safety import plan_write, resolve_path


def _normalize_lines(text: str) -> str:
    """Strip trailing whitespace per line — fixes whitespace mismatch on old_string."""
    return "\n".join(line.rstrip() for line in text.splitlines())


def _as_bool(v) -> bool:
    """Batch hunks arrive as plain dicts (not pydantic-validated field-by-field), so a
    local model can hand back a JSON string like "false" for a bool field — plain
    bool(v) would treat that non-empty string as truthy. Coerce defensively."""
    if isinstance(v, str):
        return v.strip().lower() not in ("", "false", "0", "no")
    return bool(v)


def _as_int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _line_count(s: str) -> int:
    """Logical line count for a report message — a trailing "\\n" terminates the
    last line, it does not start an extra empty one, so plain count('\\n')+1 is
    off by one for any string that ends with a newline."""
    if not s:
        return 0
    n = s.count("\n")
    return n if s.endswith("\n") else n + 1


def _join_with_boundary(before: list[str], middle: str, after: list[str]) -> str:
    """Join keepends-lines + inserted text + more keepends-lines. Ensures a newline
    separates `before`'s last line from `middle`/`after` when it's missing one (the
    file's previous final line had no trailing newline) — otherwise two lines would
    be silently fused together. Also newline-terminates `middle` itself whenever it's
    non-empty, unconditionally (including at true EOF, `after` empty) — a
    replace/insert landing at the end of the file must not silently strip the file's
    trailing newline convention."""
    if before and not before[-1].endswith("\n") and (middle or after):
        before = before[:-1] + [before[-1] + "\n"]
    if middle and not middle.endswith("\n"):
        middle = middle + "\n"
    return "".join(before) + middle + "".join(after)


def _apply_line_hunk(content: str, line_start: int, line_end: int, new_string: str) -> tuple[str | None, str | None, str]:
    """LINE mode: line_end>=line_start replaces that inclusive 1-indexed range
    (empty new_string deletes it); line_end omitted (0) inserts new_string as new
    line(s) immediately after line_start."""
    lines = content.splitlines(True)
    n_lines = len(lines)
    if line_start < 1:
        return None, f"line_start must be >= 1 (got {line_start})", ""
    if line_start > n_lines:
        return None, f"line_start {line_start} is beyond end of file ({n_lines} lines)", ""
    if line_end and line_end < line_start:
        return None, f"line_end ({line_end}) must be >= line_start ({line_start})", ""

    if line_end and line_end > 0:
        end_idx = min(line_end, n_lines)
        before = lines[:line_start - 1]
        after = lines[end_idx:]
        new_content = _join_with_boundary(before, new_string, after)
        if not new_string:
            desc = f"deleted lines {line_start}-{end_idx}"
        else:
            desc = f"replaced lines {line_start}-{end_idx} with {_line_count(new_string)} line(s)"
        return new_content, None, desc

    if not new_string:
        return None, (
            "insert mode (line_end omitted) requires non-empty new_string — "
            "use line_end=line_start with empty new_string to delete a line instead"
        ), ""
    before = lines[:line_start]
    after = lines[line_start:]
    new_content = _join_with_boundary(before, new_string, after)
    desc = f"inserted {_line_count(new_string)} line(s) after line {line_start}"
    return new_content, None, desc


def _apply_hunk(content: str, hunk: dict) -> tuple[str | None, str | None, str]:
    """Apply one hunk to `content` in memory. Returns (new_content, error, description);
    on error new_content is None. The single code path every mode (STRING/LINE/BATCH)
    routes through — one shared place for match/replace logic, not three."""
    line_start = _as_int(hunk.get("line_start") or 0)
    if line_start != 0:
        # Route any explicitly-set line_start (including invalid negatives) into LINE
        # mode so _apply_line_hunk's bounds-check reports it, instead of falling
        # through to STRING mode's "old_string is required" — misleading when the
        # caller did set line_start, just to an invalid value.
        return _apply_line_hunk(content, line_start, _as_int(hunk.get("line_end") or 0), hunk.get("new_string") or "")

    old_string = hunk.get("old_string") or ""
    new_string = hunk.get("new_string") or ""
    replace_all = _as_bool(hunk.get("replace_all", False))
    near_line = _as_int(hunk.get("near_line") or 0)

    if not old_string:
        return None, "old_string is required (or set line_start for LINE mode)", ""

    count = content.count(old_string)
    if count == 0:
        norm_content = _normalize_lines(content)
        norm_old = _normalize_lines(old_string)
        if norm_old in norm_content:
            # Map match back to original lines — avoid writing normalized whole file
            norm_idx = norm_content.index(norm_old)
            start_line = norm_content[:norm_idx].count('\n')
            n_lines = norm_old.count('\n') + 1
            orig_lines = content.splitlines(True)
            orig_chunk = "".join(orig_lines[start_line:start_line + n_lines])
            if orig_chunk in content:
                old_string = orig_chunk
                count = content.count(orig_chunk)
    if count == 0:
        return None, (
            f"old_string not found.\nFile content (first 600 chars):\n{content[:600]}\n"
            "Copy old_string exactly from the content above."
        ), ""
    if count > 1 and not replace_all and near_line > 0:
        offsets = []
        start = 0
        for _ in range(count):
            idx = content.index(old_string, start)
            offsets.append(idx)
            start = idx + len(old_string)
        # read_file/grep number lines via splitlines() (which recognizes form-feed,
        # vertical-tab, NEL, LS/PS as breaks, not just \n); match that here so a
        # near_line the agent read from those tools lands on the same occurrence.
        # The "\x00" sentinel is required so a match sitting immediately after a
        # break is counted on the NEXT line — a bare splitlines() would drop the
        # trailing empty element and under-count by one. (edit reads via read_text()
        # which universal-newline-normalizes \r/\r\n→\n, so content never holds a
        # lone \r; the sentinel only reconciles \n + the exotic breaks.)
        lines = [len((content[:idx] + "\x00").splitlines()) for idx in offsets]
        best_i = min(range(len(lines)), key=lambda i: (abs(lines[i] - near_line), i))
        best_offset = offsets[best_i]
        actual_line = lines[best_i]
        new_content = content[:best_offset] + new_string + content[best_offset + len(old_string):]
        desc = f"replaced 1 occurrence near line {actual_line} (of {count} matches, requested near_line={near_line})"
        return new_content, None, desc
    if count > 1 and not replace_all:
        return None, (
            f"old_string found {count} times — use replace_all=true, "
            "make it more unique, or pass near_line=<line number> to target the closest match"
        ), ""
    new_content = (
        content.replace(old_string, new_string)
        if replace_all else content.replace(old_string, new_string, 1)
    )
    n = count if replace_all else 1
    desc = f"replaced {n} occurrence(s)"
    return new_content, None, desc


def _py_syntax_check(p: Path) -> str:
    """Inline syntax check after a successful .py edit — catches a syntax error in the
    same turn instead of costing a separate bash round-trip. Uses the builtin compile()
    (in-memory only, no bytecode file written — py_compile.compile() refuses cfile=
    os.devnull with FileExistsError, treating a non-regular-file target as suspicious).
    Never masks a successful edit: a checker failure of any other kind returns ""
    (silent), the write itself already succeeded."""
    try:
        source = p.read_text(encoding="utf-8")
        compile(source, str(p), "exec")
        return "\n✓ syntax OK"
    except SyntaxError as e:
        where = f" (line {e.lineno})" if e.lineno else ""
        return f"\n⚠ SYNTAX ERROR{where}: {e.msg}"
    except Exception:
        return ""


@tool
def edit(path: str, old_string: str = "", new_string: str = "", replace_all: bool = False,
          near_line: int = 0, line_start: int = 0, line_end: int = 0,
          edits: list | str | None = None) -> str:
    """Modify an EXISTING file. Three modes — pick one:
    STRING (default): old_string→new_string; old_string must be a unique substring of
    the file (or replace_all=true). near_line disambiguates when old_string matches >1 place.
    LINE: set line_start (1-indexed). line_end>=line_start replaces that inclusive range
    (empty new_string deletes it); line_end omitted inserts new_string as new line(s)
    after line_start (new_string required for insert).
    BATCH: pass edits=[{...}, ...] — each item uses the same fields as above (old_string
    OR line_start, not both). Applied in order to ONE file, atomically: any hunk failure
    discards the whole batch, nothing is written. A later hunk's line numbers are resolved
    against the file as already changed by earlier hunks in the SAME batch — order
    line-based hunks bottom-to-top (highest line_start first) if a batch mixes them.
    Use write_file for new files, or write_file with overwrite=true for full rewrites.
    Files OUTSIDE the workspace are never modified in place: the edit is applied to a
    sibling working copy "name.edited.ext" (created from the original on the first edit,
    reused by later edits) — the result reports the copy's path.
    .py files get an inline syntax check after a successful write (✓/⚠ appended to
    the result) — a syntax error is reported but NOT reverted; no separate bash
    round-trip needed just to catch it."""
    if not path:
        return "[error] path is required"

    if edits is not None:
        if isinstance(edits, str):
            # Some tool-calling models (observed live with this fork's production
            # model on a qwen3_coder-style parser) double-encode a nested array
            # argument as a JSON string instead of a native array. Recover instead
            # of bouncing a raw pydantic ValidationError back at the model.
            try:
                edits = json.loads(edits)
            except Exception:
                return "[error] edits must be a list of hunk objects — could not parse it as JSON"
        if not isinstance(edits, list) or not edits:
            return "[error] edits must be a non-empty list of hunk objects"
        for i, h in enumerate(edits):
            if not isinstance(h, dict):
                return f"[error] edits[{i}] must be an object, got {type(h).__name__}"
        hunks = edits
    else:
        if not old_string and not line_start:
            return "[error] old_string is required (or set line_start for LINE mode, or edits for BATCH mode)"
        hunks = [{
            "old_string": old_string, "new_string": new_string,
            "replace_all": replace_all, "near_line": near_line,
            "line_start": line_start, "line_end": line_end,
        }]

    target, err, note = plan_write(path)
    if err:
        return err
    try:
        p = Path(target)
        if note and not p.exists():
            src = Path(resolve_path(path))
            if not src.exists():
                return f"[error] file not found: {path}"
            shutil.copy2(src, p)
        if not p.exists():
            return f"[error] file not found: {path}"
        shown = str(p) if note else path
        cow = f"\nNOTE: {note}" if note else ""

        content = p.read_text(encoding="utf-8")
        descriptions = []
        for i, hunk in enumerate(hunks):
            new_content, herr, desc = _apply_hunk(content, hunk)
            if herr:
                prefix = f"edit batch failed at hunk {i}: " if edits is not None else ""
                return f"[error] {prefix}{herr}"
            content = new_content
            descriptions.append(desc)

        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, p)

        summary = "; ".join(descriptions)
        batch_note = f" ({len(hunks)} hunks)" if len(hunks) > 1 else ""
        tail = _py_syntax_check(p) if p.suffix == ".py" else ""
        return f"edited {shown} — {summary}{batch_note}{cow}{tail}"
    except Exception as e:
        return f"[error] edit failed: {e}"
