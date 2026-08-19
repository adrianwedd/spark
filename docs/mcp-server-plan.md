> **Stale as of 2026-08-19.** This plan describes the voice loop as calling
> `claude -p --no-session-persistence` per turn. It no longer does: voice turns
> are a classified `voice_turn` request to the resident `spark-brain` session,
> and production code may not invoke Claude non-residently at all. See the
> resident-only invariant in CLAUDE.md. The MCP reasoning below may still be
> worth reading; its description of the current architecture is not.

# SPARK MCP Server & Agentic Architecture — Implementation Plan

## Context

Currently SPARK's voice loop calls `claude -p --no-session-persistence` for each voice turn. This is intentionally stateless — the full context (system prompt + session highlights + recent thoughts + transcript) is rebuilt and injected every turn. Memory is file-based: `notes-spark.jsonl`, `thoughts-spark.jsonl`, `session.json`.

The user has flagged three related improvements:

1. **MCP server** — expose SPARK's tools to the Claude Code dev session on the Pi, so development work (controlling the robot, testing tools, filing issues) can happen from here without going through the voice loop.
2. **Stateful sessions** — SPARK's conversation should accumulate context across turns, not rely entirely on file injection. Especially important if SPARK is going to make repo changes mid-conversation.
3. **SPARK as autonomous agent** — SPARK should be able to suggest features, implement them, commit, push, and create GH issues — either on its own initiative or prompted by Obi.

These three concerns are deeply related and the plan addresses them together.

---

## Current Architecture (what we have)

```
[Obi speaks]
     │
px-wake-listen (Vosk + SenseVoice STT)
     │  sets session.listening=true, session.transcript="..."
     │
voice_loop.py (polling session.json)
     │  build_model_prompt() → system prompt + session highlights + thoughts
     │
claude-voice-bridge
     │  claude -p "$PROMPT" --allowedTools "" --no-session-persistence
     │  returns one JSON action line
     │
validate_action() → execute_tool()
     │
bin/tool-* subprocess → JSON result
     │
session.json updated
```

**What's missing:**
- Claude has no memory of what it said 2 turns ago — it only sees injected file snippets
- Claude cannot use any tools beyond SPARK's tool set (no file access, no git, no GH)
- No way for the dev session (Claude Code here) to send commands to the robot directly
- SPARK cannot modify its own code or file issues

---

## Proposed Architecture

Three layers, each buildable independently:

```
┌─────────────────────────────────────────────────────────────┐
│  Layer 3: SPARK Autonomous Agent                            │
│  SPARK suggests → implements → commits → files GH issue    │
│  (Claude with agentic tools: git, gh, file r/w, web search)│
└───────────────────────────┬─────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────┐
│  Layer 2: Stateful Voice Session                            │
│  Persistent Claude session per conversation window          │
│  Conversation buffer file (last N turns, injected)          │
│  --no-session-persistence REMOVED                           │
└───────────────────────────┬─────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────┐
│  Layer 1: MCP Server (build first)                          │
│  bin/mcp-server — JSON-RPC 2.0 over stdio                   │
│  Claude Code (this session) → controls SPARK directly       │
│  All 41 tools exposed with full parameter schemas           │
└─────────────────────────────────────────────────────────────┘
```

---

## Layer 1 — MCP Server

### What it does

Exposes all 41 SPARK tools to Claude Code (this dev session) as MCP tools. From this terminal, I can say "make SPARK look left" and it happens. No voice loop needed. Good for:
- Development and testing
- Demoing to Obi without waking the voice loop
- Letting Claude Code read session state and recent thoughts while we work

### Protocol

MCP uses JSON-RPC 2.0 over stdio. The server is registered in `.claude.json` under `mcpServers`. Claude Code spawns it on start, keeps it alive, and calls it whenever an MCP tool is invoked.

```json
// .mcp.json (project-level MCP config, read by Claude Code)
{
  "mcpServers": {
    "spark": {
      "command": "/home/pi/picar-x-hacking/bin/mcp-server",
      "args": [],
      "env": {}
    }
  }
}
```

