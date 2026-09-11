# CausaDB — Project memory for AI agents

> **Español:** [README en español](README.es.md)

<p align="center">
  <strong>Switch agents without losing your project's context. One verifiable history of what your agents did — regardless of which tool you use.</strong>
</p>

<p align="center">
  <a href="https://github.com/thebuilderpeligroso-netizen/causadb/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License: MIT"></a>
  <a href="https://pypi.org/project/causadb/"><img src="https://img.shields.io/badge/python-3.10%2B-blue.svg" alt="Python 3.10+"></a>
  <a href="https://github.com/thebuilderpeligroso-netizen/causadb"><img src="https://img.shields.io/badge/status-pre--release-orange.svg" alt="Status: pre-release"></a>
  <a href="https://causadb.netlify.app"><img src="https://img.shields.io/badge/website-causadb.netlify.app-5E6AD2.svg" alt="Website"></a>
</p>

<p align="center">
  <img src="docs/assets/demo.gif" alt="CausaDB reviving and reconstructing a project's history" width="800">
</p>

---

## The problem

You're working with an AI agent, deep in the code… and then everything goes dark. An infinite loop, a power cut, a session closed by mistake.

When you come back, the agent is a stranger. It lost the context, forgot the critical details, and you start from zero. You try to "rehydrate" it, but it doesn't remember what was obvious 20 minutes ago.

**The agent doesn't have amnesia because the context window is too small. It has amnesia because nobody recorded what happened.**

It changes files, runs commands, makes decisions, reasons — and it all evaporates with the session.

CausaDB records the operational history of your project into **local, persistent, reconstructible memory**. It doesn't just save conversations: it records *effects, decisions and evidence*, protected by a cryptographic chain, so you can reconstruct what happened whenever you need it.

> **The memory belongs to the project, not the agent.**

Your agent can forget. Your project doesn't.

Three things, always true:

- **Continuity** — switch agents without losing context.
- **Verification** — reconstruct what happened, even when the agent says it's done.
- **Project memory** — one history for all your tools, not tied to any vendor.

---

## Quick Start

```bash
pip install causadb
causadb setup /your/project    # init + hooks + watcher + daemon, all in one
```

Then let it run. When you need it back:

```bash
causadb revive                  # reconstruct state + context summary
```

`revive` reconstructs the state, summarizes what happened, and hands the agent (or you) the full context back: **what events fired, what was inside each file, what was decided and why.**

That's the thing a normal memory doesn't do: **you can verify what the agent claims it did.**

- *"The agent said DONE. The ledger said otherwise."*
- *"We gave a fresh agent a project it had never seen. Instead of explaining the history, we let it reconstruct what happened using CausaDB. And it did."*

That's not promised. It's demonstrated.

---

## What you can do with it

- **Recover context** after an interrupted session — no more starting from zero.
- **Know who touched a line** and what you'd break by reverting it (`trace`, `why`, `impact`).
- **Verify what an agent claims it did.** The record doesn't bend to what the agent *says*.
- **Find the exact event that introduced a bug** (`bisect`).
- **Share memory across different agents** — the memory belongs to the project, not the agent.
- **Keep the project's history even when you switch agents or tools.**

---

## Why not just use normal memory / conversation history?

| | Chat history & vector memory | Observability (traces) | **CausaDB** |
|---|---|---|---|
| **What it records** | Prompts & conversations | LLM/tool call traces | **Real *effects* + decisions + system history** |
| **Survives a session?** | Partially (stale after files change) | Yes, but it's traces | **Yes — append-only, immutable** |
| **Can you verify a claim?** | No | Partially | **Yes — claims vs. recorded events** |
| **Reconstructs *why*** | No | No | **Yes — decisions + reasoning** |
| **Detects an agent lying ("DONE")** | No | No | **Yes, by discrepancy** |
| **Where it lives** | Cloud / SaaS | Cloud / SaaS | **Local, offline, yours** |

