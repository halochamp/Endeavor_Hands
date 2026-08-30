# Endeavor Hands project rules

- This public repository is self-contained; do not depend on private parent-repository instructions.
- Read [`AGENT.md`](AGENT.md) and [`AGENT_PROCEDURE.md`](AGENT_PROCEDURE.md) before substantial work.
- Preserve MCP stdio integrity: protocol traffic stays on stdout; diagnostics go to stderr or the existing activity log path.
- Treat file paths, credentials, tokens, Keychain/browser state, private documents, and machine-specific absolute paths as sensitive.
- Preserve the `edit`/`write_file` per-top-level-folder explicit permission gate. Never auto-grant, pre-fill, bypass, weaken, or infer user approval.
- Preserve protected-path checks, canonical-path/symlink defenses, sandbox confinement, deletion protections, and bounded execution.
- Preserve `computer` safety: fresh observation before action, Accessibility requirement, password/secure-field refusal, destructive-action refusal, and post-action verification.
- Do not re-enable raw coordinate control as a shortcut around element/fresh-observation safety.
- Guarded Git mutations must stay scoped to the dedicated `git` tool; do not route Git mutation through shell as a workaround.
- Dynamic MCP stdio servers must use direct argv/no shell and remain sandboxed; do not turn the bridge into arbitrary command execution.
- Model-facing tool descriptions live in the `@mcp.tool()` functions exposed by `server.py`; runtime descriptions and implementation must remain consistent.
- Standard deterministic regression suite: `python3 -m unittest discover -s tests -v`.
- Never weaken a guardrail merely to make a test or workflow easier.
