"""Compact semantic screen state (Accessibility + OCR fusion) used by the `computer` tool."""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re

_VOLATILE = re.compile(r"^(?:\d{1,2}:\d{2}|\d+\s*(?:s|m|h)\s+ago)$", re.I)
_ACTIONABLE = {"AXButton", "AXCheckBox", "AXComboBox", "AXLink", "AXMenuButton", "AXMenuItem", "AXPopUpButton", "AXRadioButton", "AXSearchField", "AXSecureTextField", "AXSlider", "AXTextArea", "AXTextField"}

@dataclass
class ScreenElement:
    element_id: str
    text: str
    role: str
    source: str
    point: tuple[float, float]
    bounds: tuple[float, float, float, float]
    region: str
    enabled: bool | None = None
    focused: bool | None = None
    actions: tuple[str, ...] = ()
    value_digest: str = ""
    @property
    def actionable(self) -> bool:
        return self.role in _ACTIONABLE or bool(self.actions)

@dataclass
class ScreenSnapshot:
    observation_id: str
    app: str
    window: str
    elements: list[ScreenElement]
    ocr_boxes: list[dict]
    image_path: str
    capture_size: tuple[int, int]
    screen_size: tuple[float, float]
    accessibility_status: str = "unavailable"
    window_bounds: tuple[float, float, float, float] | None = None
    fingerprint: str = field(init=False)
    visual_fingerprint: str = field(init=False, default="")
    _visual_signature: bytes = field(init=False, default=b"", repr=False)
    def __post_init__(self) -> None:
        payload = [
            (e.source, e.role, e.text.casefold(), e.region, e.enabled, e.focused, e.value_digest)
            for e in self.elements if not _VOLATILE.fullmatch(e.text.strip())
        ]
        self.fingerprint = hashlib.sha256(json.dumps((self.app.casefold(), self.window.casefold(), payload), ensure_ascii=False).encode()).hexdigest()[:20]
        self._visual_signature = _visual_signature(self.image_path)
        if self._visual_signature:
            self.visual_fingerprint = hashlib.sha256(self._visual_signature).hexdigest()[:20]
    def visual_delta(self, other: "ScreenSnapshot") -> float:
        """Fraction of downsampled/quantized cells that differ; no pixels are exposed."""
        if not self._visual_signature or len(self._visual_signature) != len(other._visual_signature):
            return 0.0
        changed = sum(a != b for a, b in zip(self._visual_signature, other._visual_signature))
        return changed / len(self._visual_signature)
    def get(self, element_id: str) -> ScreenElement | None:
        return next((e for e in self.elements if e.element_id.casefold() == element_id.strip().casefold()), None)

def _visual_signature(image_path: str) -> bytes:
    """Compact visual state: 64x36 grayscale, 4-bit quantized, kept only in memory."""
    if not image_path:
        return b""
    try:
        import cv2
        frame = cv2.imread(image_path, cv2.IMREAD_GRAYSCALE)
        if frame is None:
            return b""
        small = cv2.resize(frame, (64, 36), interpolation=cv2.INTER_AREA)
        return bytes((small // 16).astype("uint8").reshape(-1).tolist())
    except Exception:
        return b""


def _region(x: float, y: float, size: tuple[float, float]) -> str:
    w, h = size; col = "left" if x < w / 3 else "right" if x > w * 2 / 3 else ""; row = "top" if y < h / 3 else "bottom" if y > h * 2 / 3 else ""
    return f"{row}-{col}" if row and col else row or col or "center"

def build_snapshot(observation_id: str, *, app: str, ocr_boxes: list[dict], ax_state: dict | None, image_path: str, capture_size: tuple[int, int], screen_size: tuple[float, float]) -> ScreenSnapshot:
    ax_state = ax_state if isinstance(ax_state, dict) else {}; raw_bounds = ax_state.get("window_bounds"); window_bounds = tuple(raw_bounds) if isinstance(raw_bounds, list) and len(raw_bounds) == 4 else None
    elements: list[ScreenElement] = []
    for raw in ax_state.get("elements", []) if isinstance(ax_state.get("elements"), list) else []:
        try:
            x, y, w, h = (float(v) for v in raw["bounds"])
            if w <= 0 or h <= 0: continue
            role = str(raw.get("role") or "AXUnknown"); actions = tuple(str(v) for v in raw.get("actions", []) if v)
            raw_value = raw.get("value")
            raw_name = raw.get("name")
            # Never let AXSecureTextField value content become observation text,
            # even if Accessibility returns something more informative than bullets.
            display_value = "" if role == "AXSecureTextField" else raw_value
            text = " ".join(str(raw_name or display_value or "").split())
            if not text and role not in _ACTIONABLE and not actions: continue
            value_digest = ""
            if role != "AXSecureTextField" and raw_value not in (None, ""):
                value_digest = hashlib.sha256(str(raw_value).encode("utf-8", errors="replace")).hexdigest()[:16]
            elements.append(ScreenElement("", text or f"<{role}>", role, "ax", (x+w/2,y+h/2), (x,y,w,h), _region(x+w/2,y+h/2,screen_size), raw.get("enabled") if isinstance(raw.get("enabled"),bool) else None, raw.get("focused") if isinstance(raw.get("focused"),bool) else None, actions, value_digest))
        except (KeyError, TypeError, ValueError): continue
    for box in ocr_boxes:
        try:
            text = " ".join(str(box.get("text") or "").split())
            if not text: continue
            x = (float(box["x"])+float(box["w"])/2)*screen_size[0]; y = (1-(float(box["y"])+float(box["h"])/2))*screen_size[1]; w=float(box["w"])*screen_size[0]; h=float(box["h"])*screen_size[1]
            if window_bounds:
                wx,wy,ww,wh=(float(v) for v in window_bounds)
                if not (wx-8 <= x <= wx+ww+8 and wy-8 <= y <= wy+wh+8): continue
            if any(e.text.casefold()==text.casefold() and (e.point[0]-x)**2+(e.point[1]-y)**2 <= 900 for e in elements): continue
            elements.append(ScreenElement("", text, "OCRText", "ocr", (x,y), (x-w/2,y-h/2,w,h), _region(x,y,screen_size)))
        except (KeyError, TypeError, ValueError): continue
    elements.sort(key=lambda e: (not e.focused, not e.actionable, e.source != "ax", e.point[1], e.point[0], e.text.casefold()))
    for i,e in enumerate(elements,1): e.element_id=f"e{i}"
    return ScreenSnapshot(
        observation_id,
        app,
        str(ax_state.get("window") or ""),
        elements,
        list(ocr_boxes),
        image_path,
        capture_size,
        screen_size,
        str(ax_state.get("status") or "unavailable"),
        tuple(float(v) for v in window_bounds) if window_bounds else None,
    )

def format_snapshot(s: ScreenSnapshot, max_chars: int = 760) -> str:
    lines=[f"[OBS {s.observation_id} app={s.app!r}"+(f" window={s.window!r}" if s.window else "")+f" ax={s.accessibility_status}]"]
    for e in s.elements:
        state=(["focused=true"] if e.focused else [])+([f"enabled={str(e.enabled).lower()}"] if e.enabled is not None else [])
        line=f"{e.element_id} role={e.role.removeprefix('AX')} text={e.text!r} region={e.region} source={e.source}"+(" "+" ".join(state) if state else "")
        if sum(map(len,lines))+len(lines)+len(line)>max_chars: lines.append(f"… {len(s.elements)-len(lines)+1} more elements omitted; inspect or scroll"); break
        lines.append(line)
    return "\n".join(lines if len(lines)>1 else lines+["(no semantic/OCR elements found)"])
