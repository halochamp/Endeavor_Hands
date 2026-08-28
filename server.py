"""server.py — Endeavor Hands' MCP entry point.

Exposes local-machine tools (bash, git, bash_bg, python_exec, read_file, write_file,
edit, computer, and the mcp_* bridge) to an MCP client over stdio — designed for
ChatGPT web via OpenAI Secure MCP Tunnel (see README.md's "Connect this server
to ChatGPT" section), but works with any MCP client (Claude Desktop,
`mcp dev server.py`, Codex CLI, ...).

Run with the project's own virtual environment (see README.md's "Install"
section — `bash install_library/install.sh` creates it and installs
Quartz/AppKit/cv2/langchain_core/mcp):

    .venv/bin/python3 server.py

Every tool call is logged to stderr live and to logs/agent_activity.jsonl
persistently (see _logged() below). stdout is reserved for MCP's own JSON-RPC
framing — never print() there.
"""
from __future__ import annotations

import functools
import io
import os
import sys
import time
from pathlib import Path

from mcp.server.fastmcp import FastMCP, Image

from agent_log import AgentLogger

from tools.bash import _bash_impl
from tools.git import _git_impl
from tools.python_exec import _python_exec_impl
from tools.read_file import _IMAGE_EXT, _read_file_impl
from tools.computer_use import _computer_impl, pop_last_image_path

from tools.bash_bg import bash_bg as _bash_bg_tool
from tools.write_file import write_file as _write_file_tool
from tools.edit import edit as _edit_tool
from tools._safety import plan_write
from tools._edit_grants import check_grant
from tools.mcp_client import (
    mcp_list_tools as _mcp_list_tools_tool,
    mcp_call_tool as _mcp_call_tool_tool,
    mcp_add_server as _mcp_add_server_tool,
    mcp_remove_server as _mcp_remove_server_tool,
)

# ── Logging: live terminal view (stderr) + persistent record (logs/agent_activity.jsonl) ──
# AgentLogger (agent_log.py) is a plain ring-buffer JSONL logger — no LangGraph/LLM dependency.

_logger = AgentLogger()


def _logged(name: str):
    """Wrap a tool function: log the call/result/error to stderr (live) and to
    AgentLogger (persistent), then return the wrapped function's result unchanged.

    stdout is the MCP JSON-RPC channel — never print() there. flush=True because
    stderr is block-buffered when not a TTY (e.g. launched by tunnel-client), so
    without it nothing would appear until the process exits."""

    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            tid = _logger.new_turn_id()
            _logger.tool_call(name, kwargs, tid)
            print(f"[{time.strftime('%H:%M:%S')}] → {name} {kwargs}", file=sys.stderr, flush=True)
            try:
                out = fn(*args, **kwargs)
                _logger.tool_result(name, str(out), tid)
                preview = str(out)[:200]
                print(f"[{time.strftime('%H:%M:%S')}] ← {name}: {preview}", file=sys.stderr, flush=True)
                return out
            except Exception as e:
                _logger.tool_error(name, e, tid)
                print(f"[{time.strftime('%H:%M:%S')}] ✗ {name}: {e}", file=sys.stderr, flush=True)
                raise

        return wrapper

    return deco


mcp = FastMCP(
	"Endeavor Hands",
    instructions="""You are connected to Endeavor, the user's local Mac workspace assistant.

When the user asks to inspect, search, create, modify, test, build, run, or otherwise work with files,
projects, processes, or apps on their computer, use the Endeavor tools instead of only describing how to
do the task. Use read_file for text files and local images, write_file for new or complete files, edit for
existing files, bash for searches/tests/builds/short commands, git for guarded repository operations,
python_exec for Python analysis, and computer for visible Mac app interaction. The workspace is the user's Desktop.

For read-only requests, do not modify files. For requested changes, work only within the user's stated
scope, verify relevant results with an appropriate Endeavor tool, and report the files changed. Files may
be created or edited, but must never be deleted; do not use deletion commands, delete/remove UI actions,
or destructive cleanup. If a request needs a credential, payment, or irreversible action, ask the user to
perform or approve it explicitly.""",
)


