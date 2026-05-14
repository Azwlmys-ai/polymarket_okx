# AGENT_BRIDGE.md

This file is the collaboration bridge between Claude Code and ChatGPT Codex.

All agents must operate only inside:

```text
/Users/libo/polymarket_okx
```

Forbidden in Phase 1:
- real-money trading
- private key handling
- withdrawals
- browser automation
- Selenium or GUI clicking
- leverage trading
- complex profit-seeking strategy before data quality is proven

## Roles

Claude Code:
- primary implementer
- writes code
- runs tests
- updates implementation notes
- stops after each approved step

ChatGPT Codex:
- architecture reviewer
- risk reviewer
- code reviewer
- approves or rejects each step
- writes concise review notes and next-step instructions

## Required Reading Order

At the start of every agent session, read only these files first:

1. `PROJECT_CONTEXT_V2.md`
2. `AGENT_BRIDGE.md`
3. `AGENT_STATE.md`
4. `AGENT_TASKS.md`
5. `AGENT_REVIEW.md`

Do not scan unrelated directories.

If more context is needed, inspect only project files under `/Users/libo/polymarket_okx`.

## Token Hygiene

Do not paste full files into chat unless explicitly requested.

Prefer:
- concise summaries
- filenames changed
- key decisions
- test commands and results
- risks and blockers

Avoid:
- repeating the full project context
- dumping long logs
- copying generated code into chat when files already exist
- restating forbidden actions every time unless relevant

## Context Compression Command For Claude Code

When Claude Code finishes a step, update `AGENT_STATE.md` with:

```text
## Claude Compression Summary

Current step:
Files changed:
Behavior implemented:
Commands run:
Test result:
Known risks:
Open questions:
Next requested action:
```

Keep it under 120 lines.

Then stop and wait for Codex review.

## Context Compression Command For ChatGPT Codex

When Codex finishes a review, update `AGENT_REVIEW.md` with:

```text
## Codex Review Summary

Review target:
Verdict: APPROVED / CHANGES_REQUESTED / BLOCKED
High-risk findings:
Medium-risk findings:
Low-risk findings:
Required changes:
Approved next step:
Reviewer notes:
```

Keep it under 120 lines.

If approved, write the next implementation task into `AGENT_TASKS.md`.

## Step Gate

Only one development step is active at a time.

Current phase order:

1. Project initialization
2. OKX public data collection
3. Polymarket public data collection
4. Lag recording
5. Lag distribution report
6. Paper trading
7. Profitability evaluation

Claude Code must not proceed to the next step until Codex writes an approved next step in `AGENT_TASKS.md`.

## File Update Rules

Claude Code should update:
- `AGENT_STATE.md`
- source files
- tests
- README when behavior changes

Codex should update:
- `AGENT_REVIEW.md`
- `AGENT_TASKS.md`

Both agents may read all bridge files.

## Communication Format

Use concise, append-friendly Markdown.

Every update should include:
- ISO-like timestamp if convenient
- agent name
- step name
- summary
- test result
- next action

## Safety Reminder

If any requested change requires real trading, private keys, withdrawals, or browser automation, mark it as `BLOCKED` in `AGENT_REVIEW.md` and do not implement it.

