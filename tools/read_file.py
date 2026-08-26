from __future__ import annotations
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path
from tools._progress import progress

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import (
    READ_FILE_MAX_CHARS as _MAX_CHARS,
    READ_FILE_MAX_BYTES as _MAX_FILE_BYTES,
    READ_FILE_AUDIO_VIDEO_MAX_BYTES as _MAX_AV_BYTES,
    WORKSPACE,
)
from tools._transcribe import _AUDIO_EXT, _VIDEO_EXT
_CODE_EXT = {".py", ".js", ".ts", ".go", ".java", ".cpp", ".c", ".rs", ".rb", ".php", ".swift", ".kt"}
_DOC_EXT = {".pdf", ".doc", ".docx", ".xlsx", ".xls"}
# Raster image formats only — NOT .svg, which is XML text that read_file reads fine.
# The MCP server wrapper returns raster images directly to the client.
_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".heic", ".heif", ".tiff", ".tif"}
_AV_EXT = _AUDIO_EXT | _VIDEO_EXT


def _read_file_impl(
    path: str,
    user_query: str = "",
    line_start: int = 0,
    line_end: int = 0,
    page_start: int = 0,
    page_end: int = 0,
    contains: str = "",
    contains_any: list[str] | None = None,
    contains_all: list[str] | None = None,
    regex: str = "",
    regex_flags: str = "",
    whole_word: bool = False,
    doc_mode: str = "",
    context_lines: int = 3,
) -> str:
    if not path:
        return "[error] path is required"
    try:
        progress(f"กำลังอ่านไฟล์ {path}")
        from ._safety import resolve_read_path
        p = Path(resolve_read_path(path))
        if not p.exists():
            return f"[error] file not found: {path}"

        suffix = p.suffix.lower()
        size = p.stat().st_size

        if suffix in _AV_EXT:
            if page_start or page_end:
                return "[error] page_start/page_end is only supported for PDF files"
            if size > _MAX_AV_BYTES:
                mb = size / (1024 * 1024)
                limit_mb = _MAX_AV_BYTES / (1024 * 1024)
                return f"[error] file too large: {mb:.1f} MB (limit {limit_mb:.0f} MB)"
            progress(f"กำลังถอดเสียงไฟล์ {path}")
            return _transcribe_to_state(p, path, user_query)

        if regex_flags and not regex:
            return "[error] regex_flags requires regex"

        search_modes = [
            bool(contains),
            bool(contains_any),
            bool(contains_all),
            bool(regex),
        ]
        if sum(search_modes) > 1:
            return "[error] use only one of contains, contains_any, contains_all, regex"
        if doc_mode and not any(search_modes):
            return "[error] doc_mode requires one of contains, contains_any, contains_all, regex"

        if page_start < 0 or page_end < 0:
            return f"[error] invalid page range: {page_start}-{page_end}"
        if page_start or page_end:
            if line_start or line_end:
                return "[error] page_start/page_end cannot be combined with line_start/line_end"
            if any(search_modes) or doc_mode:
                return "[error] page_start/page_end cannot be combined with search filters or doc_mode"

        # Documents are binary containers (shell text search can't read them), so their size
        # gate must come before the generic one and carry doc-appropriate advice.
        # A ranged PDF read only ever touches the requested pages (pdftotext -f/-l,
        # PyMuPDF lazy page access) regardless of total file size, so it's exempt.
        ranged_pdf = bool(page_start or page_end) and suffix == ".pdf"
        if suffix in _DOC_EXT and size > _MAX_FILE_BYTES and not ranged_pdf:
            mb = size / (1024 * 1024)
            limit_mb = _MAX_FILE_BYTES / (1024 * 1024)
            hint = ("use page_start/page_end to read specific pages" if suffix == ".pdf"
                    else "split the file or extract the needed pages first")
            return f"[error] document too large: {mb:.1f} MB (limit {limit_mb:.0f} MB) — {hint}"
        if suffix in _DOC_EXT:
            progress(f"กำลังอ่านเอกสาร {path}")
            return _read_document(
                p,
                path,
                user_query,
                contains=contains,
                contains_any=contains_any or [],
                contains_all=contains_all or [],
                regex=regex,
                regex_flags=regex_flags,
                whole_word=whole_word,
                doc_mode=doc_mode,
                context_lines=context_lines,
                page_start=page_start,
                page_end=page_end,
            )

        if suffix in _IMAGE_EXT:
            return f"[error] {path} is an image file — use the MCP server's read_file wrapper to read it"

        if doc_mode:
            return "[error] doc_mode is only supported for PDF/DOC/DOCX/XLSX/XLS files"

        if page_start or page_end:
            return "[error] page_start/page_end is only supported for PDF files"

        if size > _MAX_FILE_BYTES:
            mb = size / (1024 * 1024)
            limit_mb = _MAX_FILE_BYTES / (1024 * 1024)
            return (f"[error] file too large: {mb:.1f} MB (limit {limit_mb:.0f} MB) — "
                    f"use bash (for example: rg -n / find / rg --files) to locate specific sections first")

        # Binary guard: a NUL byte in the head means this isn't decodable text;
        # read_text(errors="replace") would otherwise return megabytes of mojibake
        # with is_error=False, polluting agent state with garbage.
        raw = p.read_bytes()
        progress(f"กำลังถอดรหัสข้อความจาก {path} ({len(raw):,} bytes)")
        if b"\x00" in raw[:8192]:
            return (f"[error] {path} is a binary file, not readable as text — "
                f"use the MCP server's read_file wrapper if it's an image")
        content = raw.decode("utf-8", errors="replace")
        if any(search_modes):
            if line_start > 0 or line_end > 0:
                return "[error] search filters cannot be combined with line_start/line_end"
            return _read_matching_sections(
                content,
                path,
                context_lines,
                contains=contains,
                contains_any=contains_any or [],
                contains_all=contains_all or [],
                regex=regex,
                regex_flags=regex_flags,
                whole_word=whole_word,
                label="matches",
            )

        if line_start > 0 or line_end > 0:
            lines = content.splitlines()
            total_lines = len(lines)
            # Guard raw values BEFORE defaulting (a negative→1 via the ternary would
            # otherwise be silently accepted), then past-EOF, then inverted, then clamp.
            if line_start < 0 or line_end < 0:
                return f"[error] invalid line range: {line_start}-{line_end}"
            start = line_start if line_start > 0 else 1
            end = line_end if line_end > 0 else total_lines
            if start > total_lines:
                return f"[error] line_start {start} is past end of file ({total_lines} lines)"
            if start > end:
                return f"[error] invalid line range: {line_start}-{line_end}"
            clamped_end = min(end, total_lines)
            sliced = "\n".join(lines[start - 1:clamped_end])
            out = f"[{path} — lines {start}-{clamped_end} of {total_lines}]\n\n" + sliced
            if len(out) > _MAX_CHARS:
                out = out[:_MAX_CHARS] + "\n...(truncated — range too large, narrow line_start/line_end)"
            return out

        if len(content) <= _MAX_CHARS:
            return content
        if suffix in _CODE_EXT:
            return _extract_structure(content, path)
        return content[:_MAX_CHARS] + f"\n...(truncated — {len(content)} chars total, use line_start/line_end to read a specific range, or use bash (rg -n / rg --files / find) to locate it first)"
    except Exception as e:
        return f"[error] read_file failed: {e}"


