"""Single-step, guarded native-computer actions exposed as the `computer` MCP tool."""
from __future__ import annotations

import difflib
import re
import tempfile
import json
import os
import time
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from . import _mac_input as _backend
from ._ocr import read_layout as _ocr_layout
from .read_file import _sample_coverage
from ._screen_state import ScreenElement, ScreenSnapshot, build_snapshot, format_snapshot
from tools._progress import progress as _progress, phase as _phase

_ACTION_ATTEMPTS = [0]
_ACTION_MAX = 15
_OBSERVATION_ATTEMPTS = [0]
_OBSERVATION_MAX = 36
# Per-turn overrides — set by graph.py for turns that run with no human watching
# (e.g. an AWAKE-fired background turn). Both reset to defaults every real turn
# by reset_computer_guards() so a tightened scope never leaks into a later,
# normal (human-supervised) turn.
_ACTION_MAX_OVERRIDE = [None]
_OBSERVATION_MAX_OVERRIDE = [None]
_FILE_DELETION_FORBIDDEN = os.getenv("V2_DENY_FILE_DELETION", "").strip().lower() in {"1", "true", "yes"}
_DESTRUCTIVE_GUARD = [_FILE_DELETION_FORBIDDEN]
# Two tiers, not one — ported from MAX 2026-07-21 (live-reproduced there as
# audit F1): a single broad list wrongly catches "Format" (a menu-bar text-
# formatting item, not disk-erase) and "Trash" (Finder sidebar item — opening
# it to LOOK is not emptying it) the moment the destructive guard becomes the
# DEFAULT for ordinary human-supervised turns. Those words are genuinely
# destructive only in narrow contexts (Disk Utility's "Format..." button,
# "Empty Trash") that plain substring matching can't distinguish from the
# common benign case. _DESTRUCTIVE_MARKERS_SUPERVISED (default-guard turns,
# where over-blocking directly breaks ordinary work) omits them;
# _DESTRUCTIVE_MARKERS_UNSUPERVISED (awake-fired turns, where nobody is
# watching and maximum caution is worth the false-positive rate) keeps the
# full original list. Selected in the check itself via whether action_max
# was set (awake-fired always sets it; the normal-turn default guard never
# does) — no new per-turn flag needed, that distinction already exists.
_DESTRUCTIVE_MARKERS_SUPERVISED = (
    "delete", "ลบ", "remove", "เอาออก",
    "uninstall", "ถอนการติดตั้ง", "ลบล้าง", "ล้างข้อมูล", "empty trash", "move to trash",
)
_DESTRUCTIVE_MARKERS_UNSUPERVISED = _DESTRUCTIVE_MARKERS_SUPERVISED + (
    "trash", "ถังขยะ", "erase", "format", "wipe",
)
_LAST_SIGNATURE = [""]
# OCR text as of the end of the last action — stands in for "the screen right
# now, before this call" (nothing else touches the display between agent
# turns), so a repeat check needs no extra pre-action capture on the common path.
_LAST_SCREEN_TEXT = [""]
# Frontmost app as of the end of the last action. Live-reproduced failure mode
# (2026-07-17): something outside the agent's control (another app, the host
# IDE re-activating itself) can steal frontmost focus between two computer()
# calls. click/double_click/right_click self-correct for this by construction
# (they re-locate the target via a fresh OCR pass every time — worst case they
# error "text not found" or, more subtly, click a same-labeled element in the
# WRONG app), but type/key have no such check at all: they blindly send
# keystrokes to whatever currently has focus, so a silent focus change means
# keystrokes land in an unintended app with no error. Checked for every
# context-dependent action, not just type/key, since a click on a wrong-app
# element with a matching label is exactly as silent a failure.
_LAST_FRONTMOST_APP = [""]
# Target text used to locate the most recently clicked element (audit F3) —
# e.g. target="Password" when the model clicks a password field via its
# visible placeholder/label. A whole-screen scan for password markers would
# refuse ordinary typing on any login-adjacent screen (a visible "Password"
# label next to the username field you're legitimately filling) since OCR
# can't tell which field is actually focused; the click target the model
# itself chose is a much more precise proxy for "the field about to receive
# text" than anything scanned from the screen at large.
_LAST_CLICK_TARGET = [""]
_CONTEXT_DEPENDENT_ACTIONS = {"click", "double_click", "triple_click", "right_click", "type", "key", "scroll", "drag", "hover"}
# A visible clock changes the OCR text every minute even though nothing about
# the screen actually changed — without filtering it out, the repeat-guard's
# raw-text comparison below would only ever catch a repeat within the same
# wall-clock minute (audit F7). Same pattern as awake_engine.py's
# _VOLATILE_LINE/_screen_hash for the same reason; duplicated rather than
# imported since tools/ and the host-level awake_engine.py are different tiers.
_VOLATILE_LINE = re.compile(r"\b\d{1,2}:\d{2}\b")
_ACTION_LOG = Path(__file__).resolve().parents[1] / "logs" / "computer_actions.jsonl"
# Screen-facing text is OCR-dense/low-signal-per-char (menu bars, file trees) —
# capped well under CLAUDE.md's Tool Output Contract ceiling since this tool
# fires far more often per task than a single read_image/web call.
_SUMMARY_MAX_CHARS = 500
_PASSWORD_MARKERS = ("password", "รหัสผ่าน")
_OBSERVATION_SEQ = [0]
_LATEST_OBSERVATION: list[ScreenSnapshot | None] = [None]
_ACTION_LOCK = threading.Lock()

# Image handoff to the MCP layer (replaces the old LangGraph "queue this image
# for the model's next turn" mechanism — an MCP tool call returns its image
# immediately in the same response, there is no next-turn concept). server.py
# calls pop_last_image_path() right after _computer_impl() returns and attaches
# the file as an MCP image content block. `ephemeral=True` means the path is a
# one-off crop (from `inspect`) the caller should delete after reading it;
# `ephemeral=False` means it's the live observation image, still referenced by
# _LATEST_OBSERVATION for future element_id lookups, so the caller must NOT
# delete it.
_LAST_RESULT_IMAGE: list[tuple[str, bool]] = [("", False)]


