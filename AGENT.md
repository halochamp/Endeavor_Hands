# Endeavor Hands — Agent Overview

Quick operating map for agents working directly in this public repository.

Read order:

1. [`CLAUDE.md`](CLAUDE.md) — hard security/protocol constraints.
2. **This file** — quick architecture and decision map.
3. [`AGENT_PROCEDURE.md`](AGENT_PROCEDURE.md) — full execution procedure.
4. [`README.md`](README.md), [`CONTRIBUTING.md`](CONTRIBUTING.md), [`SECURITY.md`](SECURITY.md) — authoritative product, contributor, and security contracts.

## Core model

Endeavor Hands is a privileged local macOS MCP stdio server. The model is the planner; Hands exposes a small set of deep primitives for local files, shell/Python, guarded Git, GUI control, and nested MCP access.

Safety boundaries are product behavior, not implementation details.

## Start every task

Before editing:

1. inspect Git status;
2. classify the change as protocol/logging, filesystem, shell/sandbox, Git, computer/GUI, nested MCP, parsing/OCR/media, or docs;
3. read the corresponding SECURITY/CONTRIBUTING section;
4. identify the exact guardrail that must remain true;
5. add/run deterministic regression coverage for boundary changes.

## Non-negotiable boundaries

- MCP protocol output stays on stdout only; diagnostics use stderr/logging.
- `edit` and replacement of an existing file require explicit user permission for the top-level folder on first use in a session.
- Never fabricate or auto-use a permission nonce before the user approves it.
- Protected paths/credentials remain unreadable or unmodifiable according to the documented boundary.
- Sandbox/path confinement and symlink/canonicalization defenses stay fail-closed.
- File deletion/destructive actions stay guarded.
- `computer` works from a fresh observation, refuses password/secure fields and destructive-looking actions, and verifies mutations.
- Raw coordinates are not a substitute for deterministic element targeting.
- Git mutation uses the guarded Git tool, not shell Git mutation.
- Nested stdio MCP uses direct argv/no shell and Hands' sandbox.

## Model-facing tool contract

The descriptions in `server.py`'s `@mcp.tool()` functions are part of runtime behavior. If a schema, description, default, or recovery hint changes, verify both the exposed contract and the implementation/tests.

## Testing

Standard deterministic suite:

```bash
python3 -m unittest discover -s tests -v
```

Run targeted tests first for the touched boundary, then the full suite before completing a code change.

GUI behavior that requires real macOS Accessibility may need a live/manual verification in addition to deterministic unit tests. Do not claim live GUI verification when only mocks/subprocess tests ran.

## Git/release hygiene

Before commit/push, ensure no `.venv/`, logs, workspace state, tunnel binary, `.env`, credentials, tokens, private documents, or machine-specific absolute paths are staged.

Full workflow: [`AGENT_PROCEDURE.md`](AGENT_PROCEDURE.md).

**Mental model:** few powerful tools + explicit user gates + fail-closed local safety + deterministic verification.