def _read_document(
    p: Path,
    path: str,
    query: str = "",
    *,
    contains: str = "",
    contains_any: list[str],
    contains_all: list[str],
    regex: str = "",
    regex_flags: str = "",
    whole_word: bool = False,
    doc_mode: str = "",
    context_lines: int = 3,
    page_start: int = 0,
    page_end: int = 0,
) -> str:
    """Convert a PDF/DOC/DOCX/XLSX/XLS to markdown, sampling for coverage if large.
    Scanned/image-only PDFs fall back to Apple Vision OCR across all pages."""
    if page_start or page_end:
        return _read_document_pages(p, path, page_start, page_end, query)
    from ._doc_extract import to_markdown
    md = to_markdown(str(p))
    if md.startswith("[error]"):
        return md
    if len(md.strip()) < 10 and p.suffix.lower() == ".pdf":
        return _ocr_pdf(p, path, query)
    if contains or contains_any or contains_all or regex:
        if doc_mode:
            return _read_document_matches(
                md,
                path,
                context_lines,
                contains=contains,
                contains_any=contains_any,
                contains_all=contains_all,
                regex=regex,
                regex_flags=regex_flags,
                whole_word=whole_word,
                doc_mode=doc_mode,
            )
        return _read_matching_sections(
            md,
            path,
            context_lines,
            contains=contains,
            contains_any=contains_any,
            contains_all=contains_all,
            regex=regex,
            regex_flags=regex_flags,
            whole_word=whole_word,
            label="document matches",
        )
    if p.suffix.lower() == ".pdf" and query:
        layout_text = _pdf_layout_text(p)
        if len(layout_text.strip()) >= 10:
            return _sample_coverage(layout_text, path, query=query)
    if len(md) <= _MAX_CHARS:
        return md
    return _sample_coverage(md, path, query=query)


def _pdf_layout_text(p: Path, page_start: int = 0, page_end: int = 0) -> str:
    """Best-effort Poppler extraction for query-focused PDF table reads.
    page_start/page_end (1-indexed, inclusive) restrict extraction to that
    page range when given — otherwise the whole document is extracted."""
    try:
        cmd = ["pdftotext"]
        if page_start > 0:
            cmd += ["-f", str(page_start)]
        if page_end > 0:
            cmd += ["-l", str(page_end)]
        cmd += ["-layout", str(p), "-"]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        return result.stdout if result.returncode == 0 else ""
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return ""


def _pdf_page_count(p: Path) -> int | None:
    """Page count via PyMuPDF, or None when the library/file is unavailable."""
    try:
        import fitz  # PyMuPDF
    except ImportError:
        return None
    try:
        doc = fitz.open(str(p))
        n = len(doc)
        doc.close()
        return n
    except Exception:
        return None


def _pdf_layout_pages(p: Path, start: int, end: int) -> list[str]:
    """Per-page text for the 1-indexed inclusive [start, end] range, split on
    pdftotext's form-feed page separator so a budget-limited caller can stop
    at an exact page boundary instead of truncating mid-page. list index i
    corresponds to page start+i (pdftotext emits one trailing form feed per
    page, including the last, which is dropped here)."""
    raw = _pdf_layout_text(p, start, end)
    if not raw:
        return []
    parts = raw.split("\f")
    if parts and parts[-1] == "":
        parts = parts[:-1]
    return parts


