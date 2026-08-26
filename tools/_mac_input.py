"""Deterministic macOS input primitives for the `computer` tool.

This module deliberately contains no LangChain tool or agent-loop logic.  The
public operations accept/display coordinates in *points* with a top-left
origin; callers must use the mapping helpers for capture/model pixels first.
All PyObjC imports are optional so T0 tests can run with a fake Quartz module.
"""
from __future__ import annotations

import subprocess
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


try:  # Import guard: CI and non-macOS development machines need the pure math.
    import Quartz as _quartz  # type: ignore[import-not-found]
except Exception:  # pragma: no cover - exercised by import-safe unit test
    _quartz = None
try:
    from AppKit import NSScreen as _NSScreen  # type: ignore[import-not-found]
except Exception:  # pragma: no cover
    _NSScreen = None
try:
    from AppKit import NSWorkspace as _NSWorkspace  # type: ignore[import-not-found]
except Exception:  # pragma: no cover
    _NSWorkspace = None


_KEYCODES = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6,
    "x": 7, "c": 8, "v": 9, "b": 11, "q": 12, "w": 13, "e": 14,
    "r": 15, "y": 16, "t": 17, "1": 18, "2": 19, "3": 20, "4": 21,
    "6": 22, "5": 23, "=": 24, "9": 25, "7": 26, "-": 27, "8": 28,
    "0": 29, "]": 30, "o": 31, "u": 32, "[": 33, "i": 34, "p": 35,
    "l": 37, "j": 38, "'": 39, "k": 40, ";": 41, "\\": 42, ",": 43,
    "/": 44, "n": 45, "m": 46, ".": 47, "tab": 48, "space": 49,
    "backspace": 51, "escape": 53, "cmd": 55, "shift": 56, "option": 58,
    "ctrl": 59, "enter": 36, "return": 36, "delete": 51,
    "left": 123, "right": 124, "down": 125, "up": 126,
}

_MODIFIERS = {"cmd", "shift", "option", "ctrl"}
_BUTTONS = {"left", "right"}


def _require_quartz() -> Any:
    if _quartz is None:
        raise RuntimeError("Quartz/PyObjC is unavailable")
    return _quartz


def _positive(*values: float) -> None:
    if any(value <= 0 for value in values):
        raise ValueError("coordinate dimensions must be positive")


def capture_px_to_points(
    x: float, y: float, capture_width_px: float, capture_height_px: float,
    screen_width_points: float, screen_height_points: float,
) -> tuple[float, float]:
    """Map a top-left capture-pixel coordinate into primary-display points.

    Capture and display dimensions are explicit instead of assuming a 2x Retina
    factor; this also keeps the calculation correct for a 1x external display.
    """
    _positive(capture_width_px, capture_height_px, screen_width_points, screen_height_points)
    return (
        x * screen_width_points / capture_width_px,
        y * screen_height_points / capture_height_px,
    )


def model_space_to_points(
    x: float, y: float, model_width_px: float, model_height_px: float,
    capture_width_px: float, capture_height_px: float,
    screen_width_points: float, screen_height_points: float,
) -> tuple[float, float]:
    """Map top-left coordinates from a downscaled model image to display points."""
    _positive(model_width_px, model_height_px)
    capture_x = x * capture_width_px / model_width_px
    capture_y = y * capture_height_px / model_height_px
    return capture_px_to_points(
        capture_x, capture_y, capture_width_px, capture_height_px,
        screen_width_points, screen_height_points,
    )


_FLAG_NAMES = {
    "cmd": "kCGEventFlagMaskCommand", "shift": "kCGEventFlagMaskShift",
    "option": "kCGEventFlagMaskAlternate", "ctrl": "kCGEventFlagMaskControl",
}


def parse_modifier_flags(mods: str) -> int:
    """Return Quartz modifier flags (OR'd) for a "shift"/"cmd+shift"/... spelling,
    with no non-modifier key required — shared by parse_hotkey (a full hotkey)
    and click (modifier-held click, e.g. shift-click/cmd-click to multi-select)."""
    parts = [part.strip().lower() for part in mods.split("+") if part.strip()]
    unknown = [part for part in parts if part not in _MODIFIERS]
    if unknown:
        raise ValueError(f"unsupported modifier: {unknown[0]}")
    q = _require_quartz()
    flags = 0
    for modifier, flag_name in _FLAG_NAMES.items():
        if modifier in parts:
            flags |= getattr(q, flag_name)
    return flags


