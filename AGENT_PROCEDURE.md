# Endeavor Hands — Agent Procedure

Complete repository-agent workflow for this standalone public project.

Document roles:

- `AGENTS.md` — discovery entry point.
- `CLAUDE.md` — hard constraints.
- `AGENT.md` — quick safety/architecture map.
- **This file** — full execution procedure.
- `README.md` / `CONTRIBUTING.md` / `SECURITY.md` — authoritative public contracts.

## 1. Start-of-task procedure

Before changing code:

1. Read `CLAUDE.md` and `AGENT.md`.
2. Inspect Git status and preserve unrelated work.
3. Classify the change: MCP protocol/logging, filesystem/read-write, shell/sandbox, Git, computer/GUI, nested MCP, document/media parsing, configuration, or docs.
4. Read the corresponding README/CONTRIBUTING/SECURITY section.
5. Identify the exact security/protocol invariant.
6. Prefer a small change with a deterministic regression test.

## 2. Protocol and logging boundary

Hands is an MCP stdio server. Protocol integrity is mandatory.

- MCP protocol traffic belongs on stdout only.
- Human/diagnostic logging belongs on stderr or the existing activity log path.
- Do not print debug text to stdout from server/tool paths.
- Keep model-facing schemas/descriptions stable or deliberately version/update tests/docs when changed.
- Validate protocol/handshake behavior after server-facing changes.

## 3. Filesystem permission gate

The per-top-level-folder permission gate is an explicit user-consent boundary.

For `edit` and replacement of an existing file:

1. First attempt may return `[permission_required]` and a one-time nonce.
2. The agent must ask the user directly for permission.
3. Do not assume approval from task context.
4. Do not pass the nonce speculatively.
5. Only retry after the user explicitly approves, using the exact nonce.
6. Once granted, scope remains limited to the documented top-level folder/session.

Never weaken this into global approval, implicit approval, or auto-retry.

Creating a brand-new file follows the documented tool contract and does not justify bypassing replacement/edit gates later.

## 4. Path and sandbox boundary

For filesystem/shell changes, verify:

- protected system/credential paths remain blocked;
- canonicalization resolves `..` and symlink aliases before policy decisions where applicable;
- shell/Python writes remain within the intended sandbox/workspace policy;
- temporary/test paths are controlled;
- errors do not expose secrets;
- deletion/destructive filesystem commands remain refused where documented;
- timeout/bounded-output behavior remains intact.

Do not add a shell escape or alternate execution path that bypasses the same protection enforced by a dedicated tool.

## 5. Guarded Git procedure

Git mutation is a privileged capability and belongs in the dedicated Git tool.

Preserve:

- approved-workspace/repository scope;
- explicit-path staging;
- commit-only-staged behavior;
- non-force push;
- disabled hooks/signing during guarded commits;
- safe handling of Git metadata and stale index locks;
- credential handling through the trusted mechanism described by the implementation;
- refusal of unsafe transport/workarounds.

Do not implement a fallback that tells the model to use `bash git ...` when the guarded tool refuses an operation. A refusal is a boundary, not a routing hint.

Add regression coverage for any change to status/diff/add/commit/push or repository validation.

## 6. Computer/GUI safety procedure

`computer` must remain observation-driven and fail-closed.

Preserve the loop:

```text
observe -> choose one bounded action -> inspect effect/new observation -> continue
```

Rules:

- start from a fresh observation when state is unknown;
- prefer element IDs/current observation over stale text/targets;
- do not reuse stale element IDs after the screen changes;
- refuse password/secure-text fields;
- refuse delete/remove-looking actions under the destructive-action policy;
- retain Accessibility requirements;
- verify state after mutations;
- recover from silent failures by re-observing, not blindly repeating;
- do not restore raw coordinate control as a bypass around current safety/determinism.

A deterministic unit test is not the same as a live GUI acceptance test. State which one ran.