def _set_result_image(path: str, *, ephemeral: bool = False) -> None:
    _LAST_RESULT_IMAGE[0] = (path, ephemeral)


def pop_last_image_path() -> tuple[str, bool]:
    """Return and clear (path, ephemeral) for the image produced by the most
    recent _computer_impl call, if any. "" if the call produced no image."""
    path, ephemeral = _LAST_RESULT_IMAGE[0]
    _LAST_RESULT_IMAGE[0] = ("", False)
    return path, ephemeral


def reset_computer_guards() -> None:
    _ACTION_ATTEMPTS[0] = 0
    _OBSERVATION_ATTEMPTS[0] = 0
    _LAST_SIGNATURE[0] = ""
    _LAST_SCREEN_TEXT[0] = ""
    _LAST_FRONTMOST_APP[0] = ""
    _LAST_CLICK_TARGET[0] = ""
    _ACTION_MAX_OVERRIDE[0] = None
    _OBSERVATION_MAX_OVERRIDE[0] = None
    _DESTRUCTIVE_GUARD[0] = _FILE_DELETION_FORBIDDEN
    previous = _LATEST_OBSERVATION[0]
    if previous:
        Path(previous.image_path).unlink(missing_ok=True)
    _LATEST_OBSERVATION[0] = None


def set_computer_turn_scope(
    action_max: int | None = None,
    block_destructive: bool = False,
    observation_max: int | None = None,
) -> None:
    """Tighten this turn's computer-use ceilings below the normal defaults.

    Mutation actions and read-only observations have separate bounded budgets so
    diagnosis does not consume the same safety ceiling as click/type/key/drag.
    Unsupervised callers that already pass ``action_max`` automatically get a
    proportionally tighter observation ceiling unless they provide one explicitly.
    Both overrides reset at the next real turn.
    """
    _ACTION_MAX_OVERRIDE[0] = action_max
    _OBSERVATION_MAX_OVERRIDE[0] = (
        observation_max
        if observation_max is not None
        else max(action_max * 2, action_max) if action_max is not None else None
    )
    _DESTRUCTIVE_GUARD[0] = _FILE_DELETION_FORBIDDEN or block_destructive


def _effective_mutation_max() -> int:
    return _ACTION_MAX_OVERRIDE[0] if _ACTION_MAX_OVERRIDE[0] is not None else _ACTION_MAX


def _effective_observation_max() -> int:
    return (
        _OBSERVATION_MAX_OVERRIDE[0]
        if _OBSERVATION_MAX_OVERRIDE[0] is not None
        else _OBSERVATION_MAX
    )


def _observation_budget_available() -> bool:
    return _OBSERVATION_ATTEMPTS[0] < _effective_observation_max()


def _next_observation_id() -> str:
    _OBSERVATION_SEQ[0] += 1
    return f"obs_{_OBSERVATION_SEQ[0]}"


def _publish(snapshot: ScreenSnapshot) -> ScreenSnapshot:
    previous = _LATEST_OBSERVATION[0]
    if previous is not None and previous is not snapshot:
        Path(previous.image_path).unlink(missing_ok=True)
    _LATEST_OBSERVATION[0] = snapshot
    _queue_latest_capture(snapshot.image_path)
    return snapshot


def _normalize_combo(combo: str) -> str:
    return "+".join(sorted(part.strip().lower() for part in combo.split("+") if part.strip()))


# Dangerous hotkeys refused outright regardless of context (quit/logout).
# Stored pre-normalized so lookups are a plain set-membership check.
_FORBIDDEN_KEY_COMBOS = {_normalize_combo(c) for c in ("cmd+q", "cmd+shift+q")}

# `key`'s text is a literal hotkey combo, not a button label — a bare Backspace/
# Delete keystroke while editing text is an ordinary, extremely common edit
# action (fixing a typo), not the same risk as clicking a "Delete"/"Empty Trash"
# BUTTON. Loose substring matching against the marker lists would catch
# "delete" in a plain Backspace/Delete press and block routine text editing —
# so the destructive guard checks `key` against this separate, exact,
# normalized-combo set instead (macOS's actual "permanently remove"-flavored
# hotkeys), never by substring. click/double_click/right_click/drag still use
# the marker lists (their target/text IS a visible label, where a loose
# match is the right call).
_DESTRUCTIVE_KEY_COMBOS = {
    _normalize_combo(c) for c in ("cmd+delete", "cmd+backspace", "shift+delete", "cmd+shift+delete")
}


def _cap_for_agent(text: str) -> str:
    return _sample_coverage(text, "screen", max_chars=_SUMMARY_MAX_CHARS)


def _stable_signature(text: str) -> str:
    """`text` with volatile lines (e.g. a menu-bar clock) dropped, for the
    repeat-guard's screen-unchanged comparison — two captures a minute apart
    of a genuinely static screen must compare equal even though the clock
    line differs (audit F7)."""
    return "\n".join(line for line in text.splitlines()
                     if line.strip() and not _VOLATILE_LINE.search(line))


def _error(message: str) -> str:
    return f"[error] {message}"


# Reuses read_image's region vocabulary (region=<...> for zoom) for consistency.
# Boxes are normalized [0,1], origin BOTTOM-LEFT (Vision convention) — "top"
# means high y, not low.
def _in_region(box: dict, region: str) -> bool:
    cx = float(box["x"]) + float(box["w"]) / 2
    cy = float(box["y"]) + float(box["h"]) / 2
    if region == "top":
        return cy > 2 / 3
    if region == "bottom":
        return cy < 1 / 3
    if region == "left":
        return cx < 1 / 3
    if region == "right":
        return cx > 2 / 3
    if region == "center":
        return 1 / 3 <= cx <= 2 / 3 and 1 / 3 <= cy <= 2 / 3
    if region == "top-left":
        return cy > 2 / 3 and cx < 1 / 3
    if region == "top-right":
        return cy > 2 / 3 and cx > 2 / 3
    if region == "bottom-left":
        return cy < 1 / 3 and cx < 1 / 3
    if region == "bottom-right":
        return cy < 1 / 3 and cx > 2 / 3
    return True  # unrecognized region string — no filtering, caller sees the plain ambiguity error