def _accumulate_page_blocks(
    blocks: list[tuple[int, str]], budget: int
) -> tuple[list[tuple[int, str]], int]:
    """Greedily keep (page_number, text) blocks while the running total stays
    within `budget`, stopping only at a page boundary — never mid-page. Always
    keeps at least the first block, even if it alone exceeds budget, so a
    single oversized page still returns something instead of nothing.
    Returns (kept_blocks, last_page_number_kept)."""
    kept: list[tuple[int, str]] = []
    total = 0
    last = blocks[0][0] - 1 if blocks else 0
    for page_num, text in blocks:
        piece_len = len(text) + 24  # overhead for the "--- หน้า N ---" marker
        if kept and total + piece_len > budget:
            break
        kept.append((page_num, text))
        total += piece_len
        last = page_num
    return kept, last


def _continuation_note(last_page: int, requested_end: int, total: int) -> str:
    """Trailing hint telling the caller exactly what page_start to pass next.
    Empty once last_page reaches both the requested end and the document end
    — the only state where no further call is needed."""
    if last_page < requested_end:
        return (f"\n[note: budget reached at page {last_page} — "
                f"call again with page_start={last_page + 1} to continue]")
    if last_page < total:
        return (f"\n[note: call again with page_start={last_page + 1} "
                f"to continue reading the rest of the document]")
    return ""


def _read_document_pages(
    p: Path, path: str, page_start: int, page_end: int, query: str = ""
) -> str:
    """Read an explicit 1-indexed page range from a PDF directly, bypassing
    sampling entirely so a large PDF can be walked page-range by page-range
    to cover every page. Tries extractable text first (pdftotext -f/-l);
    falls back to per-page OCR when the range has no extractable text. Both
    paths stop only at a page boundary (never mid-page, even under the char
    budget) and always report the exact next page_start via _continuation_note."""
    if p.suffix.lower() != ".pdf":
        return "[error] page_start/page_end is only supported for PDF files (not DOCX/XLSX/XLS)"

    total = _pdf_page_count(p)
    if total is None:
        return f"[error] cannot determine page count for {path} (PyMuPDF unavailable)"
    if total == 0:
        return f"[error] {path} has no pages"

    start = page_start if page_start > 0 else 1
    end = page_end if page_end > 0 else total
    if start > total:
        return f"[error] page_start {start} is past end of document ({total} pages)"
    if start > end:
        return f"[error] invalid page range: {page_start}-{page_end}"
    end = min(end, total)

    _MAX_RANGE_PAGES = 30  # hard ceiling on pages considered per call, regardless of budget
    if end - start + 1 > _MAX_RANGE_PAGES:
        end = start + _MAX_RANGE_PAGES - 1

    page_texts = _pdf_layout_pages(p, start, end)
    if page_texts and sum(len(t.strip()) for t in page_texts) >= 10:
        blocks = [(start + i, t) for i, t in enumerate(page_texts)]
        budget = _MAX_CHARS - 300  # reserve room for header + continuation note
        kept, last_page = _accumulate_page_blocks(blocks, budget)
        body = "\n\n".join(f"--- หน้า {n} ---\n{t}" for n, t in kept)
        header = f"[{path} — pages {start}-{last_page} of {total}, extracted text]\n\n"
        note = _continuation_note(last_page, end, total)
        out = header + body + note
        if len(out) > _MAX_CHARS:  # safety net — should rarely trigger given the budget reserve
            out = out[:_MAX_CHARS] + "\n...(truncated)"
        return out

    # No extractable text in this range — fall back to OCR for just these pages.
    return _ocr_pdf(p, path, query, page_start=start, page_end=end, total_pages=total)


def _ocr_pdf(
    p: Path,
    path: str,
    query: str = "",
    *,
    page_start: int = 0,
    page_end: int = 0,
    total_pages: int | None = None,
) -> str:
    """Rasterize pages with PyMuPDF → Apple Vision OCR → combine → same
    context budget as a regular PDF (_sample_coverage).
    Without page_start/page_end: OCRs the first _MAX_PAGES pages (default
    whole-document read). With page_start/page_end (already validated/clamped
    by the caller, 1-indexed inclusive): OCRs exactly that range instead, and
    stops accumulating output at a page boundary once the char budget is hit
    (never mid-page), reporting the exact next page_start via _continuation_note."""
    try:
        import fitz  # PyMuPDF
        from ._ocr import read_layout as _ocr_layout
    except ImportError as e:
        return f"[error] scanned PDF OCR unavailable: {e}"

    try:
        doc = fitz.open(str(p))
    except Exception as e:
        return f"[error] cannot open PDF: {e}"

    _MAX_PAGES = 30
    blocks: list[tuple[int, str]] = []
    tmp_files: list[str] = []
    n_pages = len(doc)                        # capture before close
    ranged = bool(page_start or page_end)
    if ranged:
        start_idx = max(0, page_start - 1)
        end_idx = min(n_pages - 1, page_end - 1)
    else:
        start_idx = 0
        end_idx = min(n_pages, _MAX_PAGES) - 1

    try:
        for i in range(start_idx, end_idx + 1):
            try:
                pix = doc[i].get_pixmap(dpi=150)
                tmp_path = f"/tmp/_endeavor_ocr_{uuid.uuid4().hex[:8]}_p{i}.png"
                pix.save(tmp_path)
                tmp_files.append(tmp_path)
                boxes = _ocr_layout(tmp_path)
                text = "\n".join(b["text"] for b in boxes if b.get("text"))
            except Exception:
                text = ""
            if text.strip():
                blocks.append((i + 1, text))
    finally:
        doc.close()
        for f in tmp_files:
            try:
                os.unlink(f)
            except Exception:
                pass

    if not blocks:
        return f"[error] OCR ไม่พบข้อความใน {path} (PDF {n_pages} หน้า)"

    if ranged:
        total = total_pages if total_pages is not None else n_pages
        budget = _MAX_CHARS - 300  # reserve room for header + continuation note
        kept, last_page = _accumulate_page_blocks(blocks, budget)
        body = "\n\n".join(f"--- หน้า {n} ---\n{t}" for n, t in kept)
        header = f"[{path} — scanned PDF, OCR pages {start_idx + 1}-{last_page} of {total}]\n\n"
        note = _continuation_note(last_page, end_idx + 1, total)
        full = header + body + note
        if len(full) > _MAX_CHARS:  # safety net — should rarely trigger given the budget reserve
            full = full[:_MAX_CHARS] + "\n...(truncated)"
        return full

    skipped_note = (
        f"\n[หมายเหตุ: อ่านแค่ {_MAX_PAGES} หน้าแรก จาก {n_pages} หน้าทั้งหมด — "
        f"ใช้ page_start/page_end เพื่ออ่านหน้าอื่น]"
    ) if n_pages > _MAX_PAGES else ""
    combined = "\n\n".join(f"--- หน้า {n} ---\n{t}" for n, t in blocks) + skipped_note

    header = f"[{path} — scanned PDF, OCR {end_idx - start_idx + 1} หน้า]\n\n"
    full = header + combined
    if len(full) <= _MAX_CHARS:
        return full
    return _sample_coverage(combined, path, query=query)


