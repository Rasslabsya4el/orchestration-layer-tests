# Porting the orchestration core

A core file. This is the port's own account of itself, copied unedited into every target; a
target never edits it (the update rule below, not a local rewrite, is how a target's copy
changes).

## The core and the manifest

The orchestration core is the fixed set of files that turns an issue into merged work in any
repo: the brain, ORCH, the dispatcher, workers and researchers, their role files and protocols,
the token meter and the outcome log. It holds nothing of the repo it runs in. `core_manifest.txt`
names every one of those files, one repo-relative path per line; the leak check
(`core_check.py`) reads the same manifest to find them, at the home or at a copy elsewhere
(`--root`).

## What a target may edit

A target never edits a manifest file: an update rewrites every one of them and would lose an
edit silently. What a target adds goes into its own local extension files, joined to the core
at the extension points named in the core's own design record: `CLAUDE.md`'s own part (above the
one import line), a `docs/agents/local/<name>.md` counterpart next to a core protocol,
`config/orchestration.json`'s `local_modules`, and a local module's own `register(core)`
(`extensions.py`). A target's `CLAUDE.md` and `.claude/settings.json` keep every line and key of
their own; the port only adds what is missing.

## Order: Pocock's setup first, the port second

The first step of porting the core into a target is **Pocock's own setup**, run in the target
repo: `/setup-matt-pocock-skills`, answered with the four values the core supports -- **GitHub**
(Section A), the **default triage labels** (Section B), **single-context** domain docs (Section
C), and `CLAUDE.md` as the file it edits (step 4 of the skill's own process). Only once that has
written `docs/agents/issue-tracker.md`, `docs/agents/triage-labels.md`, `docs/agents/domain.md`
and the target's `## Agent skills` block does the port run. The three files are the
target's own from then on: the port writes them only where they are missing, never overwrites or
deletes them, and `core update` never touches them (`TARGET_OWNED_ENTRIES`, `port.py`). The order no longer loses a rule, because the rules this repo added to those three files now live in the core's `docs/agents/workflow.md` ("Tickets and labels"), which both orders copy and `core update` keeps current, so a target that ran Pocock's setup first gets them too.

| order | the three files | `CLAUDE.md`'s `## Agent skills` heading |
|---|---|---|
| Pocock's setup, then the port | Pocock's own, from the target's answers; the port leaves them (kept, not copied) | Pocock's own block, written once; the port adds its own `## The orchestration core` import section beside it, never a second `## Agent skills` |
| the port, then Pocock's setup | the port's copy of home's answers (GitHub, default labels, single-context); a later Pocock run rewrites them from the target's real answers | the port's `CLAUDE.md.stub` already carries the block in Pocock's own form, so Pocock's setup finds it and updates it in place (`SKILL.md`'s own "if an `## Agent skills` block already exists ... update its contents in-place") -- never a second heading |
| the port only, no Pocock's setup ever run | the port's copy stands as the target's answers until edited by hand | present once, from the stub |

**GitLab and local-markdown issue trackers are not supported by the port.** The core (`brain`,
`ORCH`, the dispatcher, `docs/agents/issue-tracker.md`'s own conventions) is written for GitHub
only; `check_target_ready` (`port.py:152-169`) itself requires a GitHub remote naming `--repo`.

## The port command

    python -m tools.agentops port --target <path> --repo <owner/name> [--brain-title "<title>"]
        [--update-rule word|auto] [--replace]

Run at the home, in order; a failed step prints one line and exits before writing anything:

1. **The home is clean.** The home's own `core check` must pass.
2. **The machine is ready.** The mattpocock-skills plugin (verified with 1.3.1) is enabled and ships every skill the
   core names (checked by `port.py`), and `gh` is 2.101 or newer. The core block and the core role files call seven of
   the plugin's skills by name (`tdd`, `diagnosing-bugs`, `code-review`, `writing-for-agents`, `domain-modeling`,
   `codebase-design`, `research`); a target without the plugin has no step it can follow.
3. **The target is ready.** A git repo whose remote names `--repo`; a target that fails this stops the port before anything is written.
4. **No clash.** A manifest file already at the target with other content stops the port and is
   listed; `--replace` overwrites it. The three files Pocock's own setup writes
   (`TARGET_OWNED_ENTRIES` above) never clash, `--replace` included: they are the target's own.
5. **Copy.** Every manifest file, byte for byte, except a `TARGET_OWNED_ENTRIES` file already
   present at the target, which is kept exactly as it is and reported as kept, not copied.