# ── bash ────────────────────────────────────────────────────────────────────
@mcp.tool()
@_logged("bash")
def bash(command: str, timeout: int = 30) -> str:
    """Run a bash command on the local machine (cwd = workspace) — system operations, run scripts,
    check processes/disk/memory. NOT for arithmetic, math, or data analysis (pandas/statistics) —
    use python_exec for those; never for shell-wrapped Python.

    GUI scripting via bash is NOT available (osascript System Events is blocked by this tool's
    sandbox) — for clicking/typing/screen interaction use the `computer` tool instead, not osascript.
    ❌ switch to Chrome → osascript keystroke/cmd+tab tricks   ✅ → open -a "Google Chrome" (or `computer`)

    NETWORK — basic read-only checks (ping, netstat, ifconfig, arp) are fine via bash.

    FILE SEARCH — cwd = workspace/, so relative paths (find ., ls) only search there. Elsewhere on
    the machine, use absolute paths or ~:
      mdfind -name "keyword"                              -> macOS Spotlight, whole-disk, fastest, try first
      find ~ -iname "*keyword*" 2>/dev/null | head -20    -> fallback when mdfind misses unindexed files
      rg -n "keyword" ~/Desktop/<project>                  -> search file CONTENT fast (grep -rl fallback if no rg)
      common dirs: ~/Desktop, ~/Documents, ~/Downloads

    MAC APP CONTROL (sandbox-safe subset): open -a "App" opens/raises an app (hidden window comes to
    front); open <path|URL> opens with its default app; pbpaste / echo "x" | pbcopy read/write the
    clipboard; osascript -e 'display notification "..." with title "..."' shows a notification;
    osascript -e 'set volume output volume 40' / -e 'output volume of (get volume settings)' sets/reads volume.

    FILE DISCOVERY / CODE SEARCH inside workspace: rg --files (flat file list, find "$PWD" -type f
    fallback); rg --files | rg "\\.md$" (find by name/pattern); rg -n "needle" path_or_dir (search text
    with line numbers).

    FILE WRITE — sandboxed: workspace/ (currently ~/Desktop) and /tmp allow writes. cwd is ALREADY
    workspace/ — do not
    re-prefix "workspace/" onto the output path (writes one level too deep).
      ❌ screencapture workspace/shot.png  -> lands at workspace/workspace/shot.png
      ✅ screencapture shot.png            -> lands at workspace/shot.png

    Output cap: output over 10,000 chars is truncated; the FULL output is saved to a workspace file
    whose path is in the leading "[bash] truncated: ..." marker.
    """
    return _bash_impl(command, timeout)


# ── git ─────────────────────────────────────────────────────────────────────
@mcp.tool()
@_logged("git")
def git(
    action: str,
    repo: str = ".",
    paths: list[str] | None = None,
    message: str = "",
    remote: str = "origin",
    branch: str = "",
    staged: bool = False,
    timeout: int = 60,
) -> str:
    """Guarded Git operations for repositories inside the approved workspace.

    Use this instead of bash for Git mutation. Supported actions: status, diff, add, commit, push.
    `add` requires explicit paths; `commit` commits only already-staged changes; `push` uses an
    existing configured remote and never force-pushes. Git hooks and commit signing are disabled
    for guarded commits so repository code cannot escape the intended operation. A confirmed stale
    `.git/index.lock` may be moved to a timestamped backup when no writer owns it; non-empty locks
    must also parse successfully as a complete Git index before automatic recovery is allowed.
    HTTPS pushes obtain stored macOS Git credentials through a fixed trusted helper path.

    Mutation actions should only be used when the user explicitly asked for the corresponding Git
    change. Source-file deletion remains blocked; the extra unlink permission is scoped only to the
    selected repository's Git metadata directory.
    """
    return _git_impl(
        action=action,
        repo=repo,
        paths=paths,
        message=message,
        remote=remote,
        branch=branch,
        staged=staged,
        timeout=timeout,
    )