def _transcribe_to_state(p: Path, path: str, user_query: str = "") -> str:
    """Transcribe an audio/video file: save the full raw transcript to workspace,
    return the transcript text (locally sampled if very long, never LLM-summarized)
    + saved path for state (same shape as scrape_table.py's saved-file +
    in-state-summary return)."""
    from ._transcribe import transcribe_media

    try:
        raw = transcribe_media(str(p))
    except Exception as e:
        return f"[error] transcribe failed for {path}: {e}"

    safe_stem = re.sub(r"[^a-zA-Z0-9_.-]", "_", p.stem)[:60]
    filename = f"transcript_{safe_stem}_{uuid.uuid4().hex[:8]}.md"
    saved_path = ""
    try:
        out_path = Path(WORKSPACE) / filename
        out_path.write_text(raw, encoding="utf-8")
        saved_path = str(out_path)
    except Exception as e:
        return f"[error] transcribed {path} but failed to save .md: {e}"

    sampled = _sample_coverage(raw, path, max_chars=9500, query=user_query) if len(raw) > 9500 else raw

    return f"[transcript] {path}\nบันทึกไฟล์ครบที่: {saved_path}\n\n{sampled}"


def _read_matching_sections(
    content: str,
    path: str,
    context_lines: int,
    *,
    contains: str = "",
    contains_any: list[str],
    contains_all: list[str],
    regex: str = "",
    regex_flags: str = "",
    whole_word: bool = False,
    label: str = "matches",
) -> str:
    if context_lines < 0:
        return f"[error] context_lines must be >= 0 (got {context_lines})"

    lines = content.splitlines()
    total_lines = len(lines)
    desc = ""
    if regex_flags and not regex:
        return "[error] regex_flags requires regex"
    if contains:
        hit_indexes = [i for i, line in enumerate(lines) if _line_matches_terms(line, [contains], mode="all", whole_word=whole_word)]
        desc = f"{contains!r}" + (" (whole word)" if whole_word else "")
    elif contains_any:
        needles = [str(s).strip() for s in contains_any if str(s).strip()]
        if not needles:
            return "[error] contains_any must include at least 1 non-empty keyword"
        hit_indexes = [i for i, line in enumerate(lines) if _line_matches_terms(line, needles, mode="any", whole_word=whole_word)]
        desc = f"any of {contains_any!r}" + (" (whole word)" if whole_word else "")
    elif contains_all:
        needles = [str(s).strip() for s in contains_all if str(s).strip()]
        if not needles:
            return "[error] contains_all must include at least 1 non-empty keyword"
        hit_indexes = [i for i, line in enumerate(lines) if _line_matches_terms(line, needles, mode="all", whole_word=whole_word)]
        desc = f"all of {contains_all!r}" + (" (whole word)" if whole_word else "")
    elif regex:
        try:
            flags = _parse_regex_flags(regex_flags)
            pat = re.compile(regex, flags)
        except re.error as e:
            return f"[error] invalid regex {regex!r}: {e}"
        hit_indexes = _regex_hit_lines(content, pat)
        extra = f" flags={regex_flags!r}" if regex_flags else ""
        desc = f"regex {regex!r}{extra}"
    else:
        return "[error] no search filter provided"

    if not hit_indexes:
        return f"[error] no matches for {desc} in {path}"

    ranges: list[tuple[int, int]] = []
    for idx in hit_indexes:
        start = max(0, idx - context_lines)
        end = min(total_lines - 1, idx + context_lines)
        if ranges and start <= ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], max(ranges[-1][1], end))
        else:
            ranges.append((start, end))

    blocks: list[str] = [f"[{path} — {len(hit_indexes)} {label} for {desc}]"]
    prev_end = -1
    for start, end in ranges:
        if prev_end >= 0 and start > prev_end + 1:
            blocks.append(f"... ({start - prev_end - 1} lines skipped) ...")
        blocks.append(f"\n[lines {start + 1}-{end + 1} of {total_lines}]")
        blocks.append("\n".join(lines[start:end + 1]))
        prev_end = end

    out = "\n\n".join(blocks)
    if len(out) > _MAX_CHARS:
        out = out[:_MAX_CHARS] + "\n...(truncated — too many matches, narrow contains or reduce context_lines)"
    return out