## 7. Nested MCP boundary

For `mcp_list_tools`, `mcp_call_tool`, `mcp_add_server`, and `mcp_remove_server`:

- dynamic registrations must remain scoped to the intended registry/workspace;
- local stdio servers use direct argv, never a shell wrapper;
- stdio child processes remain under Hands' sandbox;
- do not turn registration fields into arbitrary shell execution;
- do not leak configured headers/API keys in logs or responses;
- preserve bounded output/error handling;
- distinguish transport failure from remote tool failure.

Adding a new transport or persistence mechanism requires explicit security review and tests.

## 8. Read/write/edit behavior

When changing file tools:

1. Read the shared path/policy helpers and all affected callers.
2. Preserve differences between new-file creation, in-place edit, full replacement, and outside-workspace edited-copy behavior.
3. Preserve document/image/media conversion limits and bounded outputs.
4. Verify refusal paths and permission-gate paths, not only success.
5. Keep model-facing descriptions aligned with runtime semantics.

Do not silently broaden readable/writable areas.

## 9. Bash/Python/background jobs

For process execution changes:

- preserve the sandbox profile and protected paths;
- preserve timeout behavior;
- keep short tasks on synchronous execution and long-lived tasks on the bounded background-job registry;
- preserve background-job count/registry/log bounds;
- do not use Python execution as a shell bypass or shell as a Python/file-policy bypass;
- report stdout/stderr truncation honestly.

## 10. Document/OCR/media changes

For `read_file` and parsing helpers:

- use synthetic/non-private fixtures;
- preserve size/page/output limits;
- prefer deterministic native parsing over OCR when text is extractable;
- keep OCR/transcription privacy local as documented;
- do not expose arbitrary connector file bytes or credentials;
- verify behavior on malformed/unsupported inputs.

## 11. Model-facing tool descriptions

`server.py`'s decorated MCP functions are the model-facing source of truth.

If changing a tool argument, name, default, description, recovery hint, or permission behavior:

1. update runtime implementation and tool description together;
2. verify MCP schema/handshake exposure;
3. add/update regression tests;
4. update README/CONTRIBUTING/SECURITY when user-facing semantics change.

Do not rely on an internal helper docstring to correct a stale MCP-visible description.

## 12. Testing procedure

Standard deterministic regression suite:

```bash
python3 -m unittest discover -s tests -v
```

Run targeted tests first for the changed boundary, then the full suite before completion.

For live macOS integration changes (Accessibility, Keychain, GUI, real sandbox behavior, tunnel behavior), run the smallest safe live acceptance test only when required and available. Keep deterministic and live/manual verification claims separate.

Never weaken assertions or skip a security test merely to make a patch pass.

## 13. Privacy and release hygiene

Before sharing/commit:

- inspect status/diff;
- ensure no `.env`, API key, tunnel ID, credential, token, Keychain/browser state, personal document, personal absolute path, logs, workspace state, generated helper binary, or private screenshot is staged;
- follow README's "Before sharing" privacy guidance;
- keep examples generic.

## 14. Documentation changes

Keep roles distinct:

- `AGENT.md` = quick map;
- this file = detailed agent procedure;
- `CLAUDE.md` = hard rules;
- README = product/user documentation;
- CONTRIBUTING = contributor expectations;
- SECURITY = security boundary/reporting.

Do not import private parent-repository workflow or user-specific secrets into this public repo.

## 15. Completion criteria

A task is complete when applicable items hold:

- requested behavior is implemented;
- relevant security/protocol invariants remain intact;
- targeted and full deterministic tests pass;
- any required live integration verification is clearly separated from unit evidence;
- no permission gate/sandbox/protected-path/computer-safety boundary was weakened;
- final diff contains only intended changes;
- privacy/release checks are clean.

**Decision rule:** if convenience conflicts with an explicit guardrail, preserve the guardrail and redesign the workflow.