# ── bash_bg ─────────────────────────────────────────────────────────────────
@mcp.tool()
@_logged("bash_bg")
def bash_bg(action: str, command: str = "", job_id: str = "") -> str:
    """Background bash jobs — start a long-running command without blocking, then poll/list/kill it.

    Use for anything that legitimately runs past `bash`'s timeout and where you don't need the
    result the instant it finishes: dev servers (npm start, uvicorn), big downloads, long
    builds/installs. For anything that finishes in seconds, use `bash` directly.

    actions:
      start  — command required. Spawns under the same sandbox as `bash` (workspace/ and /tmp
               writable only), stdout+stderr redirected to a log file in workspace/. Returns
               job_id + log path right away; the command keeps running after this call returns.
               Max 5 concurrent jobs.
      status — job_id required. Returns running/exited (+ exit code when known) and the last
               ~2,000 chars of the log.
      list   — no args. Lists every tracked job: id, status, command, started_at.
      kill   — job_id required. SIGTERM, then SIGKILL after 3s if still alive.

    ❌ bash_bg(action="start", command="pytest test_foo.py") — finishes in 5s, just use bash.
    ✅ bash_bg(action="start", command="npm run dev") → poll with status later.
    """
    return _bash_bg_tool.func(action=action, command=command, job_id=job_id)


# ── python_exec ───────────────────────────────────────────────────────────
@mcp.tool()
@_logged("python_exec")
def python_exec(code: str, timeout: int = 120, max_chars: int = 10_000) -> str:
    """Run Python code (cwd = workspace) using the SAME interpreter that runs this server — data
    analysis (pandas), statistics (scipy.stats), regression/time-series (statsmodels), machine
    learning (sklearn).

    NOT for: shell commands (use bash), file I/O without Python (use read_file/write_file).

    Only stdout (print(...)) reaches you back — a computed value that's never printed is invisible
    and shows up as "(no output)".

    CAPABILITIES:
      Data analysis: pandas — read CSV/Excel/JSON, filter/groupby/aggregate, .describe()/.corr()
      Statistics: scipy.stats — hypothesis tests (ttest_ind, pearsonr, chi2_contingency, ANOVA)
      Regression / time-series: statsmodels.api — sm.OLS(y, sm.add_constant(x)).fit().summary(), ARIMA
      Machine learning: sklearn — regression, clustering, PCA, train_test_split
      File I/O inside workspace/ — read a file saved by an earlier tool call, write a derived CSV/txt
      Quick matplotlib plots as a side effect of an analysis script (matplotlib.use('Agg') before
      importing pyplot, then plt.savefig(...) — no display in this sandboxed subprocess)

    HOW:
      workspace/ is the cwd: pd.read_csv('data.csv') / df.to_csv('out.csv') use relative paths directly.
      Errors: a traceback prints under an "[stderr]" header; a crash with zero stdout instead returns
      "[error] exited <code>, no output". Common errors (ModuleNotFoundError, FileNotFoundError,
      KeyError, UnicodeDecodeError, SyntaxError) also get a one-line "[hint]" — read it before retrying.
      A hint can list more than one option — if the FIRST fails, try the NEXT before giving up.

    LIBRARIES available: pandas, numpy, matplotlib, scipy, scikit-learn, statsmodels.

    Output cap: printed output over max_chars (default 10,000) is truncated to the last complete
    line; the FULL untruncated output is saved to a workspace file and its path is in a
    "[python_exec] truncated: ..." marker. No marker means the output is complete.

    IMPORTANT — truncation only affects what got PRINTED. Any aggregate you already computed
    (df.sum(), df.groupby(...).mean(), .describe(), etc.) ran on the complete in-memory data
    regardless of the cap — those numbers are NOT samples. Only mention truncation when your
    answer relies on specific rows/values beyond the visible cutoff.
    """
    return _python_exec_impl(code, timeout, max_chars)