There's a sea of products saying *"Your AI remembers."* Most of them mean: *"the user prefers dark mode."*

CausaDB solves a different problem. It was built around:

- what happened and **when**,
- what decision was made and **why**,
- what changed afterwards,
- which agent did it and **what evidence exists**,
- and how to **reconstruct the current state**.

Normal memory *stores*. CausaDB **records effects, decisions and evidence** — cryptographically chained — so you can reconstruct and audit the history, not just recall it.

The difference is between *"your agent remembers"* and *"you can prove what it did."*

CausaDB is **local-first** and works alongside your existing AI tools, without forcing you to change models or vendors.

---

## Installation

```bash
pip install causadb
```

No Python? (compliance officers, traders, students): standalone binaries for Linux/macOS/Windows are coming with the first public release.

### Day-to-day

```bash
causadb watch start --workspace /your/project --daemon  # watcher + mcp-proxy + LLM proxy
causadb revive                       # reconstruct state + context summary
causadb trace /path/file.py 42       # who wrote this line?
causadb impact --event-id <id>       # what breaks if I revert this event?
causadb why file.py:42               # causal attribution of the line
causadb score                        # how productive was the session? (0-100)
causadb audit                        # how much code survives in git? (anti-theatre)
causadb bisect --test "pytest tests" # the exact event that introduced a bug
causadb dashboard                    # full web dashboard
causadb watch stop                   # auto-generates score + skills
```

---

## How it works

CausaDB is a **causal append-only ledger**: a cryptographically-chained (hash-chain) record that writes everything that happens — files, commands, decisions, reasoning — and cannot be altered retroactively. That's what lets you reconstruct and verify instead of trust.

Memory is organized in three tiers:

- **Long-term:** the immutable ledger (cryptographic hash-chain, append-only).
- **Medium-term:** the working set, reconstructible by deterministic replay.
- **Short-term:** OCB (session memory) with granular file detail (pre/post snapshots).

### Key commands

| Command | What it does |
|---|---|
| `revive` | Brings the agent back to life with full context. No start-from-zero. |
| `trace` / `why` / `impact` | Who touched this line? What breaks if I revert this change? |
| `snapshot` + auto-archive | Pre/post snapshots of every file — granular "what was inside" memory. |
| `resume` | A new agent receives the previous session's context. |
| `score` | Was my session productive? 0–100 measuring churn, waste and code survival. |
| `skills` / `distill` | Work patterns learned across sessions, reusable. |
| `bisect` | Find the exact event that introduced a bug. |
| `audit` / `audit-trail` | Full auditability (EU AI Act, NIST AI RMF). |
| `sentinel` / `validate` | Ledger integrity — corrupted? inconsistent events? |
| `watch --daemon` | The agent works alone while you're away. Auto-captures LLM + files + commands. |
| `undo` | Restore a file from the last known-good snapshot. |
| 21 harvest sources | Passive capture: shell, git, browser, ActivityWatch, MT5, Jupyter, Obsidian, Zotero, coding agents, n8n, Freqtrade and more. |

### Agent integration (MCP)