### Files to create

**`bin/mcp-server`** — bash launcher
```bash
#!/usr/bin/env bash
source "$(dirname "$0")/px-env"
exec "$PROJECT_ROOT/.venv/bin/python3" \
    "$PROJECT_ROOT/src/pxh/mcp_server.py" "$@"
```

**`src/pxh/mcp_server.py`** — MCP server (~350 lines)

Key components:
1. **JSON-RPC stdio loop** — reads lines from stdin, dispatches to handlers, writes responses to stdout. All logging goes to stderr (not stdout, which is the MCP transport).
2. **`tools/list` handler** — returns all 41 tools with JSON Schema for each parameter set. Schemas are derived directly from `validate_action()` — same source of truth, no duplication.
3. **`tools/call` handler** — calls `validate_action(tool, params)` → `execute_tool(tool, env, dry=False)` → returns stdout as MCP text content.
4. **`resources/list` + `resources/read` handlers** — expose session state, recent thoughts, and notes as readable MCP resources:
   - `spark://session` — current `session.json`
   - `spark://thoughts` — last 10 entries from `thoughts-spark.jsonl`
   - `spark://notes` — last 20 entries from `notes-spark.jsonl`
   - `spark://logs/{tool}` — last 50 lines of a specific tool log

### Tool schema generation

Instead of hand-writing 41 schemas, generate them from a declarative map alongside `validate_action()`:

```python
# In voice_loop.py (or a new schemas.py)
TOOL_SCHEMAS = {
    "tool_voice": {
        "description": "Speak text aloud via espeak through SPARK's speaker.",
        "properties": {
            "text": {"type": "string", "maxLength": 2000, "description": "Text to speak"}
        },
        "required": ["text"],
    },
    "tool_drive": {
        "description": "Drive SPARK forward or backward with optional steering.",
        "properties": {
            "direction": {"type": "string", "enum": ["forward", "backward"], "default": "forward"},
            "speed":     {"type": "integer", "minimum": 0, "maximum": 60, "default": 30},
            "duration":  {"type": "number", "minimum": 0.1, "maximum": 10.0, "default": 1.0},
            "steer":     {"type": "integer", "minimum": -35, "maximum": 35, "default": 0},
        },
    },
    # ... all 41 tools
}
```

This also serves as documentation.

### MCP resources for dev context

When Claude Code (this session) has `spark://session` and `spark://thoughts` available as resources, it can:
- See what persona is active without asking
- Know if Obi is mid-routine
- Read SPARK's recent inner thoughts
- Know if motion is allowed before suggesting drive commands

### Installation

```bash
# Install mcp package in venv
.venv/bin/pip install mcp

# Test server starts
echo '{"jsonrpc":"2.0","method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{}},"id":1}' \
  | bin/mcp-server
```

Register in `.mcp.json` at repo root (Claude Code auto-discovers this file).

---

## Layer 2 — Stateful Voice Session

### The problem with stateless

Every turn currently:
- Injects: system prompt (~4KB) + session highlights (~500B) + 3 recent thoughts (~600B) + transcript
- Has NO memory of: what SPARK said last turn, what Obi said before that

This means SPARK can ask "what's your name?" and then ask it again 2 turns later. It also means SPARK can't build on a thread across turns without the conversation being injected into notes explicitly.

### Option A: Conversation buffer file (recommended for Phase 1)

Add `state/conversation-spark.jsonl` — a rolling buffer of the last 10 voice turns (alternating user/assistant entries). Inject into `build_model_prompt()` as a "Recent conversation" section.

- No change to `claude -p` invocation
- Works with existing stateless bridge
- Buffer is trimmed to last N turns to stay within token budget
- Persona-scoped (`conversation-spark.jsonl`, `conversation-vixen.jsonl`)