# ── read_file ───────────────────────────────────────────────────────────────
@mcp.tool()
@_logged("read_file")
def read_file(
    path: str,
    user_query: str = "",
    line_start: int = 0,
    line_end: int = 0,
    page_start: int = 0,
    page_end: int = 0,
    contains: str = "",
    contains_any: list[str] | None = None,
    contains_all: list[str] | None = None,
    regex: str = "",
    regex_flags: str = "",
    whole_word: bool = False,
    doc_mode: str = "",
    context_lines: int = 3,
):
    """Read file contents — plain text, code, PDF/Word/Excel documents, and audio/video.

    Prefer line_start/line_end (1-indexed, inclusive) to read a known section directly instead of
    the whole file; fall back to bash (`rg -n`, `find`, `rg --files`) only when the location isn't
    known yet. Scanned or image-only PDFs are OCR'd page-by-page when Apple Vision OCR is available;
    if OCR is unavailable, the result is an explicit [error] with the recovery reason.

    IMAGES: use this same tool directly for PNG, JPG/JPEG, GIF, BMP, WebP, HEIC/HEIF, and TIFF files
    (for example, `read_file(path="~/Desktop/screenshot.png")`). The image is read-only, converted to
    PNG, and attached for you to inspect and describe; no separate image tool is needed. Image files
    must be at most 50 MB and 40,000,000 pixels. They are resized so their longest side is at most
    2048 pixels before attachment. `line_start`, `line_end`, page ranges, search filters, and `doc_mode`
    do not apply to image files.

    Also supports PDF page ranges (page_start/page_end) and text/regex search filters (contains,
    contains_any, contains_all, regex, doc_mode).

    line_start/line_end use the same numbering shown by `rg -n`/`grep -n` style file:line output; no
    effect on PDF/DOCX/XLSX/XLS, audio/video, or image files. Code files over the size limit return a
    structure map (symbols + line numbers) unless a line range is given, in which case that exact
    range is returned instead.

    PDF/DOCX/XLSX/XLS are converted to markdown; large documents are sampled for coverage (outline +
    paragraphs/rows spread evenly across the whole file, with "[... skipped N ...]" markers — this is
    plain local sampling, not an LLM call, and is expected behavior on large files, not a bug).

    PDF PAGE RANGE: page_start/page_end (1-indexed, inclusive, PDF only) reads exact pages directly
    instead of a sampled/truncated read — works for extractable-text and scanned/OCR PDFs alike. Each
    call stops at a clean page boundary once either 30 pages or the output budget is reached, and
    states the exact page_start to pass next in a trailing [note: ...] line. Mutually exclusive with
    line_start/line_end, the search filters below, and doc_mode.

    Audio/video (m4a/mp3/wav/aiff/mp4/mov, etc.) are transcribed on-device (Thai) via native Apple
    Speech — the full raw transcript is saved to a .md file in workspace and the transcript text (or a
    locally-sampled excerpt if very long — never LLM-summarized) is returned here; first-ever call on
    this machine needs Siri enabled in System Settings and a one-time consent click.

    SEARCH FILTERS (plain-text/code, and PDF/DOCX/XLSX/XLS after markdown conversion) — pass one,
    returns only matching sections with surrounding context_lines (default 3):
      contains="keyword"            case-insensitive substring
      contains_any=["a","b"]        line matches if it has any keyword
      contains_all=["a","b"]        line matches if it has every keyword
      regex="pattern"                regular expression search
      regex_flags="ims"              i=ignorecase, m=multiline, s=dotall
      whole_word=true                word-boundary match for contains/contains_any/contains_all

    DOC_MODE (document files only, requires a search filter above): "heading" markdown headings
    only; "section" whole section when heading or any line inside matches; "row" markdown table rows
    only; "cell" individual table cells.

    Files larger than READ_FILE_MAX_BYTES (default 50 MB) are rejected — use bash (rg -n / find /
    rg --files) to target sections (audio/video get a separate, larger limit). Exception: a PDF read
    with page_start/page_end is exempt, since it only ever touches the requested pages.

    Pass user_query with what you're looking for so specific figures in a large document aren't
    sampled away.
    """
    # Existing ChatGPT app snapshots may not yet include the newer read_image
    # tool. Keep image reading available through this long-lived tool schema so
    # those clients receive the image instead of an unusable redirect.
    if path and Path(path).suffix.lower() in _IMAGE_EXT:
        return _read_image_file(path)

    return _read_file_impl(
        path, user_query, line_start, line_end, page_start, page_end,
        contains, contains_any, contains_all, regex, regex_flags,
        whole_word, doc_mode, context_lines,
    )