def _fuzzy_suggestions(boxes: list[dict], needle: str, limit: int = 3) -> str:
    """Ranked near-miss candidates for a not-found target — suggestion text
    only, never used to resolve a click point. OCR/label text can differ from
    the model's guess by a typo or minor normalization (e.g. smart quotes,
    truncated ellipsis); difflib.get_close_matches surfaces the likely intent
    without ever silently substituting it, so a typo'd target can never
    resolve past the destructive-action guard below (that guard only sees
    the model's own `target` string, not a fuzzy match)."""
    if not needle:
        return ""
    # One canonical (first-seen) spelling per casefold value — two on-screen
    # labels differing only by case (e.g. a button "Remove" and, elsewhere,
    # OCR/a tooltip rendering "REMOVE") are the same suggestion to a model
    # choosing what to click next, and must not consume two of `limit` slots.
    by_casefold: dict[str, str] = {}
    for b in boxes:
        text = str(b.get("text", "")).strip()
        if text:
            by_casefold.setdefault(text.casefold(), text)
    close = difflib.get_close_matches(needle, list(by_casefold), n=limit, cutoff=0.6)
    return ", ".join(f"'{by_casefold[c]}'" for c in close)


def _find_target(boxes: list[dict], target: str, near: str = "") -> tuple[dict | None, str | None]:
    needle = target.strip().casefold()
    matches = [box for box in boxes if needle and needle in str(box.get("text", "")).casefold()]
    if not matches:
        suggestion = _fuzzy_suggestions(boxes, needle)
        hint = f" — did you mean: {suggestion}?" if suggestion else ""
        return None, _error(f"text not found: {target}{hint}")
    if len(matches) > 1:
        # Prefer a higher-precision match before falling back to raw substring
        # disambiguation — e.g. a "Save" button vs. a sentence that merely
        # contains "save" as a substring should resolve to the button alone,
        # with no need for the caller to supply near= at all.
        exact = [box for box in matches if str(box.get("text", "")).strip().casefold() == needle]
        if len(exact) == 1:
            return exact[0], None
        candidates = exact or [
            box for box in matches
            if re.search(rf"\b{re.escape(needle)}\b", str(box.get("text", "")).casefold())
        ]
        if len(candidates) == 1:
            return candidates[0], None
        if candidates:
            matches = candidates
    if len(matches) > 1 and near:
        # Real live failure this disambiguates (2026-07-17): two elements with
        # the literal same label/text — no target string, however precise,
        # can distinguish them, only screen position can (e.g. two identically
        # labeled buttons in different steps of a wizard-style dialog).
        narrowed = [box for box in matches if _in_region(box, near)]
        if len(narrowed) == 1:
            return narrowed[0], None
        if narrowed:
            matches = narrowed
    if len(matches) > 1:
        labels = ", ".join(str(box.get("text", "")) for box in matches[:3])
        hint = "" if near else ' — add near="top/bottom/left/right/center/top-left/top-right/bottom-left/bottom-right"'
        return None, _error(f"'{target}' matches {len(matches)} locations: {labels}{hint}")
    return matches[0], None


def _queue_latest_capture(path: str) -> None:
    """Record this screenshot as the image the MCP layer should attach to this
    call's result. A computer action only retains its newest view — the live
    observation image, not a one-off crop, so ephemeral=False (see
    _set_result_image): the caller must not delete it, _LATEST_OBSERVATION
    still needs it for element_id lookups on the next call."""
    _set_result_image(path, ephemeral=False)


def _log_action(action: str, target: str = "", point: tuple[float, float] | None = None) -> None:
    """Append an auditable action record; never persist typed text."""
    try:
        _ACTION_LOG.parent.mkdir(parents=True, exist_ok=True)
        record = {"ts": datetime.now(timezone.utc).isoformat(), "action": action, "target": target[:160]}
        if point is not None:
            record["point"] = [round(point[0], 2), round(point[1], 2)]
        with _ACTION_LOG.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _target_point(target: str, near: str) -> tuple[tuple[float, float] | None, str | None]:
    """Locate `target` via a fresh OCR pass and return its center in display points.
    Shared by click/scroll(target=)/drag so every text-targeted action resolves
    coordinates the same way — OCR box -> capture pixels -> display points."""
    boxes, _, capture_width, capture_height, screen_width, screen_height = _observe()
    box, problem = _find_target(boxes, target, near)
    if problem:
        return None, problem
    # Vision y is bottom-left normalized; CGEvent/display coordinates are top-left.
    capture_x = (float(box["x"]) + float(box["w"]) / 2) * capture_width
    capture_y = (1 - (float(box["y"]) + float(box["h"]) / 2)) * capture_height
    point = _backend.capture_px_to_points(capture_x, capture_y, capture_width, capture_height,
                                           screen_width, screen_height)
    return point, None


