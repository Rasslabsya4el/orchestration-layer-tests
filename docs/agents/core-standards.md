# Coding standards: the generic rules

The orchestration core's rules for how code and the work around it are written, one rule per line, each ending with
its source in parentheses. Nothing here is new: every line collects a rule written in the file it names.
A repo's `CODING_STANDARDS.md` carries these lines verbatim in its first part (a copy, because a review sub-agent
reads files and follows no `@` import) and its own rules in its second.

- The code carries no ledgers, proof surfaces, waivers, blocker taxonomies or publication gates: a function returns a result or raises. (docs/agents/core-block.md, principle 5)
- Validation is a test that measures the outcome, never a schema check. (docs/agents/core-block.md, principle 5; docs/agents/workflow.md)
- No parallel registry of tasks or decisions beside GitHub issues, `GLOSSARY.md` and `docs/adr/`. (docs/agents/core-block.md, principle 5)
- A decision becomes an ADR only when it is hard to reverse, surprising and a real trade-off; the rest lives in the commit message with its numbers. (docs/agents/core-block.md, principle 5; docs/agents/workflow.md)
- A new ADR takes the highest number in `docs/adr/` plus one; an amendment lives inside its ADR. (docs/agents/core-block.md, "Domain docs")
- Human-facing text writes a duration in a unit a human reads: seconds only under ~90 s, then minutes, then hours and minutes past ~90 min; machine files keep raw seconds. (docs/agents/core-block.md, principle 7)
- A rate is quoted per minute, never per 100 s. (docs/agents/core-block.md, principle 7)
- Each commit carries its before/after numbers in the message. (docs/agents/workflow.md)
- A red suite is never merged around: fix the code, or the data the failing tests read, then merge through the merge command. (docs/agents/workflow.md)
- A test that judges built data judges data built from the code being merged, never stale data left in a checkout. (docs/agents/workflow.md)
- A term in a report that is not in `GLOSSARY.md` is a finding for the brain. (docs/agents/core-block.md, "Skills are hard steps")
- A draft of a comment, a table or a report goes to the session's scratchpad directory, never into the repo. (docs/agents/core-block.md, "Every agent, always")
- A process an agent starts never opens a console window of its own: it inherits the session's hidden console or goes through the repo's hidden launcher. (docs/agents/core-block.md, "Every agent, always")
- Every wait is bounded, and a background shell is stopped before the report. (docs/agents/core-block.md, "Every agent, always")
- A skill is called with the Skill tool at the step that names it, before that step's first edit, and the report carries its line. (docs/agents/core-block.md, "Skills are hard steps")
- A git merge or rebase conflict is worked by the agent itself, without a skill. (docs/agents/core-block.md, "Every agent, always")
- Pocock's skills are used as shipped, never copied or overridden. (docs/agents/orch.md; docs/agents/core-block.md, "Big efforts")
- A ticket is opened or rewritten only after calling the skill `write-ticket`. (docs/agents/core-block.md, "Every agent, always")
- A subagent never decides what a domain fact means or which rule to write. (docs/agents/core-block.md, "Roles")
- A permission-guard refusal is final: never retried in another form; the exact call goes into the issue. (docs/agents/workflow.md)
- Enforced by tooling, not reviewed: the tracker guard (labels and areas on `gh`), the permission guard's red list, the worker git and process guards, the leak check of the core's files. (tools/hooks/tracker_guard.py; tools/hooks/permission_guard.py; tools/hooks/worker_git_guard.py; tools/hooks/worker_process_guard.py; tools/agentops/core_check.py)
