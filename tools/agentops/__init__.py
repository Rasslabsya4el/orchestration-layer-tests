"""agentops: the opencode-server bridge (issue #46).

Ported from the owner's separate plugin repo (`orchlib/ocserver.py`, the
opencode-driving part of `orchlib/runner.py`), whose own location is one of
the paths in `config/orchestration.json`.
See `docs/agents/PORTING.md` for what a copy into another repo needs.

Self-contained on purpose: everything another repo needs to copy lives under
this one folder (`tools/agentops/`), with no import back into the product's
own package under `src/`.
"""