def _read_document_matches(
    content: str,
    path: str,
    context_lines: int,
    *,
    contains: str = "",
    contains_any: list[str],
    contains_all: list[str],
    regex: str = "",
    regex_flags: str = "",
    whole_word: bool = False,
    doc_mode: str,
) -> str:
    mode = (doc_mode or "").strip().lower()
    if mode not in {"heading", "section", "row", "cell"}:
        return '[error] doc_mode must be one of "heading", "section", "row", "cell"'
    if context_lines < 0:
        return f"[error] context_lines must be >= 0 (got {context_lines})"

    lines = content.splitlines()
    if mode == "cell":
        return _read_document_cell_matches(
            lines,
            path,
            contains=contains,
            contains_any=contains_any,
            contains_all=contains_all,
            regex=regex,
            regex_flags=regex_flags,
            whole_word=whole_word,
        )
    if mode == "section":
        return _read_document_section_matches(
            lines,
            path,
            contains=contains,
            contains_any=contains_any,
            contains_all=contains_all,
            regex=regex,
            regex_flags=regex_flags,
            whole_word=whole_word,
        )

    hit_indexes = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if mode == "heading":
            if not stripped.startswith("#"):
                continue
        elif mode == "row":
            if not _is_markdown_table_row(stripped):
                continue
        if _search_text_matches(
            line,
            contains=contains,
            contains_any=contains_any,
            contains_all=contains_all,
            regex=regex,
            regex_flags=regex_flags,
            whole_word=whole_word,
        ):
            hit_indexes.append(i)

    desc = _search_desc(
        contains=contains,
        contains_any=contains_any,
        contains_all=contains_all,
        regex=regex,
        regex_flags=regex_flags,
        whole_word=whole_word,
    )
    if not hit_indexes:
        return f"[error] no {mode} matches for {desc} in {path}"
    return _format_line_matches(lines, path, hit_indexes, context_lines, desc, label=f"{mode} matches")


def _read_document_cell_matches(
    lines: list[str],
    path: str,
    *,
    contains: str = "",
    contains_any: list[str],
    contains_all: list[str],
    regex: str = "",
    regex_flags: str = "",
    whole_word: bool = False,
) -> str:
    desc = _search_desc(
        contains=contains,
        contains_any=contains_any,
        contains_all=contains_all,
        regex=regex,
        regex_flags=regex_flags,
        whole_word=whole_word,
    )
    hits: list[str] = []
    hit_count = 0
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not _is_markdown_table_row(stripped):
            continue
        cells = _split_markdown_row(stripped)
        matches = []
        for col_idx, cell in enumerate(cells, start=1):
            if _search_text_matches(
                cell,
                contains=contains,
                contains_any=contains_any,
                contains_all=contains_all,
                regex=regex,
                regex_flags=regex_flags,
                whole_word=whole_word,
            ):
                hit_count += 1
                matches.append(f'col {col_idx}="{cell}"')
        if matches:
            block = [
                f"[line {idx + 1}]",
                line,
                "matched cells: " + ", ".join(matches),
            ]
            hits.append("\n".join(block))

    if not hits:
        return f"[error] no cell matches for {desc} in {path}"

    out = [f"[{path} — {hit_count} cell matches for {desc}]"]
    out.extend(["", *hits])
    joined = "\n\n".join(out)
    if len(joined) > _MAX_CHARS:
        joined = joined[:_MAX_CHARS] + "\n...(truncated — too many cell matches, narrow contains)"
    return joined


def _read_document_section_matches(
    lines: list[str],
    path: str,
    *,
    contains: str = "",
    contains_any: list[str],
    contains_all: list[str],
    regex: str = "",
    regex_flags: str = "",
    whole_word: bool = False,
) -> str:
    desc = _search_desc(
        contains=contains,
        contains_any=contains_any,
        contains_all=contains_all,
        regex=regex,
        regex_flags=regex_flags,
        whole_word=whole_word,
    )
    sections = _markdown_sections(lines)
    hits: list[str] = []
    matched_sections: list[tuple[int, int, int, str]] = []
    for start, end, level, heading in sections:
        section_text = "\n".join(lines[start:end + 1])
        if _search_text_matches(
            section_text,
            contains=contains,
            contains_any=contains_any,
            contains_all=contains_all,
            regex=regex,
            regex_flags=regex_flags,
            whole_word=whole_word,
        ):
            matched_sections.append((start, end, level, heading))
    matched_sections = _prune_ancestor_section_matches(
        matched_sections, sections, lines,
        contains=contains, contains_any=contains_any, contains_all=contains_all,
        regex=regex, regex_flags=regex_flags, whole_word=whole_word,
    )

    if not matched_sections:
        return f"[error] no section matches for {desc} in {path}"

    total = len(matched_sections)
    for idx, (start, end, level, heading) in enumerate(matched_sections, start=1):
        block = [
            f"===== section {idx}/{total} =====",
            f"[heading={heading.strip()!r} level={level} lines={start + 1}-{end + 1}]",
        ]
        block.extend(lines[start:end + 1])
        hits.append("\n".join(block))

    out = [f"[{path} — {total} section matches for {desc}]"]
    out.extend(["", *hits])
    joined = "\n\n".join(out)
    if len(joined) > _MAX_CHARS:
        joined = joined[:_MAX_CHARS] + "\n...(truncated — section too large, narrow contains)"
    return joined


