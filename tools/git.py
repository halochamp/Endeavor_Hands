from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Iterable

from tools.bash import _build_sandbox_profile
from tools._truncate import truncate_with_save

_GIT_BIN = os.path.realpath(shutil.which("git") or "/usr/bin/git")
_SANDBOX_EXEC = shutil.which("sandbox-exec") or "/usr/bin/sandbox-exec"
_SSH_BIN = os.path.realpath(shutil.which("ssh") or "/usr/bin/ssh")
_GIT_EXEC_PATH = subprocess.run(
    [_GIT_BIN, "--exec-path"], capture_output=True, text=True, check=False
).stdout.strip()
_GIT_REMOTE_HTTPS = os.path.realpath(os.path.join(_GIT_EXEC_PATH, "git-remote-https")) if _GIT_EXEC_PATH else ""
_GIT_CREDENTIAL_OSXKEYCHAIN = (
    os.path.realpath(os.path.join(_GIT_EXEC_PATH, "git-credential-osxkeychain")) if _GIT_EXEC_PATH else ""
)
_ALLOWED_ACTIONS = {"status", "diff", "add", "commit", "push"}
_SAFE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
_SAFE_SCP_REMOTE = re.compile(r"^[A-Za-z0-9._-]+@[A-Za-z0-9._-]+:[^\s]+$")
_MAX_COMMIT_MESSAGE_CHARS = 4000
_STALE_LOCK_MIN_AGE_SECONDS = 5.0


def _inside(path: str, root: str) -> bool:
    try:
        return os.path.commonpath((os.path.realpath(path), os.path.realpath(root))) == os.path.realpath(root)
    except ValueError:
        return False


def _plain_git(repo: str, *args: str) -> subprocess.CompletedProcess[str]:
    """Run a fixed, read-only Git probe without a shell."""
    return subprocess.run(
        [_GIT_BIN, "-C", repo, *args],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=15,
    )


def _resolve_repo(repo: str, workspace: str) -> tuple[str, str]:
    workspace = os.path.realpath(workspace)
    requested = os.path.expanduser(repo or ".")
    if not os.path.isabs(requested):
        requested = os.path.join(workspace, requested)
    requested = os.path.realpath(requested)

    if not _inside(requested, workspace):
        raise ValueError("repository must be inside the approved workspace")
    if not os.path.isdir(requested):
        raise ValueError("repository path does not exist or is not a directory")

    root_probe = _plain_git(requested, "rev-parse", "--show-toplevel")
    if root_probe.returncode != 0:
        raise ValueError("path is not inside a Git work tree")
    root = os.path.realpath(root_probe.stdout.strip())
    if not _inside(root, workspace):
        raise ValueError("Git work tree resolves outside the approved workspace")

    git_dir_probe = _plain_git(root, "rev-parse", "--absolute-git-dir")
    if git_dir_probe.returncode != 0:
        raise ValueError("could not resolve repository Git metadata directory")
    git_dir = os.path.realpath(git_dir_probe.stdout.strip())
    if not _inside(git_dir, workspace):
        raise ValueError("Git metadata directory resolves outside the approved workspace")

    return root, git_dir


def _normalize_paths(paths: Iterable[str] | None, repo_root: str) -> list[str]:
    normalized: list[str] = []
    for raw in paths or ():
        value = str(raw).strip()
        if not value or value == ".":
            raise ValueError("Git paths must be explicit files/directories; '.' is not accepted")
        absolute = os.path.realpath(value if os.path.isabs(value) else os.path.join(repo_root, value))
        if not _inside(absolute, repo_root):
            raise ValueError(f"Git path escapes the repository: {value}")
        rel = os.path.relpath(absolute, repo_root)
        if rel == ".git" or rel.startswith(f".git{os.sep}"):
            raise ValueError("Git metadata cannot be staged as a source path")
        normalized.append(rel)
    return normalized


def _remote_transport(value: str) -> str | None:
    value = value.strip()
    if value.startswith("https://"):
        return "https"
    if value.startswith("ssh://") or _SAFE_SCP_REMOTE.fullmatch(value):
        return "ssh"
    return None


def _validate_ref(value: str, label: str) -> str:
    value = value.strip()
    if (
        not value
        or not _SAFE_REF.fullmatch(value)
        or value.startswith("-")
        or ".." in value
        or "@{" in value
        or "//" in value
    ):
        raise ValueError(f"invalid {label}")
    return value