# ── image support for read_file ─────────────────────────────────────────────
def _read_image_file(path: str, max_dimension: int = 2048):
    if not path:
        return "[error] path is required"
    if not isinstance(max_dimension, int) or max_dimension < 256 or max_dimension > 4096:
        return "[error] max_dimension must be an integer from 256 to 4096"

    try:
        from PIL import Image as PILImage
        from tools._safety import resolve_read_path

        image_path = Path(resolve_read_path(path))
        if not image_path.is_file():
            return f"[error] image file not found: {path}"
        if image_path.stat().st_size > 50 * 1024 * 1024:
            return "[error] image file is larger than 50 MB"

        with PILImage.open(image_path) as source:
            source.load()
            width, height = source.size
            if width * height > 40_000_000:
                return f"[error] image is too large: {width}x{height} pixels (limit 40,000,000)"
            image = source.convert("RGBA") if "A" in source.getbands() else source.convert("RGB")
            image.thumbnail((max_dimension, max_dimension))
            encoded = io.BytesIO()
            image.save(encoded, format="PNG", optimize=True)

        data = encoded.getvalue()
        return [
            f"[image] {image_path.name}: {width}x{height} → {image.width}x{image.height} PNG",
            Image(data=data, format="png"),
        ]
    except Exception as exc:
        return f"[error] image read failed: {exc}"


# ── write_file ────────────────────────────────────────────────────────────
@mcp.tool()
@_logged("write_file")
def write_file(path: str, content: str, overwrite: bool = False, grant_phrase: str = "") -> str:
    """Create a new workspace file, or replace the whole file when overwrite=true.

    PERMISSION GATE — replacing a file that ALREADY EXISTS (overwrite=true on an existing
    path) uses the exact same per-folder gate as `edit`: the first such write to a given
    top-level folder this session fails with [permission_required] and a one-time nonce.
    Ask the user directly, never assume yes, then retry with grant_phrase set to that nonce.
    A folder granted through `edit` also already covers write_file here, and vice versa —
    it is one shared per-folder grant, not per-tool. Creating a brand-new file never needs
    grant_phrase.

    Use this for full-file writes: creating a new file from scratch, saving generated output to a
    named file, or intentionally replacing the entire contents of an existing file.

    Do NOT use this for small/localized changes inside an existing file — use `edit` for that. This
    tool never appends and never does partial in-place edits: when overwrite=true it replaces the
    whole file atomically.

    path      : workspace-relative ("notes/out.md", "script.py") or an absolute path outside the
                workspace ("~/Desktop/out.md")
    content   : the exact full file contents to write
    overwrite : default false; if the file already exists, set true only when you intend to replace
                the entire file

    Outside the workspace: NEW files can be created anywhere except protected system paths; an
    EXISTING outside file is never replaced in place — the write is redirected to a sibling working
    copy "name.edited.ext" (the result reports the actual path written).

    Returns an [error] if the target exists and overwrite is false. For code/scripts, follow with
    `bash` to verify when execution matters.
    """
    if overwrite:
        effective_path, err, _note = plan_write(path)
        if err:
            return err
        if os.path.exists(effective_path):
            grant_err = check_grant(effective_path, grant_phrase)
            if grant_err:
                return grant_err
    return _write_file_tool.func(path=path, content=content, overwrite=overwrite)