def parse_hotkey(combo: str) -> tuple[int, int]:
    """Return ``(keycode, Quartz modifier flags)`` for a static hotkey spelling."""
    parts = [part.strip().lower() for part in combo.split("+") if part.strip()]
    if not parts:
        raise ValueError("hotkey is empty")
    key_names = [part for part in parts if part not in _MODIFIERS]
    if len(key_names) != 1:
        raise ValueError("hotkey must contain exactly one non-modifier key")
    unknown = [part for part in parts if part not in _MODIFIERS and part not in _KEYCODES]
    if unknown:
        raise ValueError(f"unsupported key: {unknown[0]}")
    _, flags = key_names[0], parse_modifier_flags("+".join(p for p in parts if p in _MODIFIERS))
    return _KEYCODES[key_names[0]], flags


def capture(path: str) -> None:
    """Capture the primary display to a PNG path without showing UI feedback."""
    target = Path(path)
    result = subprocess.run(
        ["screencapture", "-x", "-m", str(target)], capture_output=True, timeout=5,
    )
    if result.returncode or not target.exists():
        raise RuntimeError("screencapture failed")


def image_dimensions(path: str) -> tuple[int, int]:
    """Return a captured PNG's native pixel dimensions using macOS ``sips``."""
    result = subprocess.run(["sips", "-g", "pixelWidth", "-g", "pixelHeight", path],
                            capture_output=True, text=True, timeout=5)
    values = [int(value) for value in re.findall(r"pixel(?:Width|Height):\s*(\d+)", result.stdout)]
    if result.returncode or len(values) != 2:
        raise RuntimeError("could not read capture dimensions")
    return values[0], values[1]


def primary_display_size_points() -> tuple[float, float]:
    """Return the primary display's AppKit point dimensions (not Retina pixels)."""
    if _NSScreen is None:
        raise RuntimeError("AppKit is unavailable")
    size = _NSScreen.mainScreen().frame().size
    return float(size.width), float(size.height)


def frontmost_app_name() -> str:
    """Localized name of the currently-frontmost application (e.g. "TextEdit").

    Cheap (no screenshot/OCR) — used to detect focus drifting away from the app
    a computer-use session is operating on between one action and the next, a
    real live failure mode: type/key have no self-correcting target check the
    way click does (OCR just wouldn't find a missing target), so a silently
    changed frontmost app means keystrokes land in the wrong place with no error.

    NSWorkspace's frontmostApplication is delivered via a distributed
    notification that only updates when something pumps this process's run
    loop — a plain script (no NSApplication event loop, which is exactly what
    a long-running agent server is) can read a value that's stuck at whatever
    it was on first use, live-reproduced: identical queries a full 4+ seconds
    apart in the same process both returned the app active at STARTUP, while a
    fresh process queried at the same instant read correctly. A ~50ms run-loop
    pump before reading flushes the pending notification; verified reliable
    down to 10ms live, 50ms kept for margin (negligible next to the
    screenshot+OCR round trip every caller already pays).
    """
    if _NSWorkspace is None:
        raise RuntimeError("AppKit is unavailable")
    try:
        from CoreFoundation import CFRunLoopRunInMode, kCFRunLoopDefaultMode
        CFRunLoopRunInMode(kCFRunLoopDefaultMode, 0.05, False)
    except Exception:
        pass  # best-effort — a possibly-stale read beats a crash here
    app = _NSWorkspace.sharedWorkspace().frontmostApplication()
    return str(app.localizedName()) if app is not None else ""


def failsafe_abort(radius_points: float = 2.0) -> bool:
    """True when the human has moved the pointer into the top-left abort corner."""
    q = _require_quartz()
    point = q.CGEventGetLocation(q.CGEventCreate(None))
    return point.x <= radius_points and point.y <= radius_points


def click(x: float, y: float, button: str = "left", count: int = 1, modifiers: str = "") -> None:
    """Move and click at top-left display-point coordinates using CGEvent.
    `modifiers` (e.g. "shift", "cmd", "shift+cmd") are held down for the whole
    click — shift-click/cmd-click range- or multi-select in a list/Finder,
    matching macOS's own modifier-click conventions. count=3 is a triple-click
    (select paragraph/line in most text views)."""
    if button not in _BUTTONS:
        raise ValueError(f"unsupported mouse button: {button}")
    if count not in {1, 2, 3}:
        raise ValueError("click count must be 1, 2, or 3")
    q = _require_quartz()
    flags = parse_modifier_flags(modifiers) if modifiers else 0
    point = (x, y)
    q_button = q.kCGMouseButtonLeft if button == "left" else q.kCGMouseButtonRight
    down = q.kCGEventLeftMouseDown if button == "left" else q.kCGEventRightMouseDown
    up = q.kCGEventLeftMouseUp if button == "left" else q.kCGEventRightMouseUp
    move_event = q.CGEventCreateMouseEvent(None, q.kCGEventMouseMoved, point, q_button)
    if flags:
        q.CGEventSetFlags(move_event, flags)
    q.CGEventPost(q.kCGHIDEventTap, move_event)
    for click_state in range(1, count + 1):
        down_event = q.CGEventCreateMouseEvent(None, down, point, q_button)
        up_event = q.CGEventCreateMouseEvent(None, up, point, q_button)
        if flags:
            q.CGEventSetFlags(down_event, flags)
            q.CGEventSetFlags(up_event, flags)
        if count >= 2:
            # AppKit recognizes a double/triple-click from this field, not timing alone.
            q.CGEventSetIntegerValueField(down_event, q.kCGMouseEventClickState, click_state)
            q.CGEventSetIntegerValueField(up_event, q.kCGMouseEventClickState, click_state)
        q.CGEventPost(q.kCGHIDEventTap, down_event)
        q.CGEventPost(q.kCGHIDEventTap, up_event)


