# Research an external model does

What this repo adds is in `docs/agents/local/research.md`, read it when it exists.

The external researcher is GPT through the opencode bridge (`tools/agentops/`, websearch on),
Luna 5.6 for simple briefs and Terra 5.6 for complex ones (the brief's writer picks); the brief format and
the verbatim-quote rules below stay.

An outside-world question put to researcher-how goes through the `research` skill (`mattpocock-skills:research`, its file written at `<Worktree>/docs/research/<file>` in the worktree the prompt names, so it reaches main with the issue's merge; `docs/agents/orch.md`, "Worktree: <path>"); its report carries `research: called, docs/research/<file>`; researcher-find has the Skill tool but no such rule; the GPT bridge stays for forums and live web search.

## How we use GPT, do not improvise

Order of trust: the repo's own tool and its readbacks, then the wiki, then Reddit / forums, then the
owner's memory (the owner: the wiki outranks them). A number the tool does not compute is never typed
from model memory: it is a fact with a source, stored with the place it plugs into.

- **The wiki** of the domain is read by the main model itself in the Browser pane (an anti-bot check
  may need a few seconds; bots and GPT's fetchers are refused). Cached under `var/cache/wiki/`:
  the raw page, and a compact extract (the sections that carry the numbers only),
  which is the only form an agent reads afterwards.
- **Reddit and forums** are reached only by GPT with live web search (Claude's browser is refused reddit.com),
  through opencode's websearch with `OPENCODE_ENABLE_EXA=1` over the `tools/agentops` bridge.
  opencode's page fetcher alone does not reach it. Write a
  brief under `docs/research/briefs/<date>-<topic>.md` (numbered questions, a Reddit or forum URL
  with a short quote required per answer, "not found" allowed, the report file named), then run
  `python -m tools.agentops research docs/research/briefs/<brief>.md --model luna|terra [--issue N]`.
  One brief holds one topic; questions on unrelated topics go into separate briefs, each its own run, started in
  parallel by the brief's writer.
  Add `--no-reddit` for a brief whose sources are not forums (papers, official docs, engineering blogs): the
  prompt drops the Reddit instruction and no reddit.com URL is required.
  Otherwise it rejects a report that cites no reddit.com URL. A report from tooltips alone is a failed task.
  When the bridge prints `research bridge cannot start`, the research goes to researcher-how (internal) and the report says the bridge was unavailable.
  Reports quote the sources VERBATIM with URL, date and author, keep the conditions the sources
  attach, and end each question with one "what it says" line; the analysis and the number for the
  system are the main model's: a digested number hides the condition its source attached to it.
- **A run's status**: `artifacts/runs/gpt-research-<slug>.status` is one line, rewritten about every 15 seconds while the
  run goes: steps so far, minutes since the last step, sources opened, `reddit reached: yes|no`. The caller who started
  the run reads it every few minutes. A run with no new step for 10 minutes is stopped by the tool itself (not the
  orchestrator) and keeps its partial report at `docs/research/<slug>.partial.md`, beside the report path; the default
  limit is 20 minutes.
- Never: the built-in `openai/*` models in opencode (a direct OpenAI account with no credits;
  only `codex-sale-oai/...` from `opencode.json`, and only GPT / GLM / DeepSeek: the other
  opencode providers are the user's local models); the main model's browser for Reddit (refused).
- **GPT code workers** are an option, not the default route; code goes to `worker` / `worker-strong`
  (`docs/agents/orch.md`). The Claude weekly limit is shared with Opus subagents, so code
  tasks from a spec may instead run through `python -m tools.agentops worker <spec>.md --model gpt-5.6-terra --minutes N
  [--issue N]`; opencode, model
  `codex-sale-oai/gpt-5.6-terra` at reasoning `xhigh` from `opencode.json`; own worktree branch
  `gpt-<slug>`, report in `artifacts/runs/gpt-worker-<slug>.md`; the main model reviews the diff and merges.

  The bridge (`tools/agentops/worker.py`) commits a worker's uncommitted tree
  as WIP on every exit path, writes a heartbeat line every 10 minutes into `artifacts/runs/gpt-worker-<slug>.out`,
  runs ONE automatic continuation after a time limit / stall / missing report, and ends the `.out` file with the
  reason, the commit count, `wip commit` and `report: present / MISSING`; a worker's final message begins with
  `=== FINAL REPORT ===`.

- GPT's answers are checked against the repo's own tool and the wiki before use, land as **pending** facts
  (optimistic lane and a "would move the number by X%" note only), and enter the conservative
  number after the user approves them from one table (fact, number, sources, confidence, which
  builds it moves). Where the verdicts and the approved numbers are recorded is the local file's.
