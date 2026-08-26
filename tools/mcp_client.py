"""mcp_client.py — generic MCP (Model Context Protocol) client over Streamable HTTP

Two registries are merged by name: config.MCP_SERVERS (developer-provisioned,
survives a workspace wipe) and workspace/tool_mcp/servers.json (agent
self-service — config.py lives outside workspace/, and this fork's write_file/
edit enforce a create-only-outside-workspace policy, so the agent itself can
never edit config.py in place; mcp_add_server lets it register a server
without any developer action, see that tool's docstring). Workspace entries
win on a name collision. Uses the official `mcp` SDK's streamablehttp_client +
ClientSession (already installed transitively via browser-use; imported
lazily so a missing package fails as a clean [error], not an import-time
crash for every tool).
"""
from __future__ import annotations
import asyncio
import fcntl
import json
import os
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path
from langchain_core.tools import tool

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import MCP_SERVERS, MCP_MAX_CHARS, MCP_TIMEOUT, WORKSPACE


def _dynamic_path() -> Path:
    return Path(WORKSPACE) / "tool_mcp" / "servers.json"


def _load_dynamic_servers(*, strict: bool = False) -> dict:
    """Best-effort read of the agent-self-service registry — missing file,
    malformed JSON, or a non-dict shape all degrade to {} rather than raise."""
    try:
        data = json.loads(_dynamic_path().read_text(encoding="utf-8"))
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
    tmp = path.with_name(
        f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
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


async def _list_tools_async(url: str, headers: dict) -> str:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async with streamablehttp_client(url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.list_tools()
    lines = [f"{t.name}: {t.description or '(no description)'}" for t in result.tools]
    return "\n".join(lines) if lines else "(no tools exposed)"


async def _call_tool_async(url: str, headers: dict, tool_name: str, arguments: dict) -> str:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async with streamablehttp_client(url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool_name, arguments)
    parts = [c.text for c in result.content if getattr(c, "type", None) == "text"]
    body = "\n".join(parts) if parts else "(tool returned no text content)"
    return f"[error] {tool_name} returned an error: {body}" if result.isError else body


@tool
def mcp_list_tools(server: str) -> str:
    """List the tools exposed by a configured MCP (Model Context Protocol) server.
    Use to discover what a connected MCP server can do before calling mcp_call_tool.
    If the server name isn't known yet, register it first with mcp_add_server.
    Args:
        server: name of the server, as registered via mcp_add_server or config.MCP_SERVERS"""
    cfg = _all_servers().get(server)
    if cfg is None:
        return f"[error] no MCP server named '{server}' configured — known servers: {_known_servers()}"
    try:
        raw = asyncio.run(asyncio.wait_for(
            _list_tools_async(cfg["url"], cfg.get("headers") or {}), MCP_TIMEOUT))
        return _cap(raw)
    except Exception as e:
        return f"[error] mcp_list_tools failed for '{server}': {e}"


@tool
def mcp_call_tool(server: str, tool_name: str, arguments_json: str = "{}") -> str:
    """Call a tool exposed by a configured MCP (Model Context Protocol) server.
    Run mcp_list_tools first to see available tool names and confirm what arguments they expect.
    Args:
        server: name of the server, as registered via mcp_add_server or config.MCP_SERVERS
        tool_name: exact tool name returned by mcp_list_tools
        arguments_json: JSON object string of arguments for the tool, e.g. '{"query": "..."}' (default: no arguments)"""
    cfg = _all_servers().get(server)
    if cfg is None:
        return f"[error] no MCP server named '{server}' configured — known servers: {_known_servers()}"
    try:
        arguments = json.loads(arguments_json or "{}")
    except json.JSONDecodeError as e:
        return f"[error] invalid arguments_json: {e}"
    if not isinstance(arguments, dict):
        return "[error] arguments_json must be a JSON object, e.g. '{\"key\": \"value\"}'"
    try:
        raw = asyncio.run(asyncio.wait_for(
            _call_tool_async(cfg["url"], cfg.get("headers") or {}, tool_name, arguments), MCP_TIMEOUT))
        return _cap(raw)
    except Exception as e:
        return f"[error] mcp_call_tool failed for '{server}.{tool_name}': {e}"


@tool
def mcp_add_server(name: str, url: str, headers_json: str = "{}") -> str:
    """Register a new MCP (Model Context Protocol) server so mcp_list_tools/mcp_call_tool
    can use it. Use this yourself whenever the user gives you an MCP server's URL (and
    optionally an API key/header) to connect — this persists in the workspace, no
    developer action needed.
    To EDIT an existing server (new url and/or headers), call this again with the
    same name — it overwrites that entry. To remove one entirely, use mcp_remove_server.
    Args:
        name: short identifier to refer to this server later, e.g. "worldmonitor"
        url: the MCP server's endpoint, e.g. "https://worldmonitor.app/mcp"
        headers_json: JSON object string of HTTP headers such as an API key, e.g.
            '{"X-WorldMonitor-Key": "wm_xxx"}' (default: no extra headers)
    Example: mcp_add_server(name="worldmonitor", url="https://worldmonitor.app/mcp",
        headers_json='{"X-WorldMonitor-Key": "wm_xxx"}'), then confirm with
        mcp_list_tools(server="worldmonitor")."""
    name = (name or "").strip()
    if not name:
        return "[error] name is required"
    if not url.startswith(("http://", "https://")):
        return "[error] url must start with http:// or https://"
    try:
        headers = json.loads(headers_json or "{}")
    except json.JSONDecodeError as e:
        return f"[error] invalid headers_json: {e}"
    if not isinstance(headers, dict):
        return "[error] headers_json must be a JSON object, e.g. '{\"X-Api-Key\": \"...\"}'"
    try:
        with _dynamic_registry_lock():
            # Mutations are strict: treating malformed JSON as an empty
            # registry would report success while erasing every prior server.
            servers = _load_dynamic_servers(strict=True)
            servers[name] = {"url": url, "headers": headers}
            _save_dynamic_servers(servers)
    except Exception as e:
        return f"[error] mcp_add_server failed to save '{name}': {e}"
    return (
        f"registered MCP server '{name}' -> {url}. "
        f"Try mcp_list_tools(server='{name}') to confirm it connects."
    )


@tool
def mcp_remove_server(name: str) -> str:
    """Remove a previously self-registered MCP server (one added via mcp_add_server).
    Only removes entries from the workspace registry — has no effect on a server
    hardcoded in config.MCP_SERVERS (that requires a developer to edit config.py).
    Args:
        name: the server name to remove, exactly as passed to mcp_add_server"""
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
    except Exception as e:
        return f"[error] mcp_remove_server failed for '{name}': {e}"
    return f"removed MCP server '{name}' from the workspace registry."
