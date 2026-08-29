"""Shared internal diagnostics for Endeavor Hands tool results.

The public MCP surface stays text-based.  This module gives those text results a
small machine-readable diagnostic suffix and lets the server extract the same
metadata for JSONL logging without changing the top-level tool schema.
"""
from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class Diagnostic:
    code: str
    layer: str
    retryable: bool
    hint: str = ""
    exit_code: int | None = None

    def render(self) -> str:
        fields = [
            f"code={self.code}",
            f"layer={self.layer}",
            f"retryable={'true' if self.retryable else 'false'}",
        ]
        if self.exit_code is not None:
            fields.append(f"exit_code={self.exit_code}")
        return "[diagnostic] " + " ".join(fields)


def append_diagnostic(text: str, diagnostic: Diagnostic | None) -> str:
    if diagnostic is None or "[diagnostic]" in text:
        return text
    suffix = diagnostic.render()
    if diagnostic.hint and "[hint]" not in text:
        suffix += f"\n[hint] {diagnostic.hint}"
    return f"{text.rstrip()}\n{suffix}"


def classify_process_failure(
    returncode: int,
    stderr: str,
    *,
    command: str = "",
    workspace: str = "",
) -> Diagnostic | None:
    """Classify only evidence-backed process failures.

    A nested ``sandbox-exec`` failure is intentionally checked before generic
    EPERM/EACCES markers.  The former means the *test harness* tried to apply a
    second macOS sandbox, not that the requested workspace operation violated
    Hands' production policy.
    """
    if returncode == 0:
        return None
    err = stderr or ""
    err_lower = err.casefold()

    if "sandbox-exec:" in err_lower and "sandbox_apply:" in err_lower and "operation not permitted" in err_lower:
        return Diagnostic(
            "sandbox_nested",
            "sandbox",
            False,
            "nested sandboxing is blocked by macOS; use the explicit test backend or run the one-layer verifier outside Hands",
            returncode,
        )
    if returncode == 127:
        return Diagnostic(
            "command_not_found",
            "process",
            False,
            "command not found — check spelling, or use an absolute path / `which <name>` first.",
            returncode,
        )
    if returncode == 126:
        return Diagnostic(
            "os_permission_denied",
            "process",
            False,
            "permission denied executing that file — check that it is executable and is not a directory.",
            returncode,
        )
    if "sandbox-exec:" in err_lower and any(
        marker in err_lower for marker in ("operation not permitted", "permission denied", "read-only file system")
    ):
        return Diagnostic(
            "sandbox_policy_denied",
            "sandbox",
            False,
            "sandbox-exec rejected this operation under the active production policy",
            returncode,
        )
    if "-10004" in err or ("osascript" in command and "System Events" in command):
        return Diagnostic(
            "sandbox_policy_denied",
            "sandbox",
            False,
            "GUI scripting via osascript is blocked by this tool's sandbox — use the `computer` tool instead.",
            returncode,
        )
    if "Permission denied" in err:
        return Diagnostic(
            "os_permission_denied",
            "process",
            False,
            "the OS/filesystem denied this operation; check file mode/ownership or use a capability with the required permission gate",
            returncode,
        )
    if any(marker in err for marker in ("Operation not permitted", "Read-only file system")):
        hint = "operation denied by the sandbox policy"
        if workspace:
            hint += f"; ordinary shell writes are limited to workspace/ ({workspace}) and /tmp"
        return Diagnostic("sandbox_policy_denied", "sandbox", False, hint, returncode)
    return Diagnostic("process_exit_nonzero", "process", False, exit_code=returncode)


def flatten_exception(exc: BaseException, *, limit: int = 4) -> str:
    """Return useful leaf causes from ExceptionGroup/TaskGroup-style failures."""
    leaves: list[BaseException] = []

    def visit(node: BaseException) -> None:
        children = getattr(node, "exceptions", None)
        if isinstance(children, (tuple, list)) and children:
            for child in children:
                if isinstance(child, BaseException):
                    visit(child)
            return
        leaves.append(node)

    visit(exc)
    if not leaves:
        leaves = [exc]
    parts: list[str] = []
    for leaf in leaves:
        text = str(leaf).strip()
        rendered = f"{type(leaf).__name__}: {text}" if text else type(leaf).__name__
        if rendered not in parts:
            parts.append(rendered)
        if len(parts) >= max(1, limit):
            break
    return " | ".join(parts)


