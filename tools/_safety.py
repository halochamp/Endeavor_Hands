"""_safety.py — path guards (V2)

write (2026-07-21 create-only-outside policy):
  ใน WORKSPACE   → เขียน/แก้ in-place ได้เต็มที่
  นอก WORKSPACE  → สร้างไฟล์ใหม่ได้ทุกที่ที่ไม่ protected แต่ห้ามแตะไฟล์เดิม —
                   edit/overwrite ถูก redirect ไป working copy ข้างต้นฉบับ
                   (name.edited.ext) ผ่าน plan_write(); เฉพาะไฟล์ *.edited.*
                   เท่านั้นที่แก้ in-place นอก workspace ได้
  env V2_ALLOW_OUTSIDE → bypass เป็นพฤติกรรมเก่า (in-place ได้ทุกที่ที่ไม่ protected)
read:  ทุกที่ ยกเว้น system paths
"""
from __future__ import annotations
import os

_PROTECTED_PATHS = [
    "/etc/", "/usr/", "/bin/", "/sbin/", "/lib/",
    "/System/", "/Library/", "/Applications/",
    os.path.expanduser("~/.ssh/"),
    os.path.expanduser("~/.aws/"),
    os.path.expanduser("~/.gnupg/"),
    os.path.expanduser("~/.claude/"),
    # 2026-07-20: blanket ~/Library/ used to block EVERYTHING under it,
    # including ~/Library/CloudStorage/ — where macOS (Monterey+) actually
    # mounts Google Drive/OneDrive/iCloud Drive, i.e. the user's own real
    # files, not app internals. Live-caught via a real blocked read_file
    # call on a Google Drive PDF. Replaced with a targeted list of the
    # credential-bearing subpaths under ~/Library/ instead of the whole
    # tree, so cloud-drive files (and other ordinary per-app data like
    # Mail/Safari/Preferences/Caches) are readable while secrets stay
    # blocked:
    os.path.expanduser("~/Library/Keychains/"),          # Keychain databases
    os.path.expanduser("~/Library/Application Support/"),  # most apps' saved
        # credentials/tokens live here (browser "Login Data", password
        # managers, cloud-CLI credential caches, crypto wallets, etc.) —
        # the single highest-value entry beyond Keychains itself
    os.path.expanduser("~/Library/Containers/"),          # sandboxed per-app
    os.path.expanduser("~/Library/Group Containers/"),    # data, incl. many
        # third-party password managers/VPN clients macOS forces in here
        # instead of Application Support
    os.path.expanduser("~/Library/Cookies/"),
    os.path.expanduser("~/Library/HTTPStorages/"),         # web session tokens
    os.path.expanduser("~/Library/Messages/"),             # iMessage chat.db —
        # can contain SMS/iMessage-delivered 2FA/OTP codes
    # WebSocket auth token — parent of workspace, reachable via "../.agent_token"
    os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".agent_token")),
]


def _strip_ws_prefix(path: str, workspace: str) -> str:
    ws_name = os.path.basename(workspace.rstrip("/\\"))
    norm = path.replace("\\", "/")
    if norm.startswith(ws_name + "/"):
        return norm[len(ws_name) + 1:]
    return path


def _protected_hit(abs_path: str) -> str | None:
    """abs_path ต้องผ่าน realpath มาแล้ว — คืนชื่อ protected path ที่โดน, None ถ้าไม่โดน"""
    for protected in _PROTECTED_PATHS:
        real_protected = os.path.realpath(protected)
        if abs_path == real_protected or abs_path.startswith(real_protected + os.sep):
            return protected
    return None


_EDITED_MARK = ".edited"


def edited_copy_path(path: str) -> str:
    """คู่ working copy ของไฟล์: report.md → report.edited.md (ไม่มีนามสกุล → Makefile.edited)"""
    root, ext = os.path.splitext(path)
    return root + _EDITED_MARK + ext


def is_edited_copy(path: str) -> bool:
    root, _ = os.path.splitext(os.path.basename(path))
    return root.endswith(_EDITED_MARK)


def _in_workspace(abs_path: str) -> bool:
    from config import WORKSPACE
    ws_abs = os.path.realpath(WORKSPACE)
    return abs_path == ws_abs or abs_path.startswith(ws_abs + os.sep)


def check_path(path: str) -> str | None:
    """คืน error string ถ้าเขียน IN-PLACE ที่ path นี้ไม่ได้, None ถ้าเขียนได้
    นโยบายนอก workspace = create-only: ไฟล์ใหม่/working copy (*.edited.*) ผ่าน,
    ไฟล์เดิมที่มีอยู่โดน block (ผู้เรียกควรใช้ plan_write() เพื่อ redirect ไป copy แทน)
    validate RESOLVED path (relative → WORKSPACE) ให้สอดคล้องกับ resolve_path()
    realpath ทั้งสองฝั่ง — กัน symlink ใน workspace ชี้ออกนอก และ /etc→/private/etc บน macOS"""
    abs_path = os.path.realpath(resolve_path(path))
    hit = _protected_hit(abs_path)
    if hit:
        return f"[BLOCKED] protected path: {hit}"
    if os.getenv("V2_ALLOW_OUTSIDE") or _in_workspace(abs_path):
        return None
    if not os.path.exists(abs_path) or is_edited_copy(abs_path):
        return None
    return (
        "[BLOCKED] in-place write to an existing file outside workspace. "
        f"Outside the workspace only NEW files may be created; changes to '{abs_path}' "
        f"must go to a sibling working copy: {edited_copy_path(abs_path)}"
    )


