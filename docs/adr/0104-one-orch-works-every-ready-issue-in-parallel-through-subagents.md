# 2026-09-26 D104. One ORCH works every ready issue in parallel through its subagents

Status: Accepted; amended by D104 amd 1, D110

finishing; `main` has several mergers, one merge at a time) and by D104 amd 1 below (an issue is cut into units,
"one subagent per issue" no longer holds)

The owner's grill of the orchestration (`docs/research/2026-09-26-orchestration-v2-grill.md`, Q14 T* and Q22,
repeated by the owner at 20:00 the same day): exactly one ORCH session exists at a time -- more than one ORCH is
chaos -- and it works every ready issue at once, one subagent per issue, merging each itself, so `main` has one merger and
no merge lock. No cap on how many (the owner, 20:20: "чем больше тем лучше"; it does not change the tokens per
issue): only the machine and the API rate limit bound it (the grill's N = 3 was a starting value). The owner talks only to the brain, never to ORCH. ORCH's context threshold is T = B + N x X (B its
context at start, X what one issue in progress adds, N the issues it holds), measured by `scripts/token_meter.py`, not guessed; over T it
takes nothing new, finishes or parks what it holds and ends, and the dispatcher starts a fresh one.

Rejected: one issue per ORCH context (what #45 and #50 built from the grill's "clear at every issue boundary",
which dropped Q22 and cost the parallelism the orchestration was for), and several ORCH sessions side by side (the
first spec of #60). Implemented by #60.

## D104 amd 1 (2026-09-28): an issue is cut into units, the independent ones run side by side

"One subagent per issue" is replaced. Before any code of an issue is delegated, ORCH cuts the issue into units: one
outcome each, with a test command of its own and its own places to read, small enough that a `worker` ends it under
its context line with no hand-back. Every unit that needs no other unit's result starts in the same turn, each
worker in its own worktree; a unit waits only for the units whose result it needs. The units' branches meet in one
branch of the issue, and the issue goes into `main` by one merge. An issue that is one such unit stays whole and
the cut says so. An issue of two or more units is never handed whole to any model; `worker-strong` takes only a
unit of subtle core logic that cannot be cut further. The rule binds ORCH with every worker model.

The owner, 2026-09-28 (verbatim on #254): "Орк должен расщеплять таски так чтобы их можно было по оотдельности
давать соннет/дипсик моделям простым. А не так что он выписывает одну сложную таску а опус дальше сам ее пытается
решить"; "Оркестратор должен резать таски и параллелить их!!!! Это его задача!!!!".

The numbers behind it, over the 42 dispatcher-started ORCH sessions of 2026-09-26 to 2026-09-28 09:52:
`worker-strong` 102 turns per spawn at $8.92, `worker` 62 at $1.64; 87.6 % of a worker's cost after its first edit
falls on turns above 100k of context (#168); 5 workers of one night handed their work back at their context line
(#220's review).

Rejected: units of one issue run one after another, each started after the unit before it is merged (the rule of
the mini A/B's arm D, #260): it makes the tasks easier and the issue slower, and the owner named the side-by-side
run as the point of the cut. Implemented by #254; read over the next 20 closed issues after its merge.
