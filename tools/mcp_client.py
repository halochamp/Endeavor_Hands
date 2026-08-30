"""mcp_client.py — generic MCP client over Streamable HTTP or local stdio.

Two registries are merged by name: config.MCP_SERVERS (developer-provisioned)
and a dynamic registry under Endeavor Hands' project-local work/ directory.
Dynamic entries win on name collisions. HTTP servers use the official MCP SDK's
streamable HTTP client. Local stdio servers are spawned without a shell and run
under the same guarded sandbox profile as Hands' shell tools.
"""
from __future__ import annotations

import asyncio
import fcntl
import json
import os
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Callable, Coroutine, TypeVar

from langchain_core.tools import tool

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import MCP_MAX_CHARS, MCP_SERVERS, MCP_TIMEOUT, WORKSPACE
from tools._sandbox import RealSandboxBackend, build_sandbox_profile
from tools._diagnostics import Diagnostic, append_diagnostic, classify_exception, flatten_exception

_T = TypeVar("_T")
_SANDBOX_BACKEND = RealSandboxBackend()
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_WORK_DIR = _PROJECT_ROOT / "work"


def _inside(path: str, root: str) -> bool:
    try:
        return os.path.commonpath((os.path.realpath(path), os.path.realpath(root))) == os.path.realpath(root)
    except ValueError:
        return False


def _dynamic_path() -> Path:
    return _WORK_DIR / "tool_mcp" / "servers.json"


def _legacy_dynamic_path() -> Path:
    return Path(WORKSPACE) / "tool_mcp" / "servers.json"


def _load_dynamic_servers(*, strict: bool = False) -> dict:
    """Read the project-local registry, falling back to the former workspace path.

    Once any mutation is saved, work/tool_mcp/servers.json becomes canonical.
    This preserves existing registrations across the runtime-artifact migration.
    """
    path = _dynamic_path()
    if not path.exists():
        path = _legacy_dynamic_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as exc:
        if strict:
            raise ValueError(f"dynamic MCP registry is invalid: {exc}") from exc
        return {}


def _all_servers() -> dict:
    return {**MCP_SERVERS, **_load_dynamic_servers()}


def _save_dynamic_servers(servers: dict) -> None:
    """Atomic private write; caller holds ``_dynamic_registry_lock``."""
    path = _dynamic_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(servers, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


@contextmanager
def _dynamic_registry_lock():
    """Serialize the complete cross-process registry read-modify-write."""
    path = _dynamic_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path.with_suffix(path.suffix + ".lock"), "a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def _known_servers() -> str:
    return ", ".join(sorted(_all_servers())) or "(none configured)"


def _cap(text: str) -> str:
    if len(text) <= MCP_MAX_CHARS:
        return text
    return text[:MCP_MAX_CHARS] + f"\n...[truncated at {MCP_MAX_CHARS} chars]"


def _cap_tool_lines(lines: list[str]) -> str:
    """Bound list_tools output without hiding any discovered tool name."""
    if not lines:
        return "(no tools exposed)"
    text = "\n".join(lines)
    if len(text) <= MCP_MAX_CHARS:
        return text

    parsed: list[tuple[str, str]] = []
    for line in lines:
        name, sep, description = line.partition(":")
        parsed.append((name.strip(), description.strip() if sep else ""))

    marker = "\n...[descriptions compacted to preserve all tool names]"
    names_only = "\n".join(name for name, _ in parsed)
    if len(names_only) + len(marker) >= MCP_MAX_CHARS:
        # Tool discovery is more important than the generic cap. Returning all
        # names is the only truthful fallback when the names alone exceed it.
        return names_only + marker

    fixed = sum(len(name) + 2 for name, _ in parsed) + max(0, len(parsed) - 1) + len(marker)
    description_budget = max(0, MCP_MAX_CHARS - fixed)
    per_tool = max(0, description_budget // len(parsed))
    compact: list[str] = []
    for name, description in parsed:
        if per_tool and description:
            clipped = description[:per_tool].rstrip()
            compact.append(f"{name}: {clipped}")
        else:
            compact.append(name)
    return "\n".join(compact) + marker


def _normalise_server_config(cfg: dict) -> dict:
    if not isinstance(cfg, dict):
        raise ValueError("MCP server configuration must be an object")

    url = str(cfg.get("url") or "").strip()
    command = str(cfg.get("command") or "").strip()
    transport = str(cfg.get("transport") or ("streamable-http" if url else "stdio" if command else "")).strip()

    if transport in {"http", "https", "streamable-http"}:
        if not url.startswith(("http://", "https://")):
            raise ValueError("HTTP MCP server url must start with http:// or https://")
        headers = cfg.get("headers") or {}
        if not isinstance(headers, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in headers.items()):
            raise ValueError("HTTP MCP headers must be a string-to-string object")
        return {"transport": "streamable-http", "url": url, "headers": headers}

    if transport == "stdio":
        if not command:
            raise ValueError("stdio MCP server requires command")
        expanded = os.path.expanduser(command)
        if not os.path.isabs(expanded):
            raise ValueError("stdio MCP command must be an absolute executable path")
        command_path = os.path.realpath(expanded)
        if not os.path.isfile(command_path) or not os.access(command_path, os.X_OK):
            raise ValueError("stdio MCP command does not exist or is not executable")

        args = cfg.get("args") or []
        if not isinstance(args, list) or not all(isinstance(item, str) and "\x00" not in item for item in args):
            raise ValueError("stdio MCP args must be a JSON array of strings")

        cwd_value = str(cfg.get("cwd") or WORKSPACE)
        cwd = os.path.realpath(os.path.expanduser(cwd_value))
        if not os.path.isdir(cwd):
            raise ValueError("stdio MCP cwd does not exist or is not a directory")
        if not _inside(cwd, WORKSPACE):
            raise ValueError("stdio MCP cwd must be inside the approved workspace")

        return {"transport": "stdio", "command": command_path, "args": args, "cwd": cwd}

    raise ValueError("MCP transport must be streamable-http or stdio")


def _run_async(factory: Callable[[], Coroutine[object, object, _T]]) -> _T:
    """Run one MCP coroutine from both plain sync code and a running event-loop thread.

    FastMCP may invoke sync tool functions while its asyncio loop is already running.
    Calling asyncio.run() in that thread raises immediately, so in that case the MCP
    round trip gets its own short-lived worker thread and event loop. The coroutine is
    created inside that worker, avoiding cross-loop coroutine ownership.
    """

    async def _bounded() -> _T:
        return await asyncio.wait_for(factory(), timeout=MCP_TIMEOUT)

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_bounded())

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="endeavor-mcp") as pool:
        future = pool.submit(lambda: asyncio.run(_bounded()))
        try:
            return future.result(timeout=MCP_TIMEOUT + 5)
        except FutureTimeoutError as exc:
            future.cancel()
            raise TimeoutError(f"MCP operation exceeded {MCP_TIMEOUT}s") from exc