def _capture_snapshot(notify: bool = True) -> ScreenSnapshot:
    """Capture one retained, full-resolution state and queue it for the VLM.

    `notify=False` skips the per-call progress() emits — used by
    _post_action_snapshot's tight poll loop, where repeating "screenshot…/
    OCR…" every ~120ms is pure noise and, on Telegram, real edit_text() calls
    that can trip flood control."""
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
        path = handle.name
    try:
        if notify:
            _progress("กำลัง screenshot…")
        _backend.capture(path)
        if notify:
            _progress("Apple Vision OCR…")
        boxes = _ocr_layout(path)
        capture_width, capture_height = _backend.image_dimensions(path)
        screen_width, screen_height = _backend.primary_display_size_points()
        try:
            from ._accessibility import read_frontmost
            ax_state = read_frontmost()
        except Exception:
            ax_state = {"status": "unavailable", "elements": []}
        if ax_state.get("status") == "permission_required":
            raise PermissionError(
                "macOS Accessibility permission is required for computer control. "
                "Open System Settings → Privacy & Security → Accessibility and enable the app "
                "that launched this server (usually Terminal; also add tunnel-client or python3 "
                "if you launch it directly), then retry"
            )
        return build_snapshot(
            _next_observation_id(), app=_backend.frontmost_app_name(), ocr_boxes=boxes,
            ax_state=ax_state, image_path=path, capture_size=(capture_width, capture_height),
            screen_size=(screen_width, screen_height),
        )
    except Exception:
        Path(path).unlink(missing_ok=True)
        raise


def _observe() -> tuple[list[dict], str, int, int, float, float]:
    """Compatibility helper for OCR-only target lookups; does not retain files."""
    snapshot = _capture_snapshot()
    try:
        text = "\n".join(str(box.get("text", "")) for box in snapshot.ocr_boxes if box.get("text"))
        _queue_latest_capture(snapshot.image_path)
        return (
            snapshot.ocr_boxes, text or "[no OCR text found]",
            snapshot.capture_size[0], snapshot.capture_size[1],
            snapshot.screen_size[0], snapshot.screen_size[1],
        )
    finally:
        Path(snapshot.image_path).unlink(missing_ok=True)


_POST_ACTION_TIMEOUT = 1.2
_POST_ACTION_SHORT_TIMEOUT = 0.9
_POST_ACTION_OPEN_TIMEOUT = 2.4
_POST_ACTION_POLL = 0.10
_POST_ACTION_POLL_MAX = 0.40
_VISUAL_CHANGE_MIN_FRACTION = 0.003
# Extra bounded wait applied ONLY when the caller passed expect= or an
# open_app/open_url app= — both name an async condition (a page finishing
# load, an app becoming frontmost) that routinely takes longer than the
# ordinary 1.2s settle window every other action pays. Every action still
# pays the base 1.2s; this only postpones giving up when there's a specific,
# still-unsatisfied thing to wait for, never used to cut the loop short early
# (see the break condition below — expect/app satisfied is an ADDITIONAL
# requirement to stop, not an alternative to changed+stable, so an early
# expect match still can't return a mid-animation frame).
_POST_ACTION_EXTENDED_TIMEOUT = 4.0


def _screen_change_signals(before: ScreenSnapshot, current: ScreenSnapshot) -> tuple[bool, bool]:
    semantic_changed = current.fingerprint != before.fingerprint
    visual_changed = current.visual_delta(before) >= _VISUAL_CHANGE_MIN_FRACTION
    return semantic_changed, visual_changed


def _base_post_action_timeout(action: str) -> float:
    if action in {"open_app", "open_url"}:
        return _POST_ACTION_OPEN_TIMEOUT
    if action in {"click", "double_click", "triple_click", "right_click", "hover", "type"}:
        return _POST_ACTION_SHORT_TIMEOUT
    return _POST_ACTION_TIMEOUT


def _app_matches(requested_app: str, actual_app: str) -> bool:
    # `open -a` can return 0 without the target ever becoming frontmost, and
    # a localized/short app name can legitimately differ from the requested
    # string in either direction (target="Visual Studio Code" vs. real name
    # "Code"; target="Chrome" vs. "Google Chrome") — checked both directions.
    t, a = requested_app.strip().casefold(), actual_app.casefold()
    return t in a or a in t


def _post_action_snapshot(
    before: ScreenSnapshot, *, expect: str = "", requested_app: str = "", action: str = "",
) -> tuple[ScreenSnapshot, str]:
    """Poll screen state until it changes and stabilizes, or the timeout expires.

    Ported from MAX 2026-07-21: a single post-action capture (the previous
    behavior here) risked photographing a slow transition — app launch, a
    save-sheet's slide-in animation, a page still loading — mid-animation.
    On MAX that's recoverable (OCR text is a second channel independent of
    the frame), but on this fork the screenshot IS the model's only view of
    the result, so a half-finished frame is a worse silent-failure class here,
    not a milder one.

    expect=/requested_app= extend (never shorten) how long this waits: a
    still-unsatisfied expect or app-frontmost check keeps polling up to
    _POST_ACTION_EXTENDED_TIMEOUT instead of giving up at 1.2s — real-usage
    reports showed `open_url`/`key(enter)` flagged app_not_frontmost/
    expectation_not_met while the very screenshot returned alongside the
    warning showed the action had, in fact, already succeeded a moment later."""
    _progress("กำลังตรวจสอบผลลัพธ์บนหน้าจอ…")
    waiting_on_condition = bool(expect) or bool(requested_app)
    base_timeout = max(0.0, _base_post_action_timeout(action))
    started = time.monotonic()
    deadline = started + base_timeout
    extended_deadline = (
        started + max(base_timeout, _POST_ACTION_EXTENDED_TIMEOUT)
        if waiting_on_condition else deadline
    )
    chosen: ScreenSnapshot | None = None
    previous_state: tuple[str, str] | None = None
    semantic_changed = False
    visual_changed = False
    stable_hits = 0
    poll_delay = max(0.0, _POST_ACTION_POLL)
    while True:
        current = _capture_snapshot(notify=False)
        semantic_now, visual_now = _screen_change_signals(before, current)
        semantic_changed = semantic_changed or semantic_now
        visual_changed = visual_changed or visual_now
        current_state = (current.fingerprint, current.visual_fingerprint)
        stable_hits = stable_hits + 1 if previous_state is not None and current_state == previous_state else 1
        previous_state = current_state
        if chosen is not None and chosen is not current:
            Path(chosen.image_path).unlink(missing_ok=True)
        chosen = current
        now = time.monotonic()
        condition_pending = (
            (bool(expect) and not _expected(current, expect))
            or (bool(requested_app) and not _app_matches(requested_app, current.app))
        )
        changed = semantic_changed or visual_changed
        if changed and stable_hits >= 2 and not condition_pending:
            break
        if now >= deadline and not condition_pending:
            break
        if now >= extended_deadline:
            break
        time.sleep(poll_delay)
        poll_delay = min(_POST_ACTION_POLL_MAX, max(_POST_ACTION_POLL, poll_delay * 2))
    assert chosen is not None
    return _publish(chosen), ("changed" if changed else "no_visible_change")


