"""_ocr.py — Apple Vision OCR wrapper (accepts file path directly, no cv2 needed)

Adapted from ENDEAVOR_VISSION/perception/ocr_reader.py. Key difference: accepts a
file path string instead of a cv2 ndarray — the Swift binary reads the file itself
(NSImage supports JPEG/PNG/HEIC natively), so no intermediate PNG write is needed.

Returns list[str] (one per text line), [] on any failure — never raises.
"""
from __future__ import annotations
import logging
import subprocess
import tempfile
import threading
from pathlib import Path

log = logging.getLogger(__name__)

_SRC = Path(__file__).resolve().parent / "_vision_ocr.swift"
_BIN = Path(tempfile.gettempdir()) / "endeavor_v2_vision_ocr"
_compiled: bool | None = None   # None=unknown, True=ready, False=unavailable (this call only)
_compile_lock = threading.Lock()


def _ensure_binary() -> bool:
    """Compile-on-demand, guarded so concurrent first calls don't race on the same
    swiftc output, and so a transient failure doesn't permanently disable OCR for
    the rest of the process lifetime — only success is cached (audit.md #9)."""
    global _compiled
    if _compiled:
        return _compiled
    with _compile_lock:
        if _compiled:  # another thread finished compiling while we waited
            return _compiled
        try:
            fresh = _BIN.exists() and _BIN.stat().st_mtime >= _SRC.stat().st_mtime
            if not fresh:
                subprocess.run(
                    ["swiftc", "-O", str(_SRC), "-o", str(_BIN)],
                    check=True, capture_output=True, timeout=120,
                    stdin=subprocess.DEVNULL,
                )
            _compiled = _BIN.exists()
        except Exception as e:
            log.warning(f"[ocr] swift helper unavailable ({e}) — OCR disabled for this call, will retry next call")
            _compiled = False
    return _compiled


def read_layout(image_path: str) -> list[dict]:
    """Run Apple Vision OCR, returning one dict per recognized text line:
    {"text": str, "x": float, "y": float, "w": float, "h": float}.
    Coordinates are the Vision boundingBox — normalized [0,1], origin BOTTOM-LEFT
    (y increases upward). [] on any failure, never raises. Used to reconstruct
    table layout from cell positions; read_text() is the plain-text view of this.
    """
    if not image_path or not _ensure_binary():
        return []
    try:
        out = subprocess.run(
            [str(_BIN), image_path],
            capture_output=True, text=True, timeout=15,
            stdin=subprocess.DEVNULL,
        )
        raw = (out.stdout or "").strip()
        if not raw or raw.startswith("ERR"):
            return []
        boxes: list[dict] = []
        for line in raw.split("\n"):
            parts = line.split("\t", 4)         # text is last → tabs in text are safe
            if len(parts) != 5:
                continue
            try:
                x, y, w, h = (float(parts[i]) for i in range(4))
            except ValueError:
                continue
            text = parts[4].strip()
            if text:
                boxes.append({"text": text, "x": x, "y": y, "w": w, "h": h})
        return boxes
    except Exception as e:
        log.debug(f"[ocr] read error: {e}")
        return []


def read_text(image_path: str) -> list[str]:
    """Run Apple Vision OCR on image_path. [] on any failure, never raises.
    Plain-text view (reading order) of read_layout()."""
    return [b["text"] for b in read_layout(image_path)]