def _format_line_matches(
    lines: list[str],
    path: str,
    hit_indexes: list[int],
    context_lines: int,
    desc: str,
    *,
    label: str,
) -> str:
    total_lines = len(lines)
    ranges: list[tuple[int, int]] = []
    for idx in hit_indexes:
        start = max(0, idx - context_lines)
        end = min(total_lines - 1, idx + context_lines)
        if ranges and start <= ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], max(ranges[-1][1], end))
        else:
            ranges.append((start, end))

    blocks: list[str] = [f"[{path} — {len(hit_indexes)} {label} for {desc}]"]
    prev_end = -1
    for start, end in ranges:
        if prev_end >= 0 and start > prev_end + 1:
            blocks.append(f"... ({start - prev_end - 1} lines skipped) ...")
        blocks.append(f"\n[lines {start + 1}-{end + 1} of {total_lines}]")
        blocks.append("\n".join(lines[start:end + 1]))
        prev_end = end

    out = "\n\n".join(blocks)
    if len(out) > _MAX_CHARS:
        out = out[:_MAX_CHARS] + "\n...(truncated — too many matches, narrow contains or reduce context_lines)"
    return out


def _search_desc(
    *,
    contains: str = "",
    contains_any: list[str],
    contains_all: list[str],
    regex: str = "",
    regex_flags: str = "",
    whole_word: bool = False,
) -> str:
    if contains:
        return f"{contains!r}" + (" (whole word)" if whole_word else "")
    if contains_any:
        return f"any of {contains_any!r}" + (" (whole word)" if whole_word else "")
    if contains_all:
        return f"all of {contains_all!r}" + (" (whole word)" if whole_word else "")
    extra = f" flags={regex_flags!r}" if regex_flags else ""
    return f"regex {regex!r}{extra}"


def _search_text_matches(
    text: str,
    *,
    contains: str = "",
    contains_any: list[str],
    contains_all: list[str],
    regex: str = "",
    regex_flags: str = "",
    whole_word: bool = False,
) -> bool:
    if contains:
        return _line_matches_terms(text, [contains], mode="all", whole_word=whole_word)
    if contains_any:
        needles = [str(s).strip() for s in contains_any if str(s).strip()]
        return bool(needles) and _line_matches_terms(text, needles, mode="any", whole_word=whole_word)
    if contains_all:
        needles = [str(s).strip() for s in contains_all if str(s).strip()]
        return bool(needles) and _line_matches_terms(text, needles, mode="all", whole_word=whole_word)
    flags = _parse_regex_flags(regex_flags)
    return bool(re.compile(regex, flags).search(text))


def _is_markdown_table_row(line: str) -> bool:
    if "|" not in line:
        return False
    stripped = line.strip()
    if not stripped.startswith("|") or not stripped.endswith("|"):
        return False
    if set(stripped) <= set("| -:"):
        return False
    return True


def _split_markdown_row(line: str) -> list[str]:
    parts = [p.strip() for p in line.strip().strip("|").split("|")]
    return [p for p in parts if p or len(parts) == 1]


def _heading_level(line: str) -> int:
    m = re.match(r"^(#+)\s+", line.strip())
    return len(m.group(1)) if m else 0


def _markdown_sections(lines: list[str]) -> list[tuple[int, int, int, str]]:
    sections: list[tuple[int, int, int, str]] = []
    headings: list[tuple[int, int, str]] = []
    for idx, line in enumerate(lines):
        level = _heading_level(line)
        if level:
            headings.append((idx, level, line))
    for pos, (start, level, heading) in enumerate(headings):
        end = len(lines) - 1
        for next_start, next_level, _next_heading in headings[pos + 1:]:
            if next_level <= level:
                end = next_start - 1
                break
        sections.append((start, end, level, heading))
    return sections


def _section_own_text(
    lines: list[str],
    all_sections: list[tuple[int, int, int, str]],
    target: tuple[int, int, int, str],
) -> str:
    """Section text with every nested child-section range stripped out — the
    content that belongs directly under this heading, not any subheading."""
    start, end, level, _heading = target
    excluded: set[int] = set()
    for o_start, o_end, o_level, _h in all_sections:
        if (o_start, o_end, o_level) == (start, end, level):
            continue
        if start <= o_start and end >= o_end and o_level > level:
            excluded.update(range(o_start, o_end + 1))
    return "\n".join(lines[i] for i in range(start, end + 1) if i not in excluded)


def _prune_ancestor_section_matches(
    matched_sections: list[tuple[int, int, int, str]],
    all_sections: list[tuple[int, int, int, str]],
    lines: list[str],
    *,
    contains: str = "",
    contains_any: list[str],
    contains_all: list[str],
    regex: str = "",
    regex_flags: str = "",
    whole_word: bool = False,
) -> list[tuple[int, int, int, str]]:
    kept: list[tuple[int, int, int, str]] = []
    for sec in matched_sections:
        start, end, level, _heading = sec
        overshadowed = False
        for other_start, other_end, other_level, _other_heading in matched_sections:
            if (other_start, other_end, other_level) == (start, end, level):
                continue
            if start <= other_start and end >= other_end and other_level > level:
                overshadowed = True
                break
        if overshadowed:
            # A child match alone doesn't justify dropping the parent if the
            # parent's own text (outside any subsection) independently matched
            # too — that content would otherwise vanish entirely (audit.md #7).
            own_text = _section_own_text(lines, all_sections, sec)
            if _search_text_matches(
                own_text, contains=contains, contains_any=contains_any,
                contains_all=contains_all, regex=regex, regex_flags=regex_flags,
                whole_word=whole_word,
            ):
                overshadowed = False
        if not overshadowed:
            kept.append(sec)
    return kept