def classify_exception(exc: BaseException, *, layer: str = "tool") -> Diagnostic:
    leaf_text = flatten_exception(exc)
    low = leaf_text.casefold()
    if "sandbox-exec:" in low and "sandbox_apply:" in low and "operation not permitted" in low:
        return Diagnostic("sandbox_nested", "sandbox", False)
    if isinstance(exc, FileNotFoundError) and "sandbox-exec" in low:
        return Diagnostic("sandbox_unavailable", "sandbox", False)
    if isinstance(exc, TimeoutError) or "timeouterror" in low or "timed out" in low or "timeout" in low:
        return Diagnostic("timeout", layer, True)
    if isinstance(exc, PermissionError) or "permissionerror:" in low:
        return Diagnostic("os_permission_denied", layer, False)
    if isinstance(exc, (ValueError, TypeError)) or "valueerror:" in low or "typeerror:" in low:
        return Diagnostic("tool_validation", layer, False)
    if layer == "mcp":
        return Diagnostic("mcp_child_error", "mcp", False)
    return Diagnostic("tool_internal_error", layer, False)


_DIAGNOSTIC_RE = re.compile(
    r"\[diagnostic\]\s+code=(?P<code>[\w.-]+)\s+layer=(?P<layer>[\w.-]+)\s+retryable=(?P<retryable>true|false)"
    r"(?:\s+exit_code=(?P<exit>-?\d+))?",
    re.I,
)
_ARTIFACT_RE = re.compile(r"Full output saved at:\s*(\S+)", re.I)
_JOB_RE = re.compile(r"(?:\bjob_id\s*[=:]\s*|\b(?:started\s+)?job\s+)([A-Za-z0-9_.-]+)", re.I)
_EXIT_RE = re.compile(r"(?:\[error\]\s+exited\s+|\bexit_code=)(-?\d+)\b", re.I)


def metadata_from_result(text: str) -> dict[str, object]:
    """Extract additive log metadata from an existing text result."""
    raw = str(text or "")
    low = raw.casefold()
    if "[error]" in low or "[permission_required]" in low:
        status = "error"
    elif "[warning]" in low or "[no_effect]" in low:
        status = "warning"
    else:
        status = "ok"

    meta: dict[str, object] = {"status": status}
    match = _DIAGNOSTIC_RE.search(raw)
    if match:
        meta["diagnostic_code"] = match.group("code")
        meta["diagnostic_layer"] = match.group("layer")
        meta["retryable"] = match.group("retryable").casefold() == "true"
        if match.group("exit") is not None:
            meta["exit_code"] = int(match.group("exit"))
    else:
        # Recognize existing guarded-tool vocabulary so observability improves
        # even before every tool has been migrated to render diagnostics itself.
        if "[permission_required]" in low:
            meta.update(diagnostic_code="permission_gate", diagnostic_layer="tool", retryable=False)
        elif "file deletion is disabled" in low or "delete/remove-related" in low:
            meta.update(diagnostic_code="deletion_guard", diagnostic_layer="tool", retryable=False)
        elif "sandbox-exec:" in low and "sandbox_apply:" in low and "operation not permitted" in low:
            meta.update(diagnostic_code="sandbox_nested", diagnostic_layer="sandbox", retryable=False)
        elif "expectation_not_met" in low:
            meta.update(diagnostic_code="tool_validation", diagnostic_layer="computer", retryable=True)

    artifact = _ARTIFACT_RE.search(raw)
    if artifact:
        meta["artifact_path"] = artifact.group(1).rstrip(".,;)")
    job = _JOB_RE.search(raw)
    if job:
        meta["job_id"] = job.group(1)
    if "exit_code" not in meta:
        exit_match = _EXIT_RE.search(raw)
        if exit_match:
            meta["exit_code"] = int(exit_match.group(1))
    return meta


_SENSITIVE_NAME_RE = re.compile(r"(?:password|passwd|secret|token|api[_-]?key|authorization|credential|grant_phrase|headers?_json)", re.I)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_JSON_SECRET_RE = re.compile(
    r'(?i)(["\']?(?:authorization|api[_-]?key|token|secret|password)["\']?\s*[:=]\s*["\']?)([^"\'\s,}]+)'
)
_ENV_SECRET_RE = re.compile(r"(?i)\b([A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|API_KEY|APIKEY))=([^\s]+)")


def redact_text(value: str) -> str:
    value = _BEARER_RE.sub("Bearer <redacted>", value)
    value = _JSON_SECRET_RE.sub(lambda m: m.group(1) + "<redacted>", value)
    value = _ENV_SECRET_RE.sub(lambda m: m.group(1) + "=<redacted>", value)
    return value


def redact_args(args: dict) -> dict:
    """Best-effort log redaction; tool execution still receives original args."""
    safe: dict = {}
    for key, value in args.items():
        if _SENSITIVE_NAME_RE.search(str(key)):
            safe[key] = "<redacted>"
        elif isinstance(value, str):
            safe[key] = redact_text(value)
        else:
            safe[key] = value
    return safe