def _result(prefix: str, snapshot: ScreenSnapshot, effect: str = "") -> str:
    head = f"{prefix} effect={effect}" if effect else prefix
    out = f"{head}\n{format_snapshot(snapshot, max_chars=720)}"
    return out if len(out) <= _SUMMARY_MAX_CHARS else out[:_SUMMARY_MAX_CHARS - 34].rstrip() + "\n… observation truncated"


def _point_from_snapshot(snapshot: ScreenSnapshot, target: str, near: str) -> tuple[tuple[float, float] | None, str | None]:
    box, problem = _find_target(snapshot.ocr_boxes, target, near)
    if problem:
        return None, problem
    x = (float(box["x"]) + float(box["w"]) / 2) * snapshot.capture_size[0]
    y = (1 - (float(box["y"]) + float(box["h"]) / 2)) * snapshot.capture_size[1]
    return _backend.capture_px_to_points(x, y, *snapshot.capture_size, *snapshot.screen_size), None


def _inspect(observation_id: str, element_id: str, question: str) -> str:
    """Zoom into `element_id` (or the whole observation if omitted) and hand the
    image back directly via _set_result_image — no local-VLM description step.
    ChatGPT is the vision model; it looks at the attached crop itself instead of
    reading another model's text description of it (this is the one edit this
    function needed — everything else about locating/cropping is unchanged)."""
    latest = _LATEST_OBSERVATION[0]
    if latest is None or latest.observation_id != observation_id:
        return _error("inspect requires the latest observation_id from [OBS]")
    path = latest.image_path
    crop = ""
    try:
        element = latest.get(element_id) if element_id else None
        if element_id and element is None:
            return _error(f"element not found in {observation_id}: {element_id}")
        if element:
            import cv2
            frame = cv2.imread(path)
            if frame is None: return _error("retained screenshot is unreadable")
            sx, sy = latest.capture_size[0] / latest.screen_size[0], latest.capture_size[1] / latest.screen_size[1]
            x, y, w, h = element.bounds; pad = max(w, h, 80.0)
            x0, y0 = max(0, round((x-pad)*sx)), max(0, round((y-pad)*sy)); x1, y1 = min(frame.shape[1], round((x+w+pad)*sx)), min(frame.shape[0], round((y+h+pad)*sy))
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle: crop = handle.name
            if x1 <= x0 or y1 <= y0 or not cv2.imwrite(crop, frame[y0:y1, x0:x1]): return _error("could not make inspect crop")
            path = crop
        # ephemeral=bool(crop): a fresh crop is single-use (the MCP layer deletes
        # it after reading); the full observation image is not (still needed for
        # future element_id lookups) — same distinction _queue_latest_capture uses.
        _set_result_image(path, ephemeral=bool(crop))
        return (
            f"[ok] inspect observation={observation_id}"
            + (f" element={element_id}" if element_id else "")
            + " — zoomed image attached, look at it directly"
            + (f" to answer: {question.strip()}" if question.strip() else "")
        )
    except Exception as exc:
        if crop: Path(crop).unlink(missing_ok=True)
        return _error(f"inspect failed: {exc}")


_EXPECT_KINDS = {"app", "window", "text", "focus"}


def _normalize_visible_text(value: str) -> str:
    """Exact, rendering-tolerant text normalization for verification only.

    Whitespace is collapsed and OCR-introduced spaces immediately before common
    punctuation are removed. This intentionally does not use fuzzy/similarity
    matching: the normalized expected text must still be an exact substring of
    one observed line or of the ordered OCR text stream.
    """
    text = " ".join(str(value or "").casefold().split())
    text = re.sub(r"\s+([:;,.!?])", r"\1", text)
    text = re.sub(r"([\[(])\s+", r"\1", text)
    text = re.sub(r"\s+([\])])", r"\1", text)
    return text


def _ordered_ocr_text(snapshot: ScreenSnapshot) -> str:
    """Return frontmost-window OCR in deterministic reading order without fuzziness.

    Vision OCR covers the whole screenshot, so it must be clipped to Accessibility's
    focused-window bounds before it can support ``expect=text``. Without trustworthy
    bounds, whole-screen OCR is deliberately unavailable for verification rather than
    risking a false positive from a background window.
    """
    if snapshot.window_bounds is None:
        return ""
    wx, wy, ww, wh = snapshot.window_bounds
    screen_w, screen_h = snapshot.screen_size
    boxes: list[tuple[float, float, str]] = []
    for box in snapshot.ocr_boxes:
        try:
            text = str(box.get("text") or "").strip()
            if not text:
                continue
            x = float(box["x"])
            y = float(box["y"])
            w = float(box["w"])
            h = float(box["h"])
            center_x = (x + w / 2) * screen_w
            center_y = (1 - (y + h / 2)) * screen_h
            if not (wx - 8 <= center_x <= wx + ww + 8 and wy - 8 <= center_y <= wy + wh + 8):
                continue
            boxes.append((y + h / 2, x, text))
        except (KeyError, TypeError, ValueError):
            continue
    if not boxes:
        return ""

    # Vision uses bottom-left coordinates; higher y is visually higher. Group
    # neighboring boxes into lines, then concatenate those lines top-to-bottom.
    lines: list[tuple[float, list[tuple[float, str]]]] = []
    for y, x, text in sorted(boxes, key=lambda item: (-item[0], item[1])):
        match = next((line for line in lines if abs(line[0] - y) <= 0.015), None)
        if match is None:
            lines.append((y, [(x, text)]))
        else:
            match[1].append((x, text))
    rendered = [
        " ".join(text for _, text in sorted(parts, key=lambda item: item[0]))
        for _, parts in sorted(lines, key=lambda item: -item[0])
    ]
    return _normalize_visible_text(" ".join(rendered))


