# orchestration-layer

This file is the repo's own part. Add this repo's own principles, layout and rules above the
import line below; never edit the imported file itself (a core file: a port or an update
overwrites it).

A chat that is not the brain, when it is an interactive chat the owner opened, hands every change, ticket and owner
decision to the live brain chat (the one `brain_chat_title` names) with SendMessage, unless the owner gives that chat the work
itself. This is for an interactive chat only: ORCH, the unblocker and subagents are not meant.

## Agent skills

### Issue tracker

GitHub Issues of `orchestration-layer`, through the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

The five default triage labels. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context. See `docs/agents/domain.md`.

## The orchestration core

@docs/agents/core-block.md
