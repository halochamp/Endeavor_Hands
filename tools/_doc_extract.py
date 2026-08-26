"""_doc_extract.py — PDF/Word/Excel → markdown/text.

Uses MarkItDown for office documents when installed and has a native PDF
fallback (PyMuPDF/Poppler), so a missing optional dependency does not block PDF
reading. One module-level singleton is reused across calls.

Returns markdown str, or "[error] ..." on failure — never raises.
Office-document install: pip install 'markitdown[pdf,docx,xlsx,xls]'
"""
from __future__ import annotations
import subprocess

_MD = None  # MarkItDown singleton, lazily initialised


def _get_md():
    global _MD
    if _MD is None:
        from markitdown import MarkItDown
        _MD = MarkItDown()
    return _MD


def _pdf_fallback_text(path: str) -> str:
    """Extract native PDF text without MarkItDown.

    An empty result is intentional for image-only PDFs: ``read_file`` then
    enters its existing Apple Vision OCR path.  An error is returned only when
    no native extractor is available at all.
    """
    available = False
    details: list[str] = []

    try:
        import fitz

        available = True
        doc = fitz.open(path)
        try:
            pages = []
            for number, page in enumerate(doc, 1):
                text = (page.get_text("text") or "").strip()
                if text:
                    pages.append(f"--- Page {number} ---\n{text}")
            native = "\n\n".join(pages)
            if len(native.strip()) >= 10:
                return native
        finally:
            doc.close()
    except ImportError:
        details.append("PyMuPDF unavailable")
    except Exception as exc:
        details.append(f"PyMuPDF failed: {exc}")

    try:
        result = subprocess.run(
            ["pdftotext", "-layout", path, "-"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
        available = True
        if result.returncode == 0 and len((result.stdout or "").strip()) >= 10:
            return result.stdout
        if result.stderr.strip():
            details.append("pdftotext: " + " ".join(result.stderr.split())[:180])
    except FileNotFoundError:
        details.append("pdftotext unavailable")
    except (OSError, subprocess.TimeoutExpired) as exc:
        details.append(f"pdftotext failed: {exc}")

    if available:
        # Native extractors worked but found no text: likely a scanned PDF.
        return ""
    hint = "; ".join(details) or "no PDF extractor available"
    return f"[error] PDF extraction unavailable — {hint}"


def to_markdown(path: str) -> str:
    """Convert a PDF/DOC/DOCX/XLSX/XLS file to markdown text.

    Returns the markdown string (may be empty for scanned/image-only PDFs),
    or "[error] ..." when MarkItDown is missing or conversion fails.
    """
    if path.lower().endswith(".doc"):
        return _legacy_doc_to_text(path)
    is_pdf = path.lower().endswith(".pdf")
    try:
        md = _get_md()
    except ImportError:
        if is_pdf:
            return _pdf_fallback_text(path)
        return "[error] markitdown not installed — run: pip install 'markitdown[pdf,docx,xlsx,xls]'"
    try:
        result = md.convert(path)
        text = result.text_content or ""
        if text.strip() or not is_pdf:
            return text
        # MarkItDown can return an empty body for image-only PDFs.  Let the
        # caller's existing OCR fallback handle that case.
        return _pdf_fallback_text(path)
    except Exception as e:
        if is_pdf:
            fallback = _pdf_fallback_text(path)
            if not fallback.startswith("[error]"):
                return fallback
        return f"[error] document parse failed: {e}"


def _legacy_doc_to_text(path: str) -> str:
    """Extract old binary Word .doc files with the built-in macOS converter."""
    try:
        result = subprocess.run(
            ["textutil", "-convert", "txt", "-stdout", "-encoding", "UTF-8", path],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as e:
        return f"[error] legacy .doc extraction failed: {e}"
    if result.returncode != 0:
        detail = " ".join(result.stderr.split())[:240]
        return f"[error] legacy .doc extraction failed{': ' + detail if detail else ''}"
    if not result.stdout.strip():
        return "[error] legacy .doc has no extractable text"
    return result.stdout