def plan_write(path: str) -> tuple[str, str | None, str | None]:
    """วางแผน write หนึ่งครั้ง — คืน (effective_path, err, note)
    err ≠ None    → ห้ามเขียนทุกรูปแบบ (protected path)
    note ≠ None   → target เป็นไฟล์เดิมนอก workspace: effective_path ถูก redirect
                    ไป working copy (name.edited.ext) ข้างต้นฉบับ — ต้นฉบับไม่ถูกแตะ
    ปกติ          → effective_path = resolved path เดิม, note=None"""
    resolved = resolve_path(path)
    abs_path = os.path.realpath(resolved)
    hit = _protected_hit(abs_path)
    if hit:
        return resolved, f"[BLOCKED] protected path: {hit}", None
    if os.getenv("V2_ALLOW_OUTSIDE") or _in_workspace(abs_path):
        return resolved, None, None
    if not os.path.exists(abs_path) or is_edited_copy(abs_path):
        return resolved, None, None
    copy = edited_copy_path(resolved)
    hit = _protected_hit(os.path.realpath(copy))
    if hit:
        return copy, f"[BLOCKED] protected path: {hit}", None
    note = (
        "original file outside workspace is never modified in place — "
        f"changes were written to the working copy: {copy}"
    )
    return copy, None, note


def resolve_path(path: str) -> str:
    """Resolve WRITE path: absolute → as-is (check_path guards), relative → WORKSPACE/path"""
    from config import WORKSPACE
    p = os.path.expanduser(path)
    if os.path.isabs(p):
        return p
    return os.path.join(WORKSPACE, _strip_ws_prefix(p, WORKSPACE))


def resolve_read_path(path: str) -> str:
    """Resolve READ path: reads unrestricted except system paths; relative → WORKSPACE/path
    realpath + _protected_hit on BOTH branches — relative `../` traversal (e.g. ../../etc/passwd)
    must hit the same protected-path guard as an absolute /etc/passwd."""
    from config import WORKSPACE
    p = os.path.expanduser(path)
    if not os.path.isabs(p):
        p = os.path.join(WORKSPACE, _strip_ws_prefix(p, WORKSPACE))
    hit = _protected_hit(os.path.realpath(p))
    if hit:
        raise PermissionError(f"[BLOCKED] protected path: {hit}")
    return p


def find_readable(path: str) -> str | None:
    """Like resolve_read_path, but also self-heals the one known-shape mistake
    that keeps recurring live (2026-07-19): `bash`'s cwd is already WORKSPACE,
    but a command sometimes re-prefixes the workspace dirname onto its own
    relative output path anyway (`cp src workspace/x.png`) — landing the file
    one level too deep (WORKSPACE/workspace/x.png) instead of where a plain
    lookup expects it (WORKSPACE/x.png). A docstring fix alone didn't
    generalize across command shapes (covered `screencapture x.png`, not
    `cp src workspace/x.png`), so this is the code-level fallback: if the
    plain resolution isn't a real file, also try one workspace-dirname level
    deeper before giving up. Returns the real path if EITHER resolves to an
    existing file, else None (never raises for a plain not-found — a
    PermissionError from a protected path still propagates, since that's a
    policy violation, not a location guess)."""
    from config import WORKSPACE
    primary = resolve_read_path(path)
    if os.path.isfile(primary):
        return primary
    if not os.path.isabs(os.path.expanduser(path)):
        ws_name = os.path.basename(WORKSPACE.rstrip("/\\"))
        fallback = os.path.join(WORKSPACE, ws_name, _strip_ws_prefix(path, WORKSPACE))
        if os.path.isfile(fallback):
            # audit F6: unlike the primary branch above (resolve_read_path,
            # which realpath+_protected_hit-checks everything), this fallback
            # guess was only isfile-checked — a symlinked "workspace" dir
            # INSIDE workspace pointing elsewhere would let it silently
            # confirm an outside file as "found" here. This self-heal only
            # ever guesses a WORKSPACE-relative doubled-prefix location (the
            # `cp src workspace/x.png` cwd-mistake shape) regardless of
            # whether the caller (send_file, telegram_bot's link interceptor)
            # otherwise allows arbitrary outside-workspace paths, so
            # re-validate the realpath the same way resolve_read_path does
            # before trusting it.
            real_fallback = os.path.realpath(fallback)
            hit = _protected_hit(real_fallback)
            if hit:
                raise PermissionError(f"[BLOCKED] protected path: {hit}")
            ws_abs = os.path.realpath(WORKSPACE)
            if real_fallback == ws_abs or real_fallback.startswith(ws_abs + os.sep):
                return fallback
    return None
