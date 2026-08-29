"""Best-effort macOS Accessibility snapshot; OCR remains the fallback."""
from __future__ import annotations
import json, logging, subprocess, tempfile, threading
from pathlib import Path
log = logging.getLogger(__name__)
_SRC = Path(__file__).with_name("_accessibility.swift")
_BIN = Path(tempfile.gettempdir()) / "endeavor_agent_chatgpt_accessibility"
_compiled: bool | None = None
_lock = threading.Lock()
def _ensure() -> bool:
    global _compiled
    if _compiled and _BIN.exists(): return True
    with _lock:
        if _compiled and _BIN.exists(): return True
        try:
            if not (_BIN.exists() and _BIN.stat().st_mtime >= _SRC.stat().st_mtime):
                subprocess.run(["swiftc", "-O", str(_SRC), "-o", str(_BIN), "-module-cache-path", str(Path(tempfile.gettempdir()) / "endeavor-swift-module-cache"), "-framework", "AppKit", "-framework", "ApplicationServices"], check=True, capture_output=True, timeout=120, stdin=subprocess.DEVNULL)
            _compiled = _BIN.exists()
        except Exception as exc:
            log.debug("AX helper unavailable: %s", exc); _compiled = False
    return bool(_compiled)
def read_frontmost(max_nodes: int = 220) -> dict:
    if not _ensure(): return {"status":"helper_unavailable", "elements":[]}
    try:
        r=subprocess.run([str(_BIN),str(max(1,min(int(max_nodes),500)))],capture_output=True,text=True,timeout=8,stdin=subprocess.DEVNULL)
        data=json.loads((r.stdout or "").strip())
        if not isinstance(data,dict): raise ValueError("non-object AX response")
        data.setdefault("status", "ok" if r.returncode == 0 else "helper_error"); data.setdefault("elements",[]); return data
    except subprocess.TimeoutExpired: return {"status":"timeout", "elements":[]}
    except Exception as exc: log.debug("AX read failed: %s",exc); return {"status":"helper_error", "elements":[]}