# ── edit ──────────────────────────────────────────────────────────────────
@mcp.tool()
@_logged("edit")
def edit(
    path: str,
    old_string: str = "",
    new_string: str = "",
    replace_all: bool = False,
    near_line: int = 0,
    line_start: int = 0,
    line_end: int = 0,
    edits: list | str | None = None,
    grant_phrase: str = "",
) -> str:
    """Modify an EXISTING file. Three modes — pick one:

    PERMISSION GATE — the first edit targeting a given top-level folder this session
    fails with [permission_required] and a one-time nonce. Ask the user directly, in
    this conversation, whether they allow editing files in that folder — never assume
    yes and never pass grant_phrase speculatively. Only after the user says yes, call
    edit(...) again with grant_phrase set to the exact nonce from that error. Once
    granted, every file under that folder stays editable for the rest of this session
    — a different top-level folder needs its own separate grant.

    STRING (default): old_string→new_string; old_string must be a unique substring of the file (or
    replace_all=true). near_line disambiguates when old_string matches >1 place.

    LINE: set line_start (1-indexed). line_end>=line_start replaces that inclusive range (empty
    new_string deletes it); line_end omitted inserts new_string as new line(s) after line_start
    (new_string required for insert).

    BATCH: pass edits=[{...}, ...] — each item uses the same fields as above (old_string OR
    line_start, not both). Applied in order to ONE file, atomically: any hunk failure discards the
    whole batch, nothing is written. A later hunk's line numbers are resolved against the file as
    already changed by earlier hunks in the SAME batch — order line-based hunks bottom-to-top
    (highest line_start first) if a batch mixes them.

    Use write_file for new files, or write_file with overwrite=true for full rewrites. Files OUTSIDE
    the workspace are never modified in place: the edit is applied to a sibling working copy
    "name.edited.ext" (created from the original on the first edit, reused by later edits) — the
    result reports the copy's path.

    .py files get an inline syntax check after a successful write (✓/⚠ appended to the result) — a
    syntax error is reported but NOT reverted; no separate bash round-trip needed just to catch it.
    """
    effective_path, err, _note = plan_write(path)
    if err:
        return err
    grant_err = check_grant(effective_path, grant_phrase)
    if grant_err:
        return grant_err
    return _edit_tool.func(
        path=path, old_string=old_string, new_string=new_string, replace_all=replace_all,
        near_line=near_line, line_start=line_start, line_end=line_end, edits=edits,
    )