def _lock_has_writer(lock_path: str) -> bool | None:
    """Return True for a write/update holder, False for no writer, None if unknown."""
    lsof = shutil.which("lsof") or "/usr/sbin/lsof"
    if not os.path.exists(lsof):
        return None
    try:
        result = subprocess.run(
            [lsof, "-F", "pca", "--", lock_path],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None

    if result.returncode not in (0, 1):
        return None
    if result.returncode == 1 or not result.stdout.strip():
        return False

    # lsof -F emits `a<mode>` for descriptor access. A read-only observer such as
    # an IDE/indexer must not permanently block recovery; write/update holders do.
    access_modes = [line[1:].strip().lower() for line in result.stdout.splitlines() if line.startswith("a")]
    if not access_modes:
        return None
    return any(mode in {"w", "u"} for mode in access_modes)


def _recover_zero_byte_stale_index_lock(git_dir: str) -> str | None:
    """Move a demonstrably stale zero-byte index.lock aside; never delete it."""
    lock = os.path.join(git_dir, "index.lock")
    if not os.path.exists(lock):
        return None

    try:
        stat_result = os.stat(lock)
    except OSError as exc:
        raise RuntimeError(f"cannot inspect existing Git index lock: {exc}") from exc

    if stat_result.st_size != 0:
        raise RuntimeError("Git index.lock exists and is non-empty; refusing automatic recovery")
    if time.time() - stat_result.st_mtime < _STALE_LOCK_MIN_AGE_SECONDS:
        raise RuntimeError("Git index.lock is too recent to treat as stale")

    writer = _lock_has_writer(lock)
    if writer is None:
        raise RuntimeError("Git index.lock exists and writer ownership could not be verified")
    if writer:
        raise RuntimeError("Git index.lock is actively held for writing; refusing recovery")

    stamp = time.strftime("%Y%m%dT%H%M%S")
    backup = os.path.join(git_dir, f"index.lock.stale-{stamp}")
    suffix = 1
    while os.path.exists(backup):
        backup = os.path.join(git_dir, f"index.lock.stale-{stamp}-{suffix}")
        suffix += 1
    os.replace(lock, backup)
    return backup


def _git_profile(
    workspace: str,
    git_dir: str,
    *,
    mutate: bool,
    push_transport: str | None = None,
) -> str:
    extra_reads: list[str] = []
    allowed_execs = [_GIT_BIN]
    if push_transport:
        home = os.path.expanduser("~")
        git_config_dir = os.path.join(home, ".config", "git")
        if os.path.exists(git_config_dir):
            extra_reads.append(git_config_dir)
        if push_transport == "ssh":
            ssh_dir = os.path.join(home, ".ssh")
            if os.path.exists(ssh_dir):
                extra_reads.append(ssh_dir)
            if os.path.exists(_SSH_BIN):
                allowed_execs.append(_SSH_BIN)
        elif push_transport == "https":
            if _GIT_REMOTE_HTTPS and os.path.exists(_GIT_REMOTE_HTTPS):
                allowed_execs.append(_GIT_REMOTE_HTTPS)
            if _GIT_CREDENTIAL_OSXKEYCHAIN and os.path.exists(_GIT_CREDENTIAL_OSXKEYCHAIN):
                allowed_execs.append(_GIT_CREDENTIAL_OSXKEYCHAIN)

    profile = _build_sandbox_profile(
        workspace,
        extra_read_paths=tuple(extra_reads),
        extra_unlink_paths=(git_dir,) if mutate else (),
    )
    exec_rules = " ".join(f'(literal "{path}")' for path in dict.fromkeys(allowed_execs))
    return profile + f"\n; Guarded Git may not execute repository-controlled helpers/shell commands.\n(deny process-exec)\n(allow process-exec {exec_rules})\n"


def _run_guarded_git(
    workspace: str,
    repo_root: str,
    git_dir: str,
    args: list[str],
    *,
    timeout: int,
    mutate: bool = False,
    push_transport: str | None = None,
) -> subprocess.CompletedProcess[str]:
    profile_path: str | None = None
    try:
        profile = _git_profile(workspace, git_dir, mutate=mutate, push_transport=push_transport)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".sb", delete=False) as handle:
            handle.write(profile)
            profile_path = handle.name

        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        if not mutate:
            env["GIT_OPTIONAL_LOCKS"] = "0"
        result = subprocess.run(
            [_SANDBOX_EXEC, "-f", profile_path, _GIT_BIN, "-C", repo_root, *args],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=timeout,
            env=env,
        )
        return result
    finally:
        if profile_path:
            try:
                os.unlink(profile_path)
            except OSError:
                pass


def _format_result(result: subprocess.CompletedProcess[str], workspace: str) -> str:
    output = result.stdout or ""
    if result.stderr:
        output += f"\n[stderr]\n{result.stderr}"
    output = output.strip() or "(no output)"
    if result.returncode != 0:
        output = f"[error] git exited {result.returncode}\n{output}"
    return truncate_with_save(output, 10_000, workspace, "git", marker_first=True, keep_tail=True)