```python
# In build_model_prompt():
convo_file = STATE_DIR / f"conversation-{persona}.jsonl" if persona else STATE_DIR / "conversation.jsonl"
if convo_file.exists():
    turns = [json.loads(l) for l in convo_file.read_text().splitlines()[-10:]]
    convo_section = "\n".join(f"{t['role'].upper()}: {t['text'][:300]}" for t in turns)
    prompt += f"\n\n## Recent conversation\n{convo_section}"
```

After each tool execution, append to the buffer:
```json
{"ts": "...", "role": "user", "text": "can we do the morning routine"}
{"ts": "...", "role": "spark", "text": "Morning! Step one: drink some water.", "tool": "tool_routine"}
```

### Option B: Persistent Claude session (recommended for Layer 3)

Remove `--no-session-persistence` from `claude-voice-bridge`. Claude maintains its own internal session. Each turn pipes into the same session. This gives genuine multi-turn memory without file injection.

**Trade-offs:**
- Pro: True conversational memory, no injection overhead
- Pro: Required for Layer 3 (agentic Claude needs tool-call history)
- Con: Session can drift / accumulate context over very long sessions
- Con: Session file lives in `~/.claude/` — harder to inspect and debug
- Con: Claude may reference past tool results that are no longer valid

**Recommendation:** Implement Option A first (low risk, visible, debuggable). Move to Option B when implementing Layer 3.

---

## Layer 3 — SPARK as Autonomous Agent

### Vision

SPARK can suggest and implement features. The flow:

```
Obi: "Hey SPARK, I wish you could tell me a joke."
SPARK: "I can't do that yet — but I could learn. Want me to try and add it?"
Obi: "Yeah!"
SPARK: [thinks for a moment]
      "Okay. I'm going to add a joke tool. Your dad will need to approve it
       before it goes live, but I'll write it now."
      [opens file, writes bin/tool-joke, adds to voice_loop.py, commits]
      "Done. I've sent it to the waiting list. Your dad can turn it on."
```

### What this requires

1. **Persistent Claude session** (Layer 2 Option B) — so SPARK has conversational context when it decides to make changes
2. **Allowed tools expanded in `claude-voice-bridge`:**
   - `Read`, `Write`, `Edit` — file access within the repo
   - `Bash` — for `git`, `gh` commands (scoped)
   - `WebSearch` — for researching implementations
3. **Safety guardrails** — SPARK cannot push to remote without Adrian's approval. All changes land in a feature branch. Adrian reviews via PR.
4. **Intent detection in spark-voice-system.md** — SPARK knows when to use agent tools vs. regular SPARK tools

### `claude-voice-bridge` changes

```bash
# Current:
claude -p "$PROMPT" \
  --system-prompt "..." \
  --allowedTools "" \               # ← blocks everything
  --output-format text \
  --no-session-persistence          # ← stateless

# Layer 3 mode:
claude -p "$PROMPT" \
  --system-prompt "..." \
  --allowedTools "Read,Write,Edit,Bash,WebSearch" \   # ← scoped tools
  --output-format text
  # --no-session-persistence REMOVED
```

### Safety model for repo modification

SPARK operates on a `spark/auto` branch, never `master`. Changes are accumulated there. Adrian reviews and merges.

```bash
# px-spark sets up the agent branch on launch:
git checkout -B spark/auto 2>/dev/null || true
```

SPARK's Bash tool is wrapped by a restriction list: no `git push --force`, no `git reset --hard`, no `rm -rf`. Only: `git add`, `git commit`, `git checkout -b`, `gh issue create`, `gh pr create`.

When SPARK commits a change, it speaks: "I've written the code and saved it. Your dad can check it when he's ready."

### Interaction with the MCP server

Once Layer 3 is live:
- Claude Code (this session) can read the `spark/auto` branch via the MCP `spark://session` resource
- Claude Code can pull SPARK's pending changes and review/merge them
- SPARK and Claude Code are collaborating on the same repo, from different entry points

---

## Implementation Order

### Step 1 — MCP Server (1-2 sessions)