6. **The target's own files**, written only when missing: `config/orchestration.json` (the
   target's repo, its brain chat title, its own paths, `local_modules: []`, and `home` /
   `core_version` naming what was copied), a `CLAUDE.md` stub importing the core block (or the
   one import line added to an existing `CLAUDE.md`), a `CODING_STANDARDS.md` (part 1 the core's rule
   lines, part 2 empty; an existing file kept byte for byte), `.claude/settings.json`'s hook
   registrations and its `enabledPlugins` entry for the plugin under the marketplace the machine enables
   (added to an existing file, no other key touched; an entry the target already names, `true` or
   `false`, is kept as it stands), and a stub for every path a core file cites that is not itself
   a manifest file, so nothing it points at is missing. The port then commits what it wrote: exactly those
   paths, in one commit on the target's current branch. A written path the owner had already edited without
   committing stays uncommitted and unstaged, and the report line lists it; the owner's other changes stay as they were.
   Before the commit the port installs the monitor (`install_monitor` in `port.py`): the same copy `mods install`
   makes, into the machine folder named by the target's plugin id, plus its slash-command file, plus the folder
   appended to the owner's `env.CLAUDE_CODE_PLUGIN_DIRS`.
7. **Labels.** The 19 labels named in `core_labels.json` (the five triage labels, `bug`,
   `enhancement`, the five `wayfinder:*` labels, `autopilot`, `autopilot-stuck`, `parked`, `provisional`, `flow-fix`, `priority`, `hard-push`), created only where the target does not already
   have them.
8. **One report line**: files copied, own files written, labels created and already there, and
   the next step.

`--target` is refused when it resolves to the home itself.

## A new target in one command

    python -m tools.agentops target add --folder <path> --brain-title "<title>" [--update-rule auto|word] [--dry-run]

Run at the home. It prints one line per step, `step N <name>: <result>`, and the first failing step stops it
(its line says `stopped: <reason>`, the route, exit 1; nothing later is touched):

1. **target repo**: the folder is a git repo whose `origin` names a GitHub `owner/name`; an uncommitted tree is reported, never committed.
2. **machine ready**: the port's machine check (the plugin enabled, `gh` 2.101 or newer).
3. **port dry run**: the port's dry run; a clash of manifest files stops here and the deployment ticket goes `needs-info` to the brain; `--replace` is never used. Any other stop is a core issue, written with `core issue`, blocking the deployment ticket.
4. **port**: the port for real; skipped under `--dry-run` (as are steps 6 and 7).
5. **target-entry**: `{repo, folder}` appended to the home's `targets`, queued through `agentops commit --detach` (the path is stored resolved, with forward slashes); it stops when the home's `config/orchestration.json` already has uncommitted changes; under `--dry-run` it prints "would add".
6. **core status**: in the target, the count of core changes waiting.
7. **dispatch**: in the target, `dispatch --ensure`, which must print `live pid <pid> commit <sha8>` (or `started pid <pid>`).

**Once per machine, not per target**: the permission guard and the bypass acceptance (see "The permission guard
on a new machine" above), `gh` 2.101 or newer, and the mattpocock-skills plugin.

**The owner's own list**, printed at the end of a run that gets through (fixed text, the chat's title filled in):

1. run `/setup-matt-pocock-skills` once in the chat "<title>" there;
2. set that chat to the brain's model of the roles table (`docs/agents/core-block.md`, "Roles");
3. answer the brain's areas table there.

The list has no monitor step: the port installs the monitor and registers it itself (step 6). The one check is
that the monitor pane opens in the new repo's chat.

**Lessons**, one line each, every stop a real port met (a stop a later port meets adds its own line here, in its fix ticket):

- The plugin's marketplace name is read from the machine, never a fixed name (met by the Hermes port).
- The bypass acceptance and the guard install are once per machine, not per target.
- A manifest file already present at the target with other content stops the port and is listed; `--replace` is never used by the command.
- The three Pocock files (issue tracker, triage labels, domain docs) never clash: kept where present, copied where missing.
- The port installs the target's monitor mod into `~/.claude/mods/<plugin id>` (the manifest `name` and the atoms' plugin id become the target's id) and appends that folder to the owner's `env.CLAUDE_CODE_PLUGIN_DIRS` itself, a JSON edit of that one key (no change when listed); a folder another repo holds is refused, nothing written. `target add --dry-run` prints `would install the monitor at <folder> and register it` (met by the Hermes port, which printed a line for the owner and installed nothing).
- An uncommitted tree of the owner in the target is reported at step 1, never committed (met by the Hermes port).
- After a port into a target whose tree the owner keeps uncommitted, the home's auto `core update` there is refused ("uncommitted change(s) at .claude/settings.json, config/orchestration.json") until the owner or the target's brain commits the ported files; the system never commits in the owner's repo (met by the Hermes port).