def _is_compact_read_result(content: str) -> bool:
    """True when read_file already returned a compact/structured artifact and a
    wrapper should avoid summarizing it again."""
    first = (content.split("\n", 1)[0] if content else "").lower()
    markers = (
        "structure map only",
        "sampled for coverage",
        "document matches",
        "heading matches",
        "section matches",
        "row matches",
        "cell matches",
        "[transcript]",
    )
    return any(m in first for m in markers)


def _parse_regex_flags(spec: str) -> int:
    flags = 0
    if not spec:
        return flags
    for ch in spec:
        if ch == "i":
            flags |= re.IGNORECASE
        elif ch == "m":
            flags |= re.MULTILINE
        elif ch == "s":
            flags |= re.DOTALL
        else:
            raise re.error(f"unsupported regex flag: {ch}")
    return flags


def _line_matches_terms(line: str, needles: list[str], mode: str, whole_word: bool) -> bool:
    lowered = line.lower()
    checks = []
    for needle in needles:
        if whole_word:
            pat = re.compile(rf"\b{re.escape(needle)}\b", re.IGNORECASE)
            checks.append(bool(pat.search(line)))
        else:
            direct = needle.lower() in lowered
            # PDF/OCR converters often insert spaces inside Thai words
            # ("กรกฎาคม" -> "กรก ฎ าคม").  Exact substring search then reports
            # a false no-match and pushes the agent into expensive image/OCR
            # fallbacks.  Keep ordinary matching unchanged; use a whitespace-
            # insensitive fallback only when the requested term is Thai.
            if not direct and any("\u0e00" <= ch <= "\u0e7f" for ch in needle):
                compact_line = re.sub(r"\s+", "", lowered)
                compact_needle = re.sub(r"\s+", "", needle.lower())
                direct = compact_needle in compact_line
            checks.append(direct)
    return any(checks) if mode == "any" else all(checks)


def _regex_hit_lines(content: str, pat: re.Pattern[str]) -> list[int]:
    starts = [0]
    for i, ch in enumerate(content):
        if ch == "\n":
            starts.append(i + 1)
    if not starts:
        starts = [0]

    import bisect

    hits: set[int] = set()
    for m in pat.finditer(content):
        s, e = m.span()
        if e <= s:
            e = min(len(content), s + 1)
        start_line = bisect.bisect_right(starts, s) - 1
        end_line = bisect.bisect_right(starts, max(s, e - 1)) - 1
        for idx in range(max(0, start_line), max(0, end_line) + 1):
            hits.add(idx)
    return sorted(hits)


