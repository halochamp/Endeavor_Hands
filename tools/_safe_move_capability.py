"""Narrow capability for non-destructive mv/rename through the bash tool.

The normal Hands sandbox denies file-write-unlink across the workspace. macOS
Seatbelt uses that operation for both deletion and rename, so a plain mv is
blocked too. This classifier recognizes only a standalone /bin/mv invocation,
requires every source/destination to stay inside the workspace, and refuses
clobbering an existing destination. The caller may then grant unlink only for
the exact source paths while keeping the workspace-wide deletion deny in place.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
import shlex


@dataclass(frozen=True)
class SafeMoveCapability:
    argv: tuple[str, ...]
    source_paths: tuple[str, ...]


def _lexical_path(raw: str, workspace: str) -> str:
    expanded = os.path.expanduser(raw)
    if not os.path.isabs(expanded):
        expanded = os.path.join(workspace, expanded)
    return os.path.normpath(os.path.abspath(expanded))


def _inside(path: str, root: str) -> bool:
    try:
        return os.path.commonpath((path, root)) == root
    except ValueError:
        return False


def _parent_stays_inside(path: str, workspace_real: str) -> bool:
    parent_real = os.path.realpath(os.path.dirname(path) or ".")
    return _inside(parent_real, workspace_real)


def trusted_safe_move_invocation(command: str, workspace: str) -> SafeMoveCapability | None:
    """Return a bounded no-clobber mv capability, else None.

    Accepted shape: mv [-n] [-v] [--] SOURCE... DEST
    Shell composition/redirection, force/interactive flags, outside-workspace
    paths, missing sources, and any move that would replace an existing target
    are rejected.
    """
    if not command or not workspace or "\n" in command or "\r" in command:
        return None

    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()<>")
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return None

    if not tokens or any(tok in {";", "&&", "&", "|", "||", "(", ")", "<", ">"} for tok in tokens):
        return None

    if tokens[0] not in {"mv", "/bin/mv"}:
        return None

    operands: list[str] = []
    verbose = False
    end_options = False
    for token in tokens[1:]:
        if not end_options and token == "--":
            end_options = True
            continue
        if not end_options and token.startswith("-") and token != "-":
            if token == "-n":
                continue
            if token == "-v":
                verbose = True
                continue
            return None
        operands.append(token)

    if len(operands) < 2:
        return None

    workspace_lex = os.path.normpath(os.path.abspath(os.path.expanduser(workspace)))
    workspace_real = os.path.realpath(workspace_lex)
    sources = tuple(_lexical_path(value, workspace_lex) for value in operands[:-1])
    dest = _lexical_path(operands[-1], workspace_lex)

    if not all(_inside(path, workspace_lex) and _parent_stays_inside(path, workspace_real) for path in sources):
        return None
    if not _inside(dest, workspace_lex) or not _parent_stays_inside(dest, workspace_real):
        return None
    if any(not os.path.lexists(path) for path in sources):
        return None
    if len(set(sources)) != len(sources):
        return None

    dest_is_dir = os.path.isdir(dest)
    if len(sources) > 1 and not dest_is_dir:
        return None

    targets: list[str] = []
    for source in sources:
        target = os.path.join(dest, os.path.basename(source)) if dest_is_dir else dest
        target = os.path.normpath(target)
        if not _inside(target, workspace_lex) or not _parent_stays_inside(target, workspace_real):
            return None
        if os.path.lexists(target):
            return None
        if os.path.isdir(source) and _inside(target, source):
            return None
        targets.append(target)

    if len(set(targets)) != len(targets):
        return None

    argv = ["/bin/mv", "-n"]
    if verbose:
        argv.append("-v")
    argv.extend(sources)
    argv.append(dest)
    return SafeMoveCapability(tuple(argv), sources)