def _expect_text_visible(snapshot: ScreenSnapshot, needle: str) -> bool | None:
    normalized = _normalize_visible_text(needle)
    if not normalized:
        return False
    # AX traversal starts at the focused window, so AX text is safe evidence even
    # when explicit window bounds are missing. OCR elements are not safe in that
    # situation because the capture itself spans the whole screen.
    if any(
        e.source == "ax" and normalized in _normalize_visible_text(e.text)
        for e in snapshot.elements
    ):
        return True
    if snapshot.window_bounds is None:
        return None
    ordered_ocr = _ordered_ocr_text(snapshot)
    return bool(ordered_ocr and normalized in ordered_ocr)


def _expect_uses_recognized_kind(expect: str) -> bool:
    kind, sep, _ = expect.partition(":")
    return bool(sep) and kind.strip().casefold() in _EXPECT_KINDS


def _expected(snapshot: ScreenSnapshot, expect: str) -> bool | None:
    """True/False for a resolved check; None means "not checkable right now".

    ``focus:`` needs Accessibility, while ``text:`` needs either focused-window
    AX evidence or trustworthy focused-window bounds for OCR. An unavailable
    signal is never silently folded into False, which would be indistinguishable
    from a genuine miss.

    Only "app:"/"window:"/"text:"/"focus:" are recognized kind prefixes —
    treating ANY prefix before the first colon as a kind (the previous
    behavior) silently broke every URL- or time-shaped expect string:
    expect="https://youtube.com" read kind="https", needle="//youtube.com"
    (never matches); expect="3:45" read kind="3", needle="45". "text:" is an
    explicit synonym for the plain whole-screen search below (an established
    convention elsewhere in this tool's lineage); any other prefix, including
    a full prose sentence like "Search field focused" with no colon at all,
    falls back to that same literal whole-string search instead — which a
    sentence describing state (not a visible label) can never match. That
    fallback path is real-usage-confirmed (2026-08-26): "field focused" state
    checks need a real signal, not a literal-text guess — see focus: below.
    """
    kind, sep, value = expect.partition(":")
    kind_norm = kind.strip().casefold()
    if sep and kind_norm in _EXPECT_KINDS:
        needle = value.strip().casefold()
        if kind_norm == "focus":
            # ScreenElement.focused is only populated from Accessibility data
            # (OCR-only elements default it to None); when AX isn't available
            # for this capture, "no element measured as focused" would be
            # indistinguishable from "genuinely not focused" — report unknown
            # instead of a confident False the model can't tell apart from a
            # real miss.
            if snapshot.accessibility_status != "ok":
                return None
            if not needle:
                return any(e.focused for e in snapshot.elements)
            return any(e.focused and needle in e.text.casefold() for e in snapshot.elements)
        if not needle: return False
        if kind_norm == "app": return needle in snapshot.app.casefold()
        if kind_norm == "window": return needle in snapshot.window.casefold()
        return _expect_text_visible(snapshot, needle)
    needle = expect.strip().casefold()
    if not needle: return False
    return _expect_text_visible(snapshot, needle)


_EDITABLE_AX_ROLES = {"AXTextField", "AXTextArea", "AXSearchField", "AXComboBox"}


def _focused_editable(snapshot: ScreenSnapshot) -> ScreenElement | None:
    if snapshot.accessibility_status != "ok":
        return None
    return next(
        (
            e for e in snapshot.elements
            if e.source == "ax" and e.focused and e.role in _EDITABLE_AX_ROLES
        ),
        None,
    )


def _type_input_verification(before: ScreenSnapshot, after: ScreenSnapshot) -> str:
    """Verify typing without retaining/exposing field contents.

    ``value_digest`` is absent for AXSecureTextField by construction.  A digest
    change is strong evidence the focused editable's value changed; when AX can
    only prove focus continuity we report that weaker state explicitly.
    """
    before_edit = _focused_editable(before)
    after_edit = _focused_editable(after)
    if before_edit is None or after_edit is None:
        return "input_unverified"
    if before_edit.role != after_edit.role:
        return "input_unverified"
    distance_sq = (
        (before_edit.point[0] - after_edit.point[0]) ** 2
        + (before_edit.point[1] - after_edit.point[1]) ** 2
    )
    if distance_sq > 120.0 ** 2:
        return "input_unverified"
    if (
        before_edit.value_digest != after_edit.value_digest
        and (before_edit.value_digest or after_edit.value_digest)
    ):
        return "input_verified"
    return "input_unverified+focus_verified"


