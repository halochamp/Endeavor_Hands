"""_edit_grants.py — session-scoped, per-folder consent gate for tools that
can overwrite an EXISTING file's content: edit() (always) and write_file()
(only when overwrite=true AND the target already exists — creating a brand
new file never needs a grant). bash/python_exec were explicitly scoped OUT of
this gate by the user on 2026-08-26 despite being able to write files via
shell redirection — a known, accepted gap, not an oversight. The two entry
points share ONE registry: granting a folder through either tool covers both,
since the thing being consented to is "existing files in this folder may be
modified," not a specific tool name. Default is OFF: no folder may be touched
until granted, once per server process lifetime (this IS "the session" —
there is no other process boundary an MCP stdio server can observe).

Trust model — read before changing anything here: there is no OS-level
dialog and no local file the human edits directly; the only channel back to
the actual computer owner is the same chat ChatGPT is already having with
them. So the gate is a two-call protocol: the first qualifying call against a
not-yet-granted folder always fails and returns a fresh, single-use nonce,
with an explicit instruction that the model must ask the user directly and
only pass the nonce back once they've said yes THIS turn — never
speculatively. This does not cryptographically prove a human approved it; it
forces one extra explicit round-trip per folder and leaves an audit trail
(see logs/agent_activity.jsonl via the existing @_logged wrapper on both).
That is the strongest guarantee achievable without a second UI surface —
know this ceiling before relying on it for anything higher-stakes.
"""
from __future__ import annotations
import os
import secrets

_GRANTED: set[str] = set()
_PENDING: dict[str, str] = {}


def _folder_unit(effective_path: str) -> str:
    """Approval granularity: the direct child of WORKSPACE the path falls
    under (matches how a person names "folder X on the Desktop"), or the
    file's own containing directory when the path is outside WORKSPACE
    entirely (no natural top-level anchor to walk up to there)."""
    from config import WORKSPACE
    abs_path = os.path.realpath(effective_path)
    ws_abs = os.path.realpath(WORKSPACE)
    if abs_path == ws_abs:
        return ws_abs
    if abs_path.startswith(ws_abs + os.sep):
        rest = abs_path[len(ws_abs) + 1:]
        first = rest.split(os.sep, 1)[0]
        return os.path.join(ws_abs, first)
    return os.path.dirname(abs_path)


def check_grant(effective_path: str, grant_phrase: str) -> str | None:
    """Returns an error string (and registers/validates grant_phrase) if the
    covering folder isn't granted yet; None once it's safe to edit."""
    unit = _folder_unit(effective_path)
    if unit in _GRANTED:
        return None
    expected = _PENDING.get(unit)
    if expected and grant_phrase.strip() == expected:
        _GRANTED.add(unit)
        del _PENDING[unit]
        return None
    # The nonce must stay stable across repeated failed attempts (a wrong
    # guess, an unrelated call on a sibling file, a mistyped retry) — rotating
    # it on every failure would invalidate the very nonce just handed out in
    # the previous response before it could ever be echoed back correctly.
    # Only actually granting (above) retires a nonce.
    if unit not in _PENDING:
        _PENDING[unit] = secrets.token_hex(4)
    nonce = _PENDING[unit]
    return (
        f'[permission_required] edit needs the user\'s explicit permission before this or any '
        f'other file under \'{unit}\' can be edited this session. Ask the user directly, in this '
        f'conversation, whether they allow editing files in that folder — do not assume yes and '
        f'do not edit anything else first. Only if they say yes, call edit(...) again on the same '
        f'file with grant_phrase="{nonce}" added. Never pass a grant_phrase the user has not '
        f'actually approved this turn; a guessed or reused phrase will not match and the request '
        f'will be asked again with a new one.'
    )