CausaDB exposes an **MCP server with 21 tools + 4 resources** (including `recover` to reconstruct a session's full storyboard from raw source) that any compatible agent invokes in a second:

```bash
causadb opencode-config --project /your/project
```

This generates `causadb.opencode.jsonc`. Add it to your `opencode.jsonc`:

```jsonc
{
  "mcp": {
    "causadb": {
      "type": "local",
      "command": ["python", "-m", "causadb.mcp.server"],
      "enabled": true,
      "environment": {
        "CAUSADB_LEDGER_PATH": "/your/project/.causadb/ledger.log"
      }
    }
  }
}
```

#### Expose the MCP over HTTP (remote agents)

Beyond local stdio, the MCP server can be exposed over **HTTP (streamable-http)** so a remote agent (e.g. in the cloud) can safely consult the project's memory:

```bash
causadb-mcp --transport streamable-http --host 127.0.0.1 --port 8000 --ledger /your/project/.causadb/ledger.log
```

Secure by default:
- **Bind-safety:** without an API key (`CAUSADB_MCP_API_KEY`), it refuses to listen on non-loopback interfaces. No key, your machine only.
- **Read-only subset:** exposes only `revive`, `query`, `ocb_status`, `validate`, `sentinel` and `shared_document_read` — not the write tools (`log`, `shared_document_write`) nor the ones exposing everything (`replay`, `state`).
- **Agnostic coordination:** a remote agent can read the coordination plan (`AUDIT_REPORT` / `ACTION_PLAN`) another agent wrote on your machine — coordination memory belongs to the project, not the agent.
- **Redaction:** sensitive data is redacted before being returned.
- **Client-agnostic:** the same interface works for OpenCode, Claude, Gemini CLI and remote MCP-compatible agents.

#### A communication channel between roles

CausaDB is the project's memory: **tools can change, the project's memory doesn't.** This enables a collaboration pattern that separates responsibilities and saves tokens:

- **The executor** works on the project, writes its plan and executes (access to code and ledger).
- **The observer** (e.g. a frontier model like ChatGPT) **reads** the plan over HTTP and gives its analysis — a second opinion that doesn't interrupt your work or touch the code.
- **Observes and opines, doesn't write:** the observer reads the coordination plan (`AUDIT_REPORT` / `ACTION_PLAN`) but can't alter it. Coordination integrity stays protected.

Benefits: **division of responsibilities** (each brings its own view; the observer catches what the executor, loaded with orchestration, misses) and **token savings** (no need to rewrite context — the observer reads the project's memory, not a kilometer-long prompt).

### Works with any agent — even without MCP

CausaDB doesn't force you to switch tools: **it adapts to your agents**, not the other way around.

- **Standard (MCP) agents:** OpenCode, Claude, Codex, Cursor and the like connect via the MCP server in one command (see above).
- **Agents without MCP** (conversational agents, skills, plugins): CausaDB installs as one more native tool. It leaves a skill file in the agent's skills folder, enabled with a single config line.

**Real case — OpenJarvis:** a conversational agent with access to your project, that discusses improvements and ideas, searches the web and refines your prompts. In one minute, CausaDB adds a **read-only** tool it can use to:

- request `revive` (context summary to resume work),
- audit memory (`query`, `validate`, `sentinel`),
- answer "who wrote this line?" (`why`) or "what depends on what?" (`trace`),
- check session state (`ocb status`).

**Why this is powerful:** CausaDB's memory is **one per project**. All your agents share the same history — what OpenJarvis, OpenCode or anyone else did lands in the same ledger, and anyone can query it. Info is shared through the ledger, not through the agent.

> **Installation note:** the tool reads the ledger the project is connected to. If the history lives in the main project, install CausaDB from there (`causadb init` in that folder) so the agent audits the *real* history — never install it in a generic empty folder.

### Multi-agent coordination

When several agents work on the same project (Maker↔Checker, sub-agents, teams), CausaDB exposes **two shared annotators** in `.causadb/coordination/`:

- **`AUDIT_REPORT`** — written by the Auditor/Checker. States: `DRAFT` / `APPROVED` / `REJECTED` / `CHANGES_REQUESTED`.
- **`ACTION_PLAN`** — written by the Coder/Maker. States: `APPROVE` / `OBJECT` requests.

They're overwritten (the full history is kept by the ledger via `FILE_MODIFIED`). Accessed via the MCP tools `shared_document_read` / `shared_document_write`.

**Typical flow:** Maker writes its plan to `ACTION_PLAN` → Checker reads, verifies and writes the verdict to `AUDIT_REPORT` → Maker executes or adjusts. The full coordination trace lives in the ledger.

### Docs (canon)

CausaDB ships a **canon**: the minimal doctrine for an agent (or human) to interact correctly with the project's memory — a cheap-to-expensive reconstruction ladder, evidence-based audit patterns (P1–P9) and governance rules. Read it with:

```bash
causadb canon          # CLI
```

or the MCP resource `causadb://canon`. It's referenced automatically in `revive` and in each agent's setup.

### Web dashboard

Full visualization without a console: humanized timeline, search, visual causal trace, a revive button, audit export and session metrics.

---

## Platform support

- **Linux:** full support (native double-fork).
- **macOS:** identical to Linux.
- **Windows:** forkless mode (subprocess). Graceful degradation. *(Real-machine validation in progress — see pre-release checklist.)*

### Project layout

```
/your/project/
  .causadb/
    ledger.log           # causal ledger (hash-chain, append-only)
    dag.json             # DAG cache for O(1) trace/impact
    CAUSADB_CHRONICLE.md # narrative log (append-only)
    pids/                # daemon PID files
    logs/                # daemon and proxy logs
    blobs/               # content-addressed snapshots and blobs
    ocb/                 # partitioned session memory (OCB L1)
    skills/              # reconstructible skills cache (ledger-first)
  causadb.opencode.jsonc # MCP template for OpenCode
```

### Requirements

- Python 3.10+
- Linux, macOS, Windows
- No external dependencies (all stdlib)
- File watcher: `pip install watchdog` (optional)

---

## Status

**Preparing for the first public release (v0.2.0-rc1).** Test suite of ~2,135 tests; multi-platform validation in progress.

> **Transparency note:** this repo is the official source. The first public release (`v0.2.0-rc1`) isn't tagged yet — it's in validation (including the Windows run). The `causadb` pip package and standalone binaries ship together with that release.

**⭐ Star the repo to get notified of the first public release.**
**🐛 Found a bug or a real pain this solves?** Open an issue — real feedback shapes the first release.

## Genesis: onboarding an already-started project

CausaDB works with the highest fidelity when installed **from day 1** of a project. Install it at the start and it records every file, command, decision and reasoning as it happens — the full history lands in the ledger.

If you already have a project **with months of history** and now onboard it to CausaDB, **Genesis** reconstructs what it can from what already exists: code structure, git commits, files and Obsidian notes. It's a **robust but incomplete** reconstruction: conversations or processes that left no trace on disk can't be recovered (details in [Known Limitations](#known-limitations)).

> **A project can survive three months without CausaDB. But a project with CausaDB installed from day 1 doesn't just survive — it becomes more reliable than any other project.**

Complete, verifiable memory is the differentiator: the earlier you install it, the more faithful and traceable the history you accumulate.

## 📚 Documentation

- [User Guide](docs/user_guide.md)
- [FAQ](docs/faq.md)
- [Troubleshooting](docs/troubleshooting.md)

---

## Known Limitations

Transparency about the current limits of the product, in order of impact:

1. **Genesis reconstructs structure, not conversations.** Onboarding a project with existing history recovers structure (files, git commits, Obsidian) at high fidelity, but **cannot recover agent conversations or decisions that only live in past sessions' private storage.** Full fidelity is only achieved by installing CausaDB from day 1.

2. **Partial identity attribution (who touched what?).** The ledger always records **what** changed (via passive harvester), and **who** when the change passes through an agent that signs its `session_id`. But **filesystem watcher deletions** (`source="harvester:filesystem"`) carry no actor: deleted content is unrecoverable and unattributed. The `TOOL_CALLED ↔ FILE_MODIFIED` correlation and HMAC signing (`_attribution.py`) exist but aren't enabled in production. It's on the roadmap as debt #22.

3. **`undo` against broken intermediate states.** `undo` restores to the last state that differs from on-disk content (crossing disk + ledger). If the history contains a "broken" intermediate snapshot followed by a good one, `undo` may not automatically pick the last truly-valid one — it doesn't validate code content. For those cases, the [canon](docs/canon.md) documents the manual restore ritual (pattern P3).

4. **Windows in validation.** Windows support degrades to forkless mode (subprocess). Real-machine validation with the multi-platform CI is in progress before the first release.

---

*Last updated: 2026-09-11*