def _validate_web_url(url: str) -> None:
    parsed = urlsplit(url.strip())
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("open_url requires a valid http(s) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("open_url refuses URLs containing credentials")


def _computer_impl(action: str, target: str = "", text: str = "", coord: str = "", direction: str = "", amount: int = 3, near: str = "", element_id: str = "", observation_id: str = "", question: str = "", expect: str = "", modifiers: str = "", app: str = "") -> str:
    if not _ACTION_LOCK.acquire(blocking=False):
        return _error("another computer action is already running — retry after it finishes")
    try:
        action = action.strip().lower()
        _phase(f"🖥 ควบคุมหน้าจอ: {action}" + (f" ({target or text or element_id})"[:60] if (target or text or element_id) else ""))
        if action not in {"see", "inspect", "click", "double_click", "triple_click", "right_click", "type", "key", "scroll", "open_app", "open_url", "drag", "hover"}:
            return _error(f"unknown action: {action}")
        if action == "inspect":
            if not _observation_budget_available():
                return _error("computer observation limit reached — stop and report the current screen")
            _OBSERVATION_ATTEMPTS[0] += 1
            return _inspect(observation_id, element_id, question)
        if _backend.failsafe_abort():
            return _error("failsafe abort — user took the mouse")
        if action == "see":
            if not _observation_budget_available():
                return _error("computer observation limit reached — stop and report the current screen")
            _OBSERVATION_ATTEMPTS[0] += 1
            _LAST_SIGNATURE[0] = "see"
            snapshot = _publish(_capture_snapshot())
            _LAST_SCREEN_TEXT[0] = "\n".join(str(b.get("text", "")) for b in snapshot.ocr_boxes); _LAST_FRONTMOST_APP[0] = snapshot.app
            return _result("[ok] see", snapshot)
        if _ACTION_ATTEMPTS[0] >= _effective_mutation_max():
            return _error("computer mutation limit reached — use see/inspect only or report the current screen")
        before = _capture_snapshot()
        if action in _CONTEXT_DEPENDENT_ACTIONS and _LAST_FRONTMOST_APP[0] and before.app != _LAST_FRONTMOST_APP[0]:
            _publish(before)
            return _error(f"frontmost app changed since last action — latest is {before.observation_id}; inspect it before acting")
        # near/modifiers included (ported from MAX audit F2): without them, a
        # click that hit an ambiguity error recommending "retry with near="
        # was itself refused as a "repeated action" on retry (identical
        # action/target/text otherwise), and a plain click followed by the
        # SAME click again with modifiers= (e.g. cmd-click to extend a
        # selection) was wrongly treated as a no-op repeat of the first click.
        signature = "|".join((
            action, target.strip().casefold(), text.strip().casefold(), direction,
            str(amount), element_id, observation_id,
            near.strip().casefold(), modifiers.strip().casefold(),
            app.strip().casefold(),
        ))
        if signature == _LAST_SIGNATURE[0] and _LATEST_OBSERVATION[0] and before.fingerprint == _LATEST_OBSERVATION[0].fingerprint:
            Path(before.image_path).unlink(missing_ok=True); return _error("repeated action, screen unchanged — change approach")
        chosen: ScreenElement | None = None
        if element_id:
            latest = _LATEST_OBSERVATION[0]
            if latest is None or observation_id != latest.observation_id:
                Path(before.image_path).unlink(missing_ok=True); return _error("element_id requires the latest observation_id; call see")
            if before.fingerprint != latest.fingerprint or before.app != latest.app:
                _publish(before); return _error(f"screen changed since {observation_id}; latest is {before.observation_id}")
            chosen = latest.get(element_id)
            if chosen is None:
                Path(before.image_path).unlink(missing_ok=True); return _error(f"element not found: {element_id}")
        if action == "type" and (
            any(marker in text.casefold() for marker in _PASSWORD_MARKERS)
            or any(marker in _LAST_CLICK_TARGET[0].casefold() for marker in _PASSWORD_MARKERS)
            or any(e.focused and e.role == "AXSecureTextField" for e in before.elements)
        ):
            return _error("refusing to type into what looks like a password field — enter credentials manually")
        if action == "key" and _normalize_combo(text) in _FORBIDDEN_KEY_COMBOS:
            return _error(f"refusing dangerous hotkey: {text}")
        if _DESTRUCTIVE_GUARD[0] and action in {"click", "double_click", "triple_click", "right_click", "drag", "key"}:
            # `key` is checked against an exact, normalized hotkey set (a bare
            # Backspace/Delete press while editing text is ordinary, not the
            # same risk as a Delete/Trash BUTTON) — never a loose substring
            # match against the marker lists, which would false-positive on
            # every routine typo correction. click/double_click/right_click/drag
            # match target/element text by substring since that IS a visible
            # label — the marker list itself is tiered by supervision context
            # (see _DESTRUCTIVE_MARKERS_SUPERVISED/_UNSUPERVISED above): an
            # awake-fired turn always sets action_max, an ordinary turn's
            # default guard never does, so that existing distinction alone
            # picks the right tier without a new per-turn flag.
            _markers = _DESTRUCTIVE_MARKERS_UNSUPERVISED if _ACTION_MAX_OVERRIDE[0] is not None else _DESTRUCTIVE_MARKERS_SUPERVISED
            destructive_hit = (
                _normalize_combo(text) in _DESTRUCTIVE_KEY_COMBOS if action == "key" else
                any(
                    marker in str(c).casefold()
                    for c in (target, chosen.text if chosen is not None else None,
                              text if action == "drag" else None)
                    if c for marker in _markers
                )
            )
            if destructive_hit:
                # Terminate mutations, not observations: force the mutation ceiling
                # so no further click/type/key/drag/etc. can execute this turn. The
                # separate bounded see/inspect budget remains available for safe
                # diagnosis/reporting after the refusal. Applies uniformly whether
                # this is an unsupervised background turn (always blocked, no
                # exception) or an ordinary turn whose own request never said to
                # delete/remove anything (see graph.py's per-turn intent check) —
                # either way, one shot at it, not a free retry loop. A later turn
                # where the user explicitly confirms starts with a fresh
                # reset_computer_guards() remains destructive-safe when the
                # runtime has V2_DENY_FILE_DELETION enabled.
                _ACTION_ATTEMPTS[0] = _effective_mutation_max()
                return _error(
                    "refusing an action that looks delete/remove-related — file deletion is disabled "
                    "for this runtime, so it cannot be approved; computer access terminated for this turn"
                )
        _ACTION_ATTEMPTS[0] += 1
        _LAST_SIGNATURE[0] = signature
        _progress(f"กำลัง {action}: {(target or text or element_id)[:40]}…" if (target or text or element_id) else f"กำลัง {action}…")
        type_delivery = ""
        if action in {"click", "double_click", "triple_click", "right_click"}:
            if coord:
                return _error("raw coord is disabled; use element_id or visible target text")
            point, problem = (chosen.point, None) if chosen else _point_from_snapshot(before, target, near)
            if problem:
                return problem
            x, y = point
            try:
                _backend.click(
                    x, y,
                    button="right" if action == "right_click" else "left",
                    count=3 if action == "triple_click" else (2 if action == "double_click" else 1),
                    modifiers=modifiers,
                )
            except ValueError as exc:
                return _error(str(exc))
            _log_action(action, chosen.text if chosen else target, (x, y))
            _LAST_CLICK_TARGET[0] = chosen.text if chosen else target
        elif action == "hover":
            point, problem = (chosen.point, None) if chosen else _point_from_snapshot(before, target, near)
            if problem:
                return problem
            _backend.hover(*point)
            _log_action(action, chosen.text if chosen else target, point)
        elif action == "type":
            if not text:
                return _error("type requires text")
            type_delivery = _backend.type_text(text)
            _log_action(action)
        elif action == "key":
            _backend.key(text)
            _log_action(action, text)
        elif action == "scroll":
            point = None
            if target:
                point, problem = (chosen.point, None) if chosen else _point_from_snapshot(before, target, near)
                if problem:
                    return problem
            _backend.scroll(direction, amount, point=point)
            _log_action(action, direction)
        elif action == "drag":
            if not text:
                return _error("drag requires text as the drop-target's visible text")
            # near= disambiguates the SOURCE only (audit F8) — applying it to
            # the drop target too meant a near= chosen to pick out the source
            # could just as easily filter out the correct drop target, since
            # the two are rarely in the same screen region by definition.
            from_point, problem = (chosen.point, None) if chosen else _point_from_snapshot(before, target, near)
            if problem:
                return problem
            to_point, problem = _point_from_snapshot(before, text, "")
            if problem:
                return problem
            try:
                _backend.drag(from_point[0], from_point[1], to_point[0], to_point[1], modifiers=modifiers)
            except ValueError as exc:
                return _error(str(exc))
            _log_action(action, f"{target} -> {text}")
        elif action == "open_app":
            _backend.open_app(target)
            _log_action(action, target)
        else:
            _validate_web_url(target)
            _backend.open_url(target, app=app)
            _log_action(action, f"{app or 'default'}: {target}")
        # Settle before observing: live-reproduced (2026-07-17) a save-sheet's
        # slide-in animation not finished when the very next action (typing the
        # filename) fired, so it landed before the field was actually focused/
        # selected -- the default "Untitled" silently never got replaced, no
        # error anywhere. cmd+s-class actions are the common trigger but any
        # click/key can open a sheet/dialog, so this applies broadly rather
        # than special-casing specific hotkeys.
        _backend.settle()
        requested_app = target if action == "open_app" else app if action == "open_url" else ""
        after, ui_effect = _post_action_snapshot(
            before, expect=expect, requested_app=requested_app, action=action,
        )
        Path(before.image_path).unlink(missing_ok=True)
        _LAST_SCREEN_TEXT[0] = "\n".join(str(b.get("text", "")) for b in after.ocr_boxes); _LAST_FRONTMOST_APP[0] = after.app
        # Independent verification signals, combined rather than overwritten —
        # the previous version set a single `effect` string for each check in
        # turn, so supplying expect= silently discarded both whether the UI
        # actually changed AND the app_not_frontmost guard's own result. That
        # produced exactly the live-usage symptom this fixes: a `[warning]`
        # response whose own attached screenshot showed the action had, in
        # fact, succeeded — the model had no way to tell "verification heuristic
        # missed" apart from "nothing happened".
        notes: list[str] = []
        if action == "type":
            notes.append(_type_input_verification(before, after))
            if type_delivery == "clipboard_changed_externally":
                notes.append("clipboard_changed_externally")
        if requested_app.strip() and not _app_matches(requested_app, after.app):
            # `open -a` can return 0 without the target ever actually becoming
            # frontmost — reporting [ok] here would let the model believe
            # subsequent type/key calls land in the target app when they're
            # still landing wherever WAS frontmost (live-reproduced 2026-07-21
            # on MAX walk R7-CB, ported here — same design, same gap).
            notes.append("app_not_frontmost")
        expect_hint = ""
        if expect:
            verdict = _expected(after, expect)
            if verdict is None:
                # A real "can't tell" state: e.g. focus: without Accessibility,
                # or text: without focused-window AX evidence/bounds. Never fold
                # this into expectation_not_met, which would look like a confirmed miss.
                notes.append("expect_unknown")
            elif verdict:
                notes.append("verified")
            else:
                notes.append("expectation_not_met")
                if not _expect_uses_recognized_kind(expect):
                    # expect fell through to the literal whole-text search
                    # because it wasn't "app:"/"window:"/"text:"/"focus:" —
                    # a state description like "Search field focused" can
                    # never match that way. Real-usage-confirmed (2026-08-26):
                    # without this hint the model has no way to tell "the
                    # heuristic can't check this" apart from "genuinely
                    # didn't happen", and keeps retrying the same broken form.
                    expect_hint = (
                        '\nhint: expect="' + expect[:60] + '" was searched as literal on-screen '
                        'text and not found — for state checks use expect="focus:<label>" '
                        '(is it focused now), "app:<name>", or "window:<text>" instead.'
                    )
        verification_warning = bool(
            {"app_not_frontmost", "expectation_not_met", "expect_unknown", "clipboard_changed_externally"} & set(notes)
        ) or (
            action == "type"
            and "input_verified" not in notes
            and "verified" not in notes
        )
        if verification_warning:
            prefix = f"[warning] {action}"
        elif ui_effect == "changed" or "verified" in notes:
            prefix = f"[ok] {action}"
        else:
            prefix = f"[no_effect] {action}"
        effect = "+".join([ui_effect, *notes]) if notes else ui_effect
        return _result(prefix, after, effect) + expect_hint
    except Exception as exc:
        return _error(str(exc))
    finally:
        _ACTION_LOCK.release()