def _git_impl(
    action: str,
    repo: str = ".",
    paths: list[str] | None = None,
    message: str = "",
    remote: str = "origin",
    branch: str = "",
    staged: bool = False,
    timeout: int = 60,
) -> str:
    """Guarded Git operations with repository-scoped metadata mutation."""
    from config import WORKSPACE

    action = (action or "").strip().lower()
    if action not in _ALLOWED_ACTIONS:
        return f"[error] unsupported git action: {action or '<empty>'}; allowed: {', '.join(sorted(_ALLOWED_ACTIONS))}"
    if timeout < 1 or timeout > 300:
        return "[error] timeout must be between 1 and 300 seconds"

    try:
        repo_root, git_dir = _resolve_repo(repo, WORKSPACE)
        normalized_paths = _normalize_paths(paths, repo_root)

        if action == "status":
            if normalized_paths or message or branch:
                return "[error] status does not accept paths, message, or branch"
            result = _run_guarded_git(
                WORKSPACE,
                repo_root,
                git_dir,
                ["-c", "core.fsmonitor=false", "status", "--short", "--branch"],
                timeout=timeout,
            )
            return _format_result(result, WORKSPACE)

        if action == "diff":
            args = ["-c", "core.fsmonitor=false", "diff", "--no-ext-diff", "--no-textconv", "--ignore-submodules=all"]
            if staged:
                args.append("--cached")
            if normalized_paths:
                args.extend(["--", *normalized_paths])
            result = _run_guarded_git(WORKSPACE, repo_root, git_dir, args, timeout=timeout)
            return _format_result(result, WORKSPACE)

        if action == "add":
            if not normalized_paths:
                return "[error] add requires explicit paths; staging the whole repository implicitly is not allowed"
            recovered = _recover_zero_byte_stale_index_lock(git_dir)
            result = _run_guarded_git(
                WORKSPACE,
                repo_root,
                git_dir,
                ["-c", "core.hooksPath=/dev/null", "add", "--", *normalized_paths],
                timeout=timeout,
                mutate=True,
            )
            output = _format_result(result, WORKSPACE)
            if recovered:
                output = f"[git] moved stale index lock to {recovered}\n{output}"
            return output

        if action == "commit":
            commit_message = message.strip()
            if not commit_message:
                return "[error] commit requires a non-empty message"
            if len(commit_message) > _MAX_COMMIT_MESSAGE_CHARS:
                return f"[error] commit message exceeds {_MAX_COMMIT_MESSAGE_CHARS} characters"
            if normalized_paths:
                return "[error] commit does not accept paths; stage explicit paths with action=add first"
            recovered = _recover_zero_byte_stale_index_lock(git_dir)
            result = _run_guarded_git(
                WORKSPACE,
                repo_root,
                git_dir,
                ["-c", "core.hooksPath=/dev/null", "-c", "commit.gpgSign=false", "commit", "-m", commit_message],
                timeout=timeout,
                mutate=True,
            )
            output = _format_result(result, WORKSPACE)
            if recovered:
                output = f"[git] moved stale index lock to {recovered}\n{output}"
            return output

        # push: existing configured remote only, current branch by default, no force/refspec flags.
        remote_name = _validate_ref(remote or "origin", "remote")
        remote_probe = _plain_git(repo_root, "remote", "get-url", "--push", remote_name)
        if remote_probe.returncode != 0:
            return f"[error] configured remote not found: {remote_name}"
        remote_url = remote_probe.stdout.strip()
        transport = _remote_transport(remote_url)
        if transport is None:
            return "[error] push remote must use SSH or HTTPS; local/ext/custom transports are not allowed"

        branch_name = branch.strip()
        if not branch_name:
            branch_probe = _plain_git(repo_root, "symbolic-ref", "--quiet", "--short", "HEAD")
            if branch_probe.returncode != 0 or not branch_probe.stdout.strip():
                return "[error] cannot push from detached HEAD without an explicit branch"
            branch_name = branch_probe.stdout.strip()
        branch_name = _validate_ref(branch_name, "branch")

        result = _run_guarded_git(
            WORKSPACE,
            repo_root,
            git_dir,
            ["-c", "core.hooksPath=/dev/null", "push", remote_name, branch_name],
            timeout=timeout,
            mutate=True,
            push_transport=transport,
        )
        output = _format_result(result, WORKSPACE)
        if result.returncode == 0:
            output = f"[git] pushed {branch_name} to {remote_name}\n{output}"
        return output

    except subprocess.TimeoutExpired:
        return f"[error] git command timed out after {timeout}s"
    except (OSError, RuntimeError, ValueError) as exc:
        return f"[error] {exc}"