# ── computer ──────────────────────────────────────────────────────────────
@mcp.tool()
@_logged("computer")
def computer(
    action: str,
    target: str = "",
    text: str = "",
    coord: str = "",
    direction: str = "",
    amount: int = 3,
    near: str = "",
    element_id: str = "",
    observation_id: str = "",
    question: str = "",
    expect: str = "",
    modifiers: str = "",
    app: str = "",
):
    """Control a native Mac app one guarded action at a time. You see the newest screen image after
    every call (attached to this tool's result) and also receive a compact [OBS] Accessibility/OCR
    element list as text.

    Start with action="see" when state is unknown — attaches the current screenshot and returns
    [OBS obs_N] with eN elements to act on (scroll to reveal more). Prefer element_id="eN" +
    observation_id="obs_N" over target text for click/double_click/triple_click/right_click/hover/
    scroll/drag. coord is disabled. Look at the attached image / read [OBS] after every mutation.

    Silent-failure recovery: click → no_visible_change → detect failure from effect/image/[OBS] →
    see or inspect → retry a different current element/target. Never repeat or claim success. A
    Delete/remove-looking click/drag targets and permanently-remove hotkeys are refused when the
    runtime has file-deletion protection enabled.

    Actions: see/inspect; click/double_click/triple_click/right_click/hover/drag; type literal text;
    key(text=one key/combo, e.g. enter or cmd+s); scroll(direction, amount, optional target/element);
    open_app(target=installed app); open_url(target=full HTTP(S) URL, app=installed browser). Use
    open_url when a site should open directly in Chrome/Safari.
    ❌ submit typed URL → type(text="return")
    ✅ submit typed URL → key(text="enter")
    ❌ site requested → open_app(app="Google Chrome")
    ✅ site requested → open_url(target="https://www.youtube.com", app="Google Chrome")

    expect=<kind>:<value> asks the tool to check state AFTER the action and report it
    in the result's effect note, instead of you re-inspecting by eye. Four kinds:
    expect="focus:<label>" — is the named element focused now (Accessibility-based);
    expect="app:<name>" — is this app frontmost; expect="window:<text>" — does the
    window title contain this text; expect="text:<text>" — is this text visible
    on screen. Recognized-kind results append +verified (met) or +expectation_not_met
    (not met) to effect; if Accessibility can't be checked right now it appends
    +expect_unknown instead of a false negative. A bare prose expect with no kind
    prefix (e.g. expect="Search field focused") is only searched as literal on-screen
    text and will usually fail — use one of the four kinds above instead; a failed
    unrecognized-form expect also returns a "hint:" line repeating this.
    KNOWN LIMIT — in Chrome/Chromium, expect="focus:<label>" is reliable for the
    browser's own controls (address bar, buttons) but not for a field INSIDE a
    loaded web page (a page's search box, a form input): Chrome only builds its
    web-content accessibility tree once it detects a persistent assistive-technology
    client, which this tool is not, so page-content elements may be invisible to
    Accessibility even though ax=ok. For those, verify with expect="text:<value>"
    (does the typed text now appear on screen) instead of focus:.
    ❌ verify focus by prose → expect="Search field focused"
    ✅ verify focus by prose → expect="focus:Search"

    [computer usage notes — observe → one mouse/key action → verify]
    CONTROL LOOP — 1) If state is unknown, see. 2) Choose the smallest single action. 3) Look at the
    new image + [OBS] + effect. 4) Continue only from the newest observation; prefer
    element_id+observation_id. 5) If no_effect/warning/unexpected screen, do not repeat or claim
    success: see/inspect, identify the changed focus/popup/target, then try a different safe action
    once. Decompose every app task into this loop; never chain actions from an old screen.

    MOUSE — click focuses/selects/presses; double_click opens a file/item; triple_click selects a
    text block; right_click opens a context menu; hover reveals hidden controls/tooltips; drag uses
    target=<source text>, text=<drop-target text>; scroll uses direction=up/down/left/right,
    amount=<positive lines>, and target/element_id when a specific pane must scroll. Use visible text
    or newest eN, never raw coordinates. If text matches several places, retry with
    near=top/bottom/left/right/center/corners. After a menu, right-click, hover, drag, or scroll,
    inspect the newly revealed state before acting.

    KEYBOARD — type(text=...) inserts literal Unicode into the currently focused editable field only.
    key(text=...) sends ONE key/combo: enter, tab, shift+tab, escape, space, arrows, cmd+a/c/v/s/f/l,
    shift+arrow. To replace field text: click field → verify focus → key(cmd+a) → type(new text) →
    verify. To submit: key(enter). Put modifiers inside key text (cmd+s), not the modifiers argument;
    modifiers is for click/drag selection. Tab moves focus, shift+tab moves back, escape safely closes
    a menu/dialog. Switch apps with open_app, not cmd+tab. Never type passwords, OTPs, payment data,
    or credentials.

    COMMON APP PATTERNS — form: click field → type → tab/click next → type → click Submit → verify
    result. Menu/dialog: click menu/button → look at new OBS → click item/choice → verify
    dismissal/result. Editor: click text → cmd+a only when the whole field/document should be
    replaced → type → cmd+s → verify content/title. Finder/list: double_click opens; right_click then
    inspect exposes actions; modifier-click selects multiple items. Multi-pane apps: scroll the named
    pane, not wherever the pointer happened to remain.

    RECOVERY EXAMPLES — click Play → no_visible_change → see/inspect → dismiss safe popup or choose
    the current Play → click once → verify progress. Type lands in the wrong place → stop typing →
    see → click the intended editable field → cmd+a only there → type again → verify visible text.
    Drag changes nothing → see → select the current source and visible drop target → retry once;
    otherwise report the boundary instead of guessing.

    POPUPS/ADS — inspect first; click an unambiguous Skip/close or Play once and verify. Never bypass
    CAPTCHA, sign-in, age/subscription/region gates, or ads deceptively; stop for manual intervention.

    `inspect` zooms into element_id (or the whole observation if omitted) and attaches that crop
    directly as an image — look at it yourself, no separate description step.
    """
    text_result = _computer_impl(
        action=action, target=target, text=text, coord=coord, direction=direction,
        amount=amount, near=near, element_id=element_id, observation_id=observation_id,
        question=question, expect=expect, modifiers=modifiers, app=app,
    )
    img_path, ephemeral = pop_last_image_path()
    if not img_path:
        return text_result
    try:
        data = Path(img_path).read_bytes()
    except Exception:
        return text_result
    finally:
        if ephemeral:
            Path(img_path).unlink(missing_ok=True)
    return [text_result, Image(data=data, format="png")]