## The first brain chat of a target

The port command writes the target's files and syncs its labels; it never opens a chat (step 8's
own report line names this as the next step, its own title given by `--brain-title` or the
default `Brain: <repo name>`, `build_orchestration_settings`). Nothing in Claude Code sets a new
chat's role, model, effort or Remote Control from a title alone: a plain chat opened and renamed
to that title is, to the harness, still a plain chat. Every step below is one a human can do with
what the app or the CLI shows; the exact model and effort are the core's own row for `brain`
(`docs/agents/core-block.md`, "Roles"): Fable 5.1 high, fallback Opus 5.5 xhigh.

**CLI** (every flag below is quoted from `claude --help` of Claude Code 2.1.280):

1. In the target repo's own folder:
   `claude --agent brain --model fable --effort high --name "<brain_chat_title>" --remote-control "<brain_chat_title>"`
   -- the title is `config/orchestration.json`'s `brain_chat_title`, written by the port.
   `--agent <agent>`: "Agent for the current session"; it loads `.claude/agents/brain.md`, whose own
   frontmatter already says `model: fable`. `--model <model>`: "an alias for the latest model (e.g.
   'fable', 'opus', or 'sonnet')". `--effort <level>`: "(low, medium, high, xhigh, max)".
   `-n, --name <name>`: "Set a display name for this session". `--remote-control [name]`: "Start an
   interactive session with Remote Control enabled (optionally named)".
2. When Fable's limit is out, the core's fallback: the same line with `--model opus --effort xhigh`.

**Desktop app**: opening a chat with a role, model, effort and name preset (rather than typing them
after the fact) is not a step this session can source — no desktop-app guide was available to
read here. The chat opened this way is a plain chat: the app offers no way to pick an agent for a
new chat; until it names an agent, `--agent brain` above is the sourced path. Once the chat is
open:

1. Rename it to the title above (the app's own rename control on the chat).
2. Set its model and effort to match the core's brain row, the app's own per-chat model/effort
   picker (unsourced here beyond the fact that one exists: it is where every other chat's model in
   this repo is already set; a chat left untouched keeps the app's own default model and effort
   until changed by hand).
3. Turn Remote Control on for this chat (the app's own per-chat toggle, `docs/agents/core-block.md`
   "Roles": "the only one with Remote Control").
4. Send its first message: an issue number labelled `ready-for-agent`, or a request the brain
   turns into one (`docs/agents/brain.md`).

In that first chat the brain's `core status` also says that no areas are set (the port writes the
setting `areas` empty and creates no `area:` label): the brain proposes the target's areas from
`python -m tools.agentops areas propose` in one table, and creates and sorts them only after the
owner's yes (`docs/agents/brain.md`, "Checks the core").

**The first-prompt brain-title check.** Even when the chat is opened plain (either path above,
before its role, model or effort are set), `tools/hooks/brain_reset_nudge.py`'s existing
`UserPromptSubmit` hook reads the chat's own title from its transcript (the `custom-title` record
the app writes on a rename, the same record `scripts/token_meter.py`'s `parse_main_transcript`
already reads to classify a chat by title) and, the first time it sees no earlier assistant turn
in that transcript, checks it against `brain_chat_title` (`config/orchestration.json`, read by
`tools/agentops/core_settings.py`, the same way this hook already reads `context_line`). A title
match injects, once, that this chat is the brain and to read `docs/agents/brain.md`; it also names
the core's own brain row (`docs/agents/core-block.md`) next to the model this transcript's own
`model` attachment record names, so a chat left at the app's default model is told so on its very
first prompt rather than only after the owner notices. A hook cannot read a chat's effort from any
Claude Code payload or transcript record (none exists in the shapes this check reads): the note
only compares the model by name, and fires whenever it is not the primary one (Fable 5.1), since
the fallback model alone (Opus 5.5) does not prove the required `xhigh`.

**Live check, left for the brain** (this session wrote nothing and started no session in the test
target repo, per its own instructions): open a chat there, follow the CLI steps
above (or the app steps, whichever is available), then read the session back with
`mcp__ccd_session_mgmt__get_session` (`list_sessions` then `get_session`) and confirm its `model`,
`effort` and Remote Control state match this section's numbers.