1. Create `src/pxh/mcp_server.py` with stdio JSON-RPC loop
2. Add `TOOL_SCHEMAS` dict to `src/pxh/voice_loop.py` (or `src/pxh/schemas.py`)
3. Create `bin/mcp-server` launcher
4. Install `mcp` package in venv: `.venv/bin/pip install mcp`
5. Create `.mcp.json` at repo root
6. Test: verify Claude Code can call `tool_voice` from this session
7. Add MCP resources: `spark://session`, `spark://thoughts`, `spark://notes`
8. Add dry-run tests in `tests/test_mcp.py`

### Step 2 — Conversation Buffer (1 session)

1. Add `state/conversation-{persona}.jsonl` write to `execute_tool()` (or voice loop post-step)
2. Add conversation injection to `build_model_prompt()`
3. Trim buffer to last 10 turns
4. Add `.gitignore` entry for `state/conversation-*.jsonl`
5. Test: verify SPARK remembers what it said 2 turns ago

### Step 3 — Agentic Claude (1-2 sessions)

1. Add `--allowedTools` flag to `claude-voice-bridge` (configurable, not hardcoded)
2. Add `PX_CLAUDE_ALLOWED_TOOLS` env var (default: `""` for safety)
3. `px-spark` sets `PX_CLAUDE_ALLOWED_TOOLS=Read,Write,Edit,Bash,WebSearch` in agentic mode
4. Add `spark/auto` branch setup to `px-spark`
5. Update `spark-voice-system.md` with repo-modification guidance and safety rules
6. Add Bash tool restriction config (`.claude/settings.json` `bash_restrictions`)
7. Test: SPARK adds a simple tool (`bin/tool-joke`), commits to `spark/auto`, speaks confirmation

---

## Web UI Status

**Already implemented** (previous session). The REST API at port 8420 serves:

- `GET /` — self-contained dark-theme HTML dashboard (no external dependencies)
  - Sidebar buttons for every SPARK tool with quick-action forms
  - Chat input → `POST /api/v1/chat` → LLM pick-a-tool → execute
  - Session state panel (persona, mood, routine, quiet mode)
- `POST /api/v1/chat` — text → Claude → tool → result (stateless, same `claude -p` bridge)
- All other REST endpoints documented in README

Access via: `http://<pi-ip>:8420/` with API token in browser (prompted on first load).

**Gaps to address in web UI:**
- No conversation history display (chat is stateless — each message is a fresh LLM turn)
- No live session state updates (currently static, no WebSocket)
- Adding conversation buffer (Layer 2) would fix the statefulness gap in the web UI too
- A WebSocket endpoint for live log streaming would make the UI much more useful for dev

---

## File Summary

| File | Status | Layer |
|------|--------|-------|
| `src/pxh/mcp_server.py` | To create | 1 |
| `src/pxh/schemas.py` | To create | 1 |
| `bin/mcp-server` | To create | 1 |
| `.mcp.json` | To create | 1 |
| `tests/test_mcp.py` | To create | 1 |
| `state/conversation-{persona}.jsonl` | To create (runtime) | 2 |
| `src/pxh/voice_loop.py` | Update `build_model_prompt()` | 2 |
| `bin/claude-voice-bridge` | Update `--allowedTools`, remove `--no-session-persistence` | 3 |
| `bin/px-spark` | Add agentic mode flag, branch setup | 3 |
| `docs/prompts/spark-voice-system.md` | Add repo-mod guidance + safety rules | 3 |

---

## Open Questions for Adrian and Obi

1. **Agent safety**: How much autonomy should SPARK have? Always ask before modifying code? Only write, never commit? Or full commit-to-branch?
2. **Review flow**: Should Adrian get a GitHub notification every time SPARK proposes a change, or batch them?
3. **Obi's involvement**: Should SPARK explain what it's doing in kid-friendly terms as it codes, or work quietly and report when done?
4. **Session length**: How long should a conversation window persist? Per wake-word session? All day? Reset at midnight?
5. **Feature branch name**: `spark/auto`? `obi-ideas`? Let Obi name it?