def hover(x: float, y: float) -> None:
    """Move the pointer to (x, y) in top-left display points without clicking —
    reveals hover-only UI (tooltips, per-row action buttons in a list, video
    scrubbers) that a click would otherwise activate or bypass."""
    q = _require_quartz()
    moved = q.CGEventCreateMouseEvent(None, q.kCGEventMouseMoved, (x, y), q.kCGMouseButtonLeft)
    q.CGEventPost(q.kCGHIDEventTap, moved)


def drag(x1: float, y1: float, x2: float, y2: float, steps: int = 10, modifiers: str = "") -> None:
    """Press at (x1,y1), move through intermediate points to (x2,y2), then release —
    top-left display points. Posts intermediate LeftMouseDragged events (not a single
    down+up jump) so apps that recognize a drag via tracked movement, not just the two
    endpoints, register it correctly. `modifiers` (e.g. "option") are held for the
    whole gesture — e.g. option-drag to copy a file in Finder instead of moving it."""
    if steps < 1:
        raise ValueError("drag steps must be a positive integer")
    q = _require_quartz()
    flags = parse_modifier_flags(modifiers) if modifiers else 0
    q_button = q.kCGMouseButtonLeft
    down = q.CGEventCreateMouseEvent(None, q.kCGEventLeftMouseDown, (x1, y1), q_button)
    if flags:
        q.CGEventSetFlags(down, flags)
    q.CGEventPost(q.kCGHIDEventTap, down)
    for step in range(1, steps + 1):
        t = step / steps
        point = (x1 + (x2 - x1) * t, y1 + (y2 - y1) * t)
        moved = q.CGEventCreateMouseEvent(None, q.kCGEventLeftMouseDragged, point, q_button)
        if flags:
            q.CGEventSetFlags(moved, flags)
        q.CGEventPost(q.kCGHIDEventTap, moved)
    up = q.CGEventCreateMouseEvent(None, q.kCGEventLeftMouseUp, (x2, y2), q_button)
    if flags:
        q.CGEventSetFlags(up, flags)
    q.CGEventPost(q.kCGHIDEventTap, up)


def type_text(text: str) -> None:
    """Type arbitrary Unicode in one event, independent of the active keyboard layout."""
    if not text:
        return
    q = _require_quartz()
    event = q.CGEventCreateKeyboardEvent(None, 0, True)
    q.CGEventKeyboardSetUnicodeString(event, len(text), text)
    q.CGEventPost(q.kCGHIDEventTap, event)


def key(combo: str) -> None:
    """Send one static hotkey such as ``cmd+s`` or ``enter``."""
    q = _require_quartz()
    keycode, flags = parse_hotkey(combo)
    for is_down in (True, False):
        event = q.CGEventCreateKeyboardEvent(None, keycode, is_down)
        q.CGEventSetFlags(event, flags)
        q.CGEventPost(q.kCGHIDEventTap, event)


def scroll(direction: str, amount: int = 3, point: tuple[float, float] | None = None) -> None:
    """Scroll a positive number of line units in one direction (up/down/left/right).
    Scroll-wheel events land under wherever the pointer currently is, not
    wherever has keyboard focus — pass `point` (top-left display points) to
    move the pointer there first, e.g. to scroll a specific pane instead of
    whatever the pointer happened to be hovering last. left/right (added for
    wide spreadsheets/Finder column view/timelines) is a second wheel axis —
    the actual on-screen direction still follows the user's System Settings
    "natural scrolling" toggle, same as up/down already did."""
    if direction not in {"up", "down", "left", "right"}:
        raise ValueError("scroll direction must be 'up', 'down', 'left', or 'right'")
    if not isinstance(amount, int) or amount < 1:
        raise ValueError("scroll amount must be a positive integer")
    q = _require_quartz()
    if point is not None:
        moved = q.CGEventCreateMouseEvent(None, q.kCGEventMouseMoved, point, q.kCGMouseButtonLeft)
        q.CGEventPost(q.kCGHIDEventTap, moved)
    if direction in ("up", "down"):
        delta = amount if direction == "up" else -amount
        event = q.CGEventCreateScrollWheelEvent(None, q.kCGScrollEventUnitLine, 1, delta)
    else:
        delta = amount if direction == "right" else -amount
        event = q.CGEventCreateScrollWheelEvent(None, q.kCGScrollEventUnitLine, 2, 0, delta)
    q.CGEventPost(q.kCGHIDEventTap, event)