## Keeping the dispatcher alive

`keepalive install` registers a scheduled host task that runs `dispatch --ensure` on a short
interval and at logon, so the dispatcher comes back on its own after a reboot, a crash or a logoff.
It is not run by the port command itself: it is a step of a port, run at the target only on the
owner's own word, after the port and the first dispatcher start.

Without administrator rights (measured with two real, non-elevated `schtasks` probe calls: a task
with only a clock trigger registers, one with only a logon trigger is refused, "Access is denied")
the task still covers a crash or a logoff: the clock trigger alone brings the dispatcher back
within the 5-minute interval regardless of why it stopped. What it does not cover without an
administrator's install is the start at logon itself -- after a reboot the dispatcher waits for
that same clock trigger to fire rather than starting the moment the owner logs in.

## The permission guard on a new machine

Before `python -m tools.agentops guard install`, the owner runs `claude --dangerously-skip-permissions` once
in a terminal and accepts Claude Code's bypass disclaimer. Without that, `claude --bg` in `bypassPermissions`
refuses. Claude Code records the acceptance in `~/.claude.json` (`bypassPermissionsModeAccepted`); our code
never writes it. `guard check`'s 5th line shows whether it is accepted.

## The update rule

Set per target in its own `config/orchestration.json` (`update_rule`): `word` takes a change of
the core only when the target's own operator says so; `auto` takes every change the moment it is
available, before any work of its own starts (the dispatcher applies it once, before its first
tick); `home` means this is the home itself, and both commands below change nothing. Either way an
update takes the core from the home's own GitHub repo, never from a local folder, so it works the
same regardless of where the target sits.

The private home repo is read with the machine's own `gh` login; a target on another machine
needs a login of its own that can read the home, or `core status` / `core update` fail on the read.

    python -m tools.agentops core status   # how many core changes wait at the home, 0 files touched
    python -m tools.agentops core update   # rewrites the manifest's files to the home's version

`core update` refuses, changing nothing, while an ORCH session is live in the target. Neither
command touches a file outside the manifest but the two cases named below; the target's own files (`CLAUDE.md`,
`.claude/settings.json`, `config/orchestration.json`'s `core_version`, `core_version_before` and `core_version_count` apart) are left exactly as
they were. Two files are written for a target that lacks them: `CODING_STANDARDS.md`, rendered from the
stub, and `config/core_leak_patterns.json`, a starter holding only `L3` (the target's folder path and
repo name); an existing one is kept byte for byte, and the starter stays the target's own file, not in the manifest.
The target's monitor `mods/<its folder>/hooks/project.ts` (its own constants, written once by the port, not in the
manifest) gets an append and no other change: each export the updated hooks import from `./project` that the file
lacks is added from the stub with the target's values, in the update's own commit; every existing line stays byte for
byte. `mods install` refuses a source whose hooks import a name its `project.ts` does not export.
Test copies are removed to a fixed point against the new manifest: a target's `tests/test_*.py` goes when it
imports only core modules, is not in the manifest and has a same-named test at the home (the home holds that
test); a test whose only import outside the manifest is another test file being removed (its helper) goes with
it. One with no home copy stays and the update names it (`kept (no home copy)`); a test that imports such a
kept file keeps it too, so no kept test loses its helper.

After every home merge the runner runs `core update` in every target listed under the home's `targets`.
A target with a live ORCH defers it to that ORCH's next start. A target that fails gets one line in the
merge's comment (line `targets:`), in its area's brain chat and in its own `dispatch.out`, and takes the
change at its next update; a failed commit leaves the target's tree as it found it.

## What the core imports outside the standard library

None. Every module under `tools/agentops/` and every script the manifest names is standard
library only, so a target's copy runs without installing anything beyond a Python interpreter,
`git` and `gh`.

## What was measured in the test repo

- `.claude/rules/` (if the target ever adds one) reaches a subagent the way `CLAUDE.md` does, not
  only the top-level session: a `researcher-find` on Haiku and a `worker` on Sonnet 5 both
  answered with a codeword that stood only in `.claude/rules/probe.md`.
- A session without the plugin enabled does not silently no-op a role file's skill call: it shows
  the error `Unknown skill: mattpocock-skills:grilling`. This is why the port itself now names the
  plugin in the target's settings (step 6): a target that does so lists all 11 of the plugin's
  skills to a headless session with no user settings of its own; a target whose settings hold only
  the hooks lists 0.