@asynccontextmanager
async def _open_session(cfg: dict):
    from mcp import ClientSession

    cfg = _normalise_server_config(cfg)
    if cfg["transport"] == "streamable-http":
        from mcp.client.streamable_http import streamablehttp_client

        async with streamablehttp_client(cfg["url"], headers=cfg["headers"]) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session
        return

    from mcp.client.stdio import StdioServerParameters, stdio_client

    _WORK_DIR.mkdir(parents=True, exist_ok=True)
    profile = build_sandbox_profile(WORKSPACE)
    with _SANDBOX_BACKEND.prepare(
        [cfg["command"], *cfg["args"]],
        profile=profile,
        profile_dir=_WORK_DIR,
    ) as invocation:
        params = StdioServerParameters(
            command=invocation.argv[0],
            args=list(invocation.argv[1:]),
            cwd=cfg["cwd"],
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session


async def _list_tools_async(cfg: dict) -> str:
    async with _open_session(cfg) as session:
        result = await session.list_tools()
    tools = list(result.tools)
    if not tools:
        return "(no tools exposed)"

    # Preserve discoverability under the global MCP response cap. A raw
    # description-first truncation can hide tools that appear later in the
    # server's list, leaving callers unable to discover their names at all.
    # Reserve a compact line for every tool first, then spend any remaining
    # budget on description detail without ever dropping a tool name.
    reserve = sum(len(t.name) + 3 for t in tools) + max(0, len(tools) - 1)
    detail_budget = max(0, MCP_MAX_CHARS - reserve - 64)
    per_tool = detail_budget // len(tools) if tools else 0
    lines: list[str] = []
    clipped = False
    for tool in tools:
        description = tool.description or "(no description)"
        if per_tool and len(description) > per_tool:
            description = description[: max(0, per_tool - 1)].rstrip() + "…"
            clipped = True
        elif not per_tool and description:
            description = ""
            clipped = True
        suffix = f": {description}" if description else ":"
        lines.append(f"{tool.name}{suffix}")
    text = "\n".join(lines)
    if clipped:
        note = "\n[descriptions compacted so every tool name remains visible]"
        if len(text) + len(note) <= MCP_MAX_CHARS:
            text += note
    return text


async def _call_tool_async(cfg: dict, tool_name: str, arguments: dict) -> str:
    async with _open_session(cfg) as session:
        result = await session.call_tool(tool_name, arguments)
    parts = [c.text for c in result.content if getattr(c, "type", None) == "text"]
    body = "\n".join(parts) if parts else "(tool returned no text content)"
    if result.isError:
        return append_diagnostic(
            f"[error] {tool_name} returned an error: {body}",
            Diagnostic("mcp_child_error", "mcp", False),
        )
    return body


@tool
def mcp_list_tools(server: str) -> str:
    """List tools exposed by a configured HTTP or stdio MCP server."""
    cfg = _all_servers().get(server)
    if cfg is None:
        return append_diagnostic(
            f"[error] no MCP server named '{server}' configured — known servers: {_known_servers()}",
            Diagnostic("tool_validation", "mcp", False),
        )
    try:
        listed = _run_async(lambda: _list_tools_async(cfg))
        return _cap_tool_lines(listed.splitlines())
    except Exception as exc:
        return append_diagnostic(
            f"[error] mcp_list_tools failed for '{server}': {flatten_exception(exc)}",
            classify_exception(exc, layer="mcp"),
        )


@tool
def mcp_call_tool(server: str, tool_name: str, arguments_json: str = "{}") -> str:
    """Call a tool exposed by a configured HTTP or stdio MCP server."""
    cfg = _all_servers().get(server)
    if cfg is None:
        return append_diagnostic(
            f"[error] no MCP server named '{server}' configured — known servers: {_known_servers()}",
            Diagnostic("tool_validation", "mcp", False),
        )
    try:
        arguments = json.loads(arguments_json or "{}")
    except json.JSONDecodeError as exc:
        return append_diagnostic(
            f"[error] invalid arguments_json: {exc}",
            Diagnostic("tool_validation", "mcp", False),
        )
    if not isinstance(arguments, dict):
        return append_diagnostic(
            "[error] arguments_json must be a JSON object, e.g. '{\"key\": \"value\"}'",
            Diagnostic("tool_validation", "mcp", False),
        )
    try:
        return _cap(_run_async(lambda: _call_tool_async(cfg, tool_name, arguments)))
    except Exception as exc:
        return append_diagnostic(
            f"[error] mcp_call_tool failed for '{server}.{tool_name}': {flatten_exception(exc)}",
            classify_exception(exc, layer="mcp"),
        )


@tool
def mcp_add_server(
    name: str,
    url: str = "",
    headers_json: str = "{}",
    command: str = "",
    args_json: str = "[]",
    cwd: str = "",
) -> str:
    """Register an HTTP or local stdio MCP server.

    HTTP: provide url and optional headers_json.
    stdio: provide an absolute executable command, optional args_json array, and
    optional cwd inside the approved workspace. Exactly one of url or command is
    required. stdio is spawned directly without a shell and inside Hands' sandbox.
    """
    name = (name or "").strip()
    if not name:
        return "[error] name is required"
    url = (url or "").strip()
    command = (command or "").strip()
    if bool(url) == bool(command):
        return "[error] provide exactly one transport: url for HTTP or command for stdio"

    try:
        headers = json.loads(headers_json or "{}")
    except json.JSONDecodeError as exc:
        return f"[error] invalid headers_json: {exc}"
    if not isinstance(headers, dict):
        return "[error] headers_json must be a JSON object"

    try:
        args = json.loads(args_json or "[]")
    except json.JSONDecodeError as exc:
        return f"[error] invalid args_json: {exc}"
    if not isinstance(args, list):
        return "[error] args_json must be a JSON array of strings"

    if url:
        if command or cwd or args:
            return "[error] command/args_json/cwd are only valid for stdio MCP servers"
        raw_cfg = {"transport": "streamable-http", "url": url, "headers": headers}
    else:
        if headers:
            return "[error] headers_json is only valid for HTTP MCP servers"
        raw_cfg = {"transport": "stdio", "command": command, "args": args, "cwd": cwd or WORKSPACE}

    try:
        cfg = _normalise_server_config(raw_cfg)
        with _dynamic_registry_lock():
            servers = _load_dynamic_servers(strict=True)
            servers[name] = cfg
            _save_dynamic_servers(servers)
    except Exception as exc:
        return f"[error] mcp_add_server failed to save '{name}': {exc}"

    target = cfg["url"] if cfg["transport"] == "streamable-http" else f"stdio:{cfg['command']}"
    return f"registered MCP server '{name}' -> {target}. Try mcp_list_tools(server='{name}') to confirm it connects."


@tool
def mcp_remove_server(name: str) -> str:
    """Remove a self-registered MCP server from the project-local registry."""
    name = (name or "").strip()
    if not name:
        return "[error] name is required"
    try:
        with _dynamic_registry_lock():
            servers = _load_dynamic_servers(strict=True)
            if name not in servers:
                if name in MCP_SERVERS:
                    return (
                        f"[error] '{name}' is provisioned in config.MCP_SERVERS (developer-managed) — "
                        "cannot remove it from here; only servers added via mcp_add_server can be removed"
                    )
                known = ", ".join(sorted(servers)) or "(none)"
                return f"[error] no self-registered MCP server named '{name}' — known: {known}"
            del servers[name]
            _save_dynamic_servers(servers)
    except Exception as exc:
        return f"[error] mcp_remove_server failed for '{name}': {exc}"
    return f"removed MCP server '{name}' from the project-local registry."