_SETTLE_SECONDS = 0.2


def settle() -> None:
    """Brief pause for a just-triggered UI transition (sheet/dialog slide-in,
    window animation) to finish before the caller screenshots+OCRs. Value is
    not tuned to a benchmark — it's a cheap insurance policy against exactly
    the race that broke a real save-dialog interaction live: typing a filename
    a beat before the sheet's field actually had focus, so the default text
    silently never got replaced."""
    time.sleep(_SETTLE_SECONDS)


def _resolve_app_path(name: str) -> str | None:
    """Best-effort fuzzy app-name resolution via Spotlight for when `open -a`
    rejects a common short name outright. Verified live (2026-07-18):
    `open -a "Chrome"` fails with "Unable to find application named 'Chrome'"
    even though the tool's own docstring lists "Chrome" as a valid example —
    the actual bundle is "Google Chrome.app". Ranks candidates exact > word-
    boundary match on the display name, then prefers a top-level
    /Applications bundle over a nested helper/uninstaller, then the shortest
    display name (closest to the plain app itself rather than a specialized
    variant, e.g. "Google Chrome" over "Chrome Remote Desktop" for "Chrome")."""
    escaped = name.replace("\\", "\\\\").replace('"', '\\"')
    try:
        result = subprocess.run(
            ["mdfind", f'kMDItemKind == "Application" && kMDItemDisplayName == "*{escaped}*"cd'],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if result.returncode:
        return None
    paths = [line for line in result.stdout.splitlines() if line.strip()]
    if not paths:
        return None
    needle = name.strip().casefold()

    def display(p: str) -> str:
        return Path(p).stem.casefold()

    exact = [p for p in paths if display(p) == needle]
    if exact:
        return exact[0]
    word_boundary = [p for p in paths if re.search(rf"\b{re.escape(needle)}\b", display(p))]
    candidates = word_boundary or paths
    candidates.sort(key=lambda p: (
        0 if Path(p).parent == Path("/Applications") else 1,
        len(display(p)),
    ))
    return candidates[0]


def open_app(name: str) -> None:
    """Open an installed macOS application by its display name. Falls back to
    a Spotlight-resolved bundle path when the exact name doesn't match
    LaunchServices directly (see _resolve_app_path)."""
    if not name.strip():
        raise ValueError("application name is empty")
    result = subprocess.run(["open", "-a", name], capture_output=True, text=True, timeout=10)
    if not result.returncode:
        return
    resolved = _resolve_app_path(name)
    if resolved is None:
        raise RuntimeError(result.stderr.strip() or f"could not open {name}")
    fallback = subprocess.run(["open", resolved], capture_output=True, text=True, timeout=10)
    if fallback.returncode:
        raise RuntimeError(fallback.stderr.strip() or f"could not open {resolved}")


def open_url(url: str, app: str = "") -> None:
    """Open an HTTP(S) URL, optionally in a named native macOS application.

    Uses argv-only ``open`` calls (never a shell).  Local files, custom URL
    schemes, and credentials embedded in a URL are rejected because this
    primitive is exposed directly to a model-facing computer-control tool.
    """
    value = url.strip()
    parsed = urlsplit(value)
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("open_url requires a valid http(s) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("open_url refuses URLs containing credentials")
    application = app.strip()
    command = ["open", "-a", application, value] if application else ["open", value]
    result = subprocess.run(command, capture_output=True, text=True, timeout=10)
    if not result.returncode:
        return
    if not application:
        raise RuntimeError(result.stderr.strip() or f"could not open {value}")
    resolved = _resolve_app_path(application)
    if resolved is None:
        raise RuntimeError(result.stderr.strip() or f"could not open {application}")
    fallback = subprocess.run(["open", "-a", resolved, value], capture_output=True, text=True, timeout=10)
    if fallback.returncode:
        raise RuntimeError(fallback.stderr.strip() or f"could not open {value} in {resolved}")