# ── MCP bridge ────────────────────────────────────────────────────────────
@mcp.tool()
@_logged("mcp_list_tools")
def mcp_list_tools(server: str) -> str:
    """List the tools exposed by a configured MCP (Model Context Protocol) server.
    Use to discover what a connected MCP server can do before calling mcp_call_tool.
    If the server name isn't known yet, register it first with mcp_add_server.
    Args:
        server: name of the server, as registered via mcp_add_server or config.MCP_SERVERS
    """
    return _mcp_list_tools_tool.func(server=server)


@mcp.tool()
@_logged("mcp_call_tool")
def mcp_call_tool(server: str, tool_name: str, arguments_json: str = "{}") -> str:
    """Call a tool exposed by a configured MCP (Model Context Protocol) server.
    Run mcp_list_tools first to see available tool names and confirm what arguments they expect.
    Args:
        server: name of the server, as registered via mcp_add_server or config.MCP_SERVERS
        tool_name: exact tool name returned by mcp_list_tools
        arguments_json: JSON object string of arguments for the tool, e.g. '{"query": "..."}' (default: none)
    """
    return _mcp_call_tool_tool.func(server=server, tool_name=tool_name, arguments_json=arguments_json)


@mcp.tool()
@_logged("mcp_add_server")
def mcp_add_server(name: str, url: str, headers_json: str = "{}") -> str:
    """Register a new MCP (Model Context Protocol) server so mcp_list_tools/mcp_call_tool can use it.
    Use this yourself whenever the user gives you an MCP server's URL (and optionally an API
    key/header) to connect — this persists in the workspace, no developer action needed.
    To EDIT an existing server (new url and/or headers), call this again with the same name — it
    overwrites that entry. To remove one entirely, use mcp_remove_server.
    Args:
        name: short identifier to refer to this server later, e.g. "worldmonitor"
        url: the MCP server's endpoint, e.g. "https://worldmonitor.app/mcp"
        headers_json: JSON object string of HTTP headers such as an API key, e.g.
            '{"X-WorldMonitor-Key": "wm_xxx"}' (default: no extra headers)
    """
    return _mcp_add_server_tool.func(name=name, url=url, headers_json=headers_json)


@mcp.tool()
@_logged("mcp_remove_server")
def mcp_remove_server(name: str) -> str:
    """Remove a previously self-registered MCP server (one added via mcp_add_server).
    Only removes entries from the workspace registry — has no effect on a server hardcoded in
    config.MCP_SERVERS (that requires a developer to edit config.py).
    Args:
        name: the server name to remove, exactly as passed to mcp_add_server
    """
    return _mcp_remove_server_tool.func(name=name)


if __name__ == "__main__":
    mcp.run()  # stdio transport