def _sample_coverage(text: str, path: str, max_chars: int | None = None, query: str = "") -> str:
    """Approach C — outline + uniform sampling across the whole document.

    Keeps whole lines (paragraphs for prose, rows for tables) spread evenly so
    the head, middle, and tail are all represented, with [... skipped N ...] markers.
    max_chars overrides the module-level _MAX_CHARS for callers with a different budget.
    query, if given, pins lines matching its keywords so specific figures aren't
    diluted away by uniform sampling on large pages (e.g. a fee % buried at char
    25,000 of a 1.2M-char JS-app page survives instead of being sampled out).
    """
    if max_chars is None:
        max_chars = _MAX_CHARS
    _LINE_CAP = 2_000   # hard cap on any single line so one giant unit can't blow the budget
    lines = text.split("\n")
    total_chars = len(text)
    units = [(ln if len(ln) <= _LINE_CAP else ln[:_LINE_CAP] + " …[line truncated]")
             for ln in lines if ln.strip()]
    n = len(units)

    # Pin structural lines that must survive sampling: markdown headings (#…) and
    # table header rows (the line directly above a "| --- |" separator + the
    # separator itself) — without them, sampled table rows are unlabelled.
    pinned: set[int] = set()
    for i, u in enumerate(units):
        s = u.lstrip()
        if s.startswith("#"):
            pinned.add(i)
        elif "---" in s and set(s) <= set("| -:"):
            pinned.add(i)
            if i > 0:
                pinned.add(i - 1)

    # Keyword-anchored pinning: capped so a keyword-dense page can't blow the
    # budget and starve general coverage sampling. Thai has no inter-word spaces,
    # so a multi-word Thai query tokenises into one long compound that rarely
    # substring-matches a line; break long Thai tokens into overlapping 4-grams so
    # the component words still pin relevant lines (the kw_budget below bounds any
    # over-pinning the n-grams cause).
    keywords: list[str] = []
    for w in (m.lower() for m in re.findall(r"\w+", query) if len(m) >= 2):
        if len(w) > 6 and any("฀" <= c <= "๿" for c in w):
            keywords += [w[j:j + 4] for j in range(0, len(w) - 3, 2)]
        else:
            keywords.append(w)
    keywords = list(dict.fromkeys(keywords))
    query_pinned: set[int] = set()
    best_query_indexes: list[int] = []
    if keywords:
        kw_budget = int(max_chars * 0.4)
        kw_chars = 0
        candidates: list[tuple[int, int]] = []
        for i, u in enumerate(units):
            if i in pinned:
                continue
            score = sum(
                1 for kw in keywords
                if _line_matches_terms(u, [kw], mode="any", whole_word=False)
            )
            if score:
                candidates.append((score, i))
        # Rank relevance before document order.  A generic year can match many
        # early rows and exhaust the pin budget before the actual subject appears.
        ranked_candidates = sorted(candidates, key=lambda item: (-item[0], item[1]))
        if ranked_candidates and ranked_candidates[0][0] >= 2:
            best_score = ranked_candidates[0][0]
            best_query_indexes = [i for score, i in ranked_candidates if score == best_score][:8]
        for _score, i in ranked_candidates:
            if kw_chars >= kw_budget:
                break
            query_pinned.add(i)
            kw_chars += len(units[i])

    # A strong multi-keyword hit is safer and cheaper as a focused excerpt than
    # mixing it with unrelated coverage from the rest of the document.  Weak or
    # absent matches retain the normal head/middle/tail coverage behavior.
    if best_query_indexes:
        focused = _format_line_matches(
            units, path, best_query_indexes, 3, repr(query), label="query-focused lines"
        )
        if len(focused) <= max_chars:
            return focused
        return focused[:max_chars] + "\n...(truncated — narrow user_query)"

    headings = [units[i].strip() for i in sorted(pinned)
                if units[i].lstrip().startswith("#")][:40]
    header = [f"[{path} — document, {total_chars} chars total, sampled for coverage]", ""]
    if query_pinned:
        header += ["query matches:"] + [units[i] for i in sorted(query_pinned)] + [""]
    if headings:
        header += ["outline:"] + [f"  {h}" for h in headings] + [""]
    header_str = "\n".join(header)

    budget = max_chars - len(header_str) - 200
    if budget < 500:
        budget = 500

    body_chars = sum(len(u) for u in units)
    if body_chars <= budget:
        return header_str + "\n".join(units)

    # Keep a uniform fraction of lines spread across the whole document. Size the
    # ratio so the entire pass (kept lines + skip markers) fits inside the budget,
    # so we never stop early and the tail is always reached. _MARKER_LEN is the
    # estimated cost of one "[... skipped N lines ...]" line.
    _MARKER_LEN = 28
    pinned_chars = sum(len(units[i]) for i in pinned)
    free_indexes = [i for i in range(n) if i not in pinned]
    free_chars = sum(len(units[i]) for i in free_indexes)
    free_budget = max(0, budget - pinned_chars)
    ratio = min(1.0, free_budget / max(1, free_chars + len(free_indexes) * _MARKER_LEN))

    out: list[str] = []
    acc = 1.0 - ratio  # ensures the first line is always kept (head coverage)
    skipped = 0
    for i, u in enumerate(units):
        keep = i in pinned
        if not keep:
            acc += ratio
            if acc >= 1.0:
                keep = True
                acc -= 1.0
        if keep:
            if skipped:
                out.append(f"[... skipped {skipped} lines ...]")
                skipped = 0
            out.append(u)
        else:
            skipped += 1
    if skipped:
        # force-include the final line so the document tail is always represented
        if skipped > 1:
            out.append(f"[... skipped {skipped - 1} lines ...]")
        out.append(units[-1])

    result = header_str + "\n".join(out)
    if len(result) > max_chars:        # safety net — never exceed the cap
        # Truncate the MIDDLE, not the tail: a head-only cut would drop the document
        # end that the sampling loop deliberately force-includes (and any keyword-
        # pinned lines near the end), defeating the tail/coverage guarantee.
        keep_tail = min(1200, max_chars // 4)
        marker = "\n…[truncated middle to cap]…\n"
        head_budget = max_chars - keep_tail - len(marker)
        result = result[:head_budget].rstrip() + marker + result[-keep_tail:].lstrip()
    return result


def _extract_structure(content: str, path: str) -> str:
    lines = content.splitlines()
    symbols: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        m = re.match(r'^(def |class |async def |function |const |export const |export function |export class )([\w]+)', line)
        if m:
            sig = line.rstrip()
            j = i + 1
            while j < min(i + 6, len(lines)) and not re.search(r'[):{]', sig):
                sig += ' ' + lines[j].strip()
                j += 1
            sig = re.split(r'\s*[:{]\s*$', sig.strip())[0][:90]
            symbols.append(f"  {sig:<92} :{i + 1}")
        i += 1
    # Minified/bundled code (a few very long lines) defeats the line-anchored scan
    # above → empty map. Fall back to a global, non-anchored scan so the symbol list
    # isn't useless for the exact files most likely to be over the size limit.
    if not symbols and max((len(l) for l in lines), default=0) > 2000:
        names = re.findall(r'(?:function|class|const|let|var)\s+([A-Za-z_$][\w$]*)', content)
        symbols = [f"  {nm}" for nm in dict.fromkeys(names)][:40]
    imports = [l for l in lines[:30] if l.startswith(("import ", "from ", "require", "use ", "#include"))]
    out = [f"[{path} — {len(lines)} lines, structure map only]", ""]
    if imports:
        out += ["imports:"] + [f"  {l[:80]}" for l in imports[:10]] + [""]
    out += ["symbols:"] + (symbols if symbols else ["  (none found)"])
    out += ["", "Use bash (for example: rg -n) to locate specific sections by line number."]
    return "\n".join(out)
