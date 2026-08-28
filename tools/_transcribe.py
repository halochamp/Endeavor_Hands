"""_transcribe.py — native Apple Speech transcription for audio/video files

Audio and video route through the exact same SFSpeechRecognizer pipeline —
AVFoundation demuxes the audio track from video containers internally, so no
ffmpeg/extraction step is needed (confirmed empirically against a real .mov).
Thai on-device recognition only (no cloud, no model download — the language
asset ships with macOS).

Compile-on-demand .app bundle, same cache-by-mtime pattern as `_ocr.py`'s
swiftc helper, one level up: Speech (unlike Vision) requires the calling
process to be a real .app bundle with NSSpeechRecognitionUsageDescription,
launched via LaunchServices (`open -a`) — a bare binary crashes with a TCC
privacy violation even when correctly signed and run directly.

One-time machine setup (not handled silently — surfaced as actionable errors):
  1. System Settings → Apple Intelligence & Siri → enable Siri.
  2. First call pops a system consent dialog for "EndeavorSTT" — click Allow.
"""
from __future__ import annotations
import json
import logging
import subprocess
import sys
import os
import tempfile
import threading
import time
import uuid
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

log = logging.getLogger(__name__)

_SRC = Path(__file__).resolve().parent / "_transcribe_speech.swift"
_APP_DIR = Path(tempfile.gettempdir()) / "EndeavorSTT.app"
_BIN = _APP_DIR / "Contents" / "MacOS" / "EndeavorSTT"
_PLIST = _APP_DIR / "Contents" / "Info.plist"
_compiled: bool | None = None
_compile_lock = threading.Lock()

_AUDIO_EXT = {".m4a", ".mp3", ".wav", ".aiff", ".aif", ".caf", ".flac", ".aac"}
_VIDEO_EXT = {".mp4", ".mov", ".m4v"}

_INFO_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key>
    <string>EndeavorSTT</string>
    <key>CFBundleIdentifier</key>
    <string>com.endeavor.stt</string>
    <key>CFBundleName</key>
    <string>EndeavorSTT</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>NSSpeechRecognitionUsageDescription</key>
    <string>Transcribe local audio/video files for Endeavor Hands</string>
</dict>
</plist>
"""


def _ensure_bundle() -> bool:
    """Compile-on-demand, guarded so concurrent first calls don't race on the
    same swiftc/codesign output, and so a transient failure (missing
    swiftc/codesign on PATH, disk hiccup) doesn't permanently disable the
    feature for the rest of the process lifetime — only success is cached."""
    global _compiled
    if _compiled:
        return _compiled
    with _compile_lock:
        if _compiled:  # another thread finished compiling while we waited
            return _compiled
        try:
            fresh = _BIN.exists() and _BIN.stat().st_mtime >= _SRC.stat().st_mtime
            if not fresh:
                _BIN.parent.mkdir(parents=True, exist_ok=True)
                _PLIST.write_text(_INFO_PLIST, encoding="utf-8")
                subprocess.run(
                    ["swiftc", "-O", str(_SRC), "-o", str(_BIN)],
                    check=True, capture_output=True, timeout=120,
                )
                subprocess.run(
                    ["codesign", "--force", "--deep", "-s", "-", str(_APP_DIR)],
                    check=True, capture_output=True, timeout=30,
                )
            _compiled = _BIN.exists()
        except Exception as e:
            log.warning(f"[_transcribe] swift bundle unavailable ({e})")
            _compiled = False
    return _compiled


def _probe_duration_sec(path: str) -> float | None:
    """Best-effort duration via afinfo (ships with macOS; reads audio + video
    containers). Returns None on failure — caller treats that as unknown,
    not an error."""
    try:
        out = subprocess.run(["afinfo", path], capture_output=True, text=True, timeout=15)
        for line in out.stdout.splitlines():
            line = line.strip()
            if line.startswith("estimated duration:"):
                return float(line.split(":", 1)[1].strip().split()[0])
    except Exception:
        pass
    return None


def transcribe_media(path: str) -> str:
    """Transcribe an audio/video file via native on-device Speech recognition.
    Raises RuntimeError with an actionable message on failure — the caller
    (read_file) formats the final [error] string (Tool Output Contract)."""
    from config import READ_FILE_AUDIO_VIDEO_MAX_DURATION_SEC

    if not _ensure_bundle():
        raise RuntimeError(
            "native Speech helper unavailable — swiftc/codesign failed (check Xcode Command Line Tools)"
        )

    abs_path = str(Path(path).resolve())
    duration = _probe_duration_sec(abs_path)
    if duration is not None and duration > READ_FILE_AUDIO_VIDEO_MAX_DURATION_SEC:
        mins = duration / 60
        limit_min = READ_FILE_AUDIO_VIDEO_MAX_DURATION_SEC / 60
        raise RuntimeError(f"recording too long: {mins:.0f} min (limit {limit_min:.0f} min)")

    # Generous wait budget: real transcription time + headroom for a first-run
    # consent click — a process that exits at its deadline before the click
    # registers looks identical to a permanent hang (confirmed empirically).
    # When afinfo can't read duration, size off the policy cap itself (not a
    # short fixed guess) — a real long file still gets a fair shot, and if it
    # truly exceeds the cap, the Swift helper's own internal deadline
    # (= wait_budget) enforces the limit by timing out cleanly instead of
    # either being killed early or running unbounded.
    wait_budget = max(120, int((duration or READ_FILE_AUDIO_VIDEO_MAX_DURATION_SEC) * 1.5) + 90)

    out_json = Path(tempfile.gettempdir()) / f"endeavor_stt_{uuid.uuid4().hex[:8]}.json"
    try:
        # -n without -W: `open` hands off to LaunchServices and returns almost
        # immediately. We poll for out_json ourselves so that on timeout we
        # can kill the actual helper process — `open -W`'s wait is on a
        # process LaunchServices owns, not a child of `open`, so a subprocess
        # timeout on `open` alone leaves the real helper orphaned forever.
        try:
            subprocess.run(
                ["open", "-n", "-a", str(_APP_DIR), "--args",
                 abs_path, str(out_json), str(wait_budget)],
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            subprocess.run(["pkill", "-f", str(out_json)], capture_output=True)
            raise RuntimeError("failed to launch Speech helper (open timed out)")

        deadline = time.monotonic() + wait_budget + 30
        while not out_json.exists() and time.monotonic() < deadline:
            time.sleep(0.3)
        if not out_json.exists():
            subprocess.run(["pkill", "-f", str(out_json)], capture_output=True)
            raise RuntimeError("transcription timed out — helper process killed")
        result = json.loads(out_json.read_text(encoding="utf-8"))
    finally:
        out_json.unlink(missing_ok=True)

    err = result.get("error")
    if err:
        low = err.lower()
        if "siri" in low and "disabled" in low:
            raise RuntimeError(
                f"{err} — เปิด Siri ที่ System Settings → Apple Intelligence & Siri แล้วลองใหม่"
            )
        if "not authorized" in low:
            raise RuntimeError(
                f"{err} — กดอนุญาต Speech Recognition popup ของ \"EndeavorSTT\" "
                f"(System Settings → Privacy & Security → Speech Recognition) แล้วลองใหม่"
            )
        raise RuntimeError(err)

    text = (result.get("text") or "").strip()
    if not text:
        raise RuntimeError("transcription returned empty text — file may have no speech or be silent")
    return text
