"""tools/replays/gh_stub.py: a recording stand-in for the `gh` CLI inside a replay run (issue #349).

``handle(argv, state_dir, moment_iso, repo, real_gh)`` answers one `gh` call (argv without the leading ``gh``).
It fails closed: only an explicit allowlist of reads reaches the real gh; every other call is a write.

* READS pass through to ``real_gh``: the commands in ``_READ_COMMANDS``, ``--version``, ``api`` with GET (or no
  field) on a REST path, and ``api graphql`` whose query has no ``mutation``. Issues created after the moment are
  hidden from ``issue list`` / ``issue status`` / ``issue view``, from ``search issues`` / ``search prs`` and from
  ``api`` results on ``repos/<o>/<r>/issues`` and ``search/issues`` (any repo, from ``-R`` or ``--repo`` or the
  repo of the run); comments created after the moment are hidden from the issue they sit on. A ``gh issue view`` of
  an issue the stub created answers from the recorded create. A filtered read the stub cannot filter exactly
  (``--jq`` / ``--template``, a plain ``issue view --comments``, an answer that is not the JSON it asked for) is
  refused with rc 1 and a workaround, not shown unfiltered. A body edited after the moment (an issue's, or a
  comment's, in ``issue view --json`` / ``issue list --json`` / ``search issues --json`` and in ``api`` issue and
  comment objects) is served as it stood then, from GraphQL ``userContentEdits`` read through ``real_gh`` (#359);
  with no version at or before the moment the read is refused, and a plain ``issue view`` of such an issue is refused
  with the ``--json body,comments`` advice. A direct ``api graphql`` query passes through unchanged, as do
  ``pr view`` / ``pr list``: the stub does not rewrite a body there.
* WRITES are everything else: any other verb, ``api`` with a method other than GET (or a field without an explicit
  GET), a graphql mutation. Each is appended in full to ``<state_dir>/writes.jsonl`` (argv, UTC time, body, every api
  field value) and answered with rc 0 and a plausible success: ``issue create``, ``pr create`` and an api POST to the
  issues path print a URL numbered from 9001 (the number kept in ``<state_dir>/counter``, shared by issues and PRs);
  the others print the issue or PR URL of the first positional after the subcommand, or an empty line.

``main()`` takes the same from the environment (``REPLAY_STATE_DIR``, ``REPLAY_MOMENT`` (ISO UTC), ``REPLAY_REPO``,
``REPLAY_REAL_GH``, the path of the real gh) and ``sys.argv[1:]``, so a ``gh.cmd`` shim can call
``python -m tools.replays.gh_stub <args>``.

Standard library only.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

RealGh = Callable[[list[str]], tuple[int, str, str]]

#: The first number a stub-created issue or PR gets.
FIRST_NUMBER = 9001
#: The reads that pass through to the real gh, by (group, subcommand). Anything else (and not an api read) is a write.
_READ_COMMANDS = frozenset({
    ("pr", "list"), ("pr", "view"), ("pr", "status"), ("pr", "diff"), ("pr", "checks"),
    ("search", "code"), ("search", "repos"), ("search", "commits"),
    ("repo", "view"), ("repo", "list"),
    ("label", "list"),
    ("release", "list"), ("release", "view"),
    ("run", "list"), ("run", "view"),
    ("workflow", "list"), ("workflow", "view"),
    ("auth", "status"),
})
#: The reads whose issues (or PRs, for search prs) created after the moment are hidden.
_FILTERED_READS = frozenset({
    ("issue", "list"), ("issue", "status"), ("search", "issues"), ("search", "prs"),
})
#: Subcommand aliases: `gh issue ls` is `gh issue list`.
_ALIASES = {"ls": "list"}
#: Value-taking flags of the gh commands the stub reads or writes (alias -> canonical name).
_GH_VALUE_FLAGS = {
    "-R": "repo", "--repo": "repo",
    "--json": "json", "-q": "jq", "--jq": "jq", "-t": "template", "--template": "template",
    "-b": "body", "--body": "body", "-F": "body-file", "--body-file": "body-file",
    "--title": "title", "-L": "limit", "--limit": "limit", "-s": "state", "--state": "state",
    "-S": "search", "--search": "search", "-l": "label", "--label": "label",
    "--add-label": "add-label", "--remove-label": "remove-label",
    "--add-assignee": "add-assignee", "--remove-assignee": "remove-assignee",
    "--add-project": "add-project", "--remove-project": "remove-project", "--reason": "reason",
    "-a": "assignee", "--assignee": "assignee", "-A": "author", "--author": "author",
    "-m": "milestone", "--milestone": "milestone", "-r": "reviewer", "--reviewer": "reviewer",
    "-B": "base", "--base": "base", "-H": "head", "--head": "head",
}
#: Value-taking flags of `gh api` (alias -> canonical name). ``-f`` / ``--raw-field`` send the value as text;
#: ``-F`` / ``--field`` read ``@file`` values. A field or ``--input`` without an explicit GET makes a POST.
_API_VALUE_FLAGS = {
    "-X": "method", "--method": "method",
    "-f": "field", "--raw-field": "field",
    "-F": "typed-field", "--field": "typed-field",
    "--input": "input", "-H": "header", "--header": "header",
    "-q": "jq", "--jq": "jq", "-t": "template", "--template": "template",
    "-p": "preview", "--preview": "preview", "--hostname": "hostname", "--cache": "cache",
}
_API_TABLE = {**_API_VALUE_FLAGS, "-R": "repo", "--repo": "repo"}
_API_BODY_FIELDS = ("field", "typed-field", "input")
_API_PREFIX = "https://api.github.com/"
_FILTERED_API_RE = re.compile(r"(?:search/issues|repos/[^/]+/[^/]+/issues)(?:/|$)")
_MUTATION_RE = re.compile(r"\bmutation\b", re.IGNORECASE)
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
#: A `#N` or `owner/repo#N` at a line's start (after its indent), or `N<tab>`: the plain columns of list and status.
_LINE_NUMBER_RE = re.compile(r"^(?:(?:[\w.-]+/[\w.-]+)?#(\d+)\b|(\d+)\t)")
#: What `_load` returns for text that is not JSON (JSON `null` is a value, not this).
_INVALID = object()


def handle(argv: list[str], state_dir: Path, moment_iso: str, repo: str, real_gh: RealGh) -> tuple[int, str, str]:
    """Answer one gh call; return (rc, stdout, stderr). ``argv`` is the gh arguments without ``gh``."""
    args = list(argv)
    state = Path(state_dir)
    moment = _parse_time(moment_iso)
    if args == ["--version"]:
        return real_gh(args)
    pairs, pos = _parse(args, _GH_VALUE_FLAGS)
    group = pos[0] if pos else ""
    if group == "api":
        api_pairs, api_pos = _parse(args, _API_TABLE)
        endpoint = api_pos[1] if len(api_pos) > 1 else ""
        target = _last(api_pairs, "repo") or repo
        if _is_api_read(api_pairs, endpoint, target):
            return _api_read(args, api_pairs, endpoint, moment, target, real_gh)
        return _api_write(args, api_pairs, endpoint, state, target)
    sub = _ALIASES.get(pos[1], pos[1]) if len(pos) > 1 else ""
    head = (group, sub)
    if head == ("issue", "view"):
        return _view(args, pairs, pos, moment, state, real_gh, _last(pairs, "repo") or repo)
    if head in _FILTERED_READS:
        return _listing(args, pairs, moment, real_gh, _last(pairs, "repo") or repo, head[0] == "search")
    if head in _READ_COMMANDS:
        return real_gh(args)
    return _write(args, pairs, pos, state, repo)


#: The gh groups whose commands take ``-R`` / ``--repo`` and otherwise infer the repo from the cwd's git remote.
_REPO_FLAG_GROUPS = frozenset({"issue", "pr", "label", "release", "run", "workflow"})


def pin_repo(args: list[str], repo: str) -> list[str]:
    """``args`` for the real gh with the run's repo named when the call names none (#357): a replay run tree is a sealed
    clone without a remote, so the real gh cannot infer a repo from it. ``-R <repo>`` is added to the groups that take
    it; ``repo view`` gets the repo as its positional. Calls that already name a repo, and groups with no such flag
    (``api``, ``search``, ``auth``), are returned unchanged."""
    _, pos = _parse(args, _GH_VALUE_FLAGS)
    group = pos[0] if pos else ""
    if any(a in ("-R", "--repo") or a.startswith(("--repo=", "-R")) for a in args):
        return list(args)
    if group in _REPO_FLAG_GROUPS:
        return [*args, "-R", repo]
    if group == "repo" and len(pos) == 2 and pos[1] == "view":
        i = args.index("view")
        return [*args[:i + 1], repo, *args[i + 1:]]
    return list(args)


def real_gh_runner(real: str, repo: str) -> RealGh:
    """The ``RealGh`` of a run: the real gh with the repo pinned (``pin_repo``) and ``GH_REPO`` set, which covers the
    commands without a repo flag (``api`` placeholders ``{owner}`` / ``{repo}``)."""
    def real_gh(args: list[str]) -> tuple[int, str, str]:
        proc = subprocess.run([real, *pin_repo(args, repo)], capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=120, creationflags=_NO_WINDOW, env={**os.environ, "GH_REPO": repo})
        return proc.returncode, proc.stdout, proc.stderr

    return real_gh


def main() -> int:
    """The shim's entry point: the environment says where the state is, which moment, which repo, which real gh."""
    env = os.environ
    rc, out, err = handle(sys.argv[1:], Path(env["REPLAY_STATE_DIR"]), env["REPLAY_MOMENT"], env["REPLAY_REPO"],
                          real_gh_runner(env["REPLAY_REAL_GH"], env["REPLAY_REPO"]))
    sys.stdout.buffer.write(out.encode("utf-8", "replace"))
    sys.stdout.flush()
    sys.stderr.write(err)
    return rc


# ---- writes -------------------------------------------------------------------------------------------------------

def _write(args: list[str], pairs, pos: list[str], state: Path, repo: str) -> tuple[int, str, str]:
    group = pos[0] if pos else ""
    sub = pos[1] if len(pos) > 1 else ""
    target = _last(pairs, "repo") or repo
    try:
        body = _body_text(pairs)
    except (OSError, ValueError) as exc:
        return _unreadable(exc)
    if group in ("issue", "pr") and sub == "create":
        number = _next_number(state)
        url = _url(target, group, number)
        created = {"number": number, "title": _last(pairs, "title"), "body": body, "url": url} \
            if group == "issue" else None
        _record(state, args, body, [], created)
        return 0, url + "\n", ""
    _record(state, args, body, [])
    number = _number(pos[2]) if len(pos) > 2 else None
    if group not in ("issue", "pr") or number is None:
        return 0, "\n", ""
    return 0, _url(target, group, number) + "\n", ""


def _api_write(args: list[str], pairs, endpoint: str, state: Path, target: str) -> tuple[int, str, str]:
    method = (_last(pairs, "method") or "").upper() or "POST"
    try:
        fields = _api_fields(pairs)
        body = _api_body(pairs, fields)
    except (OSError, ValueError) as exc:
        return _unreadable(exc)
    if method == "POST" and _api_path(endpoint, target) == f"repos/{target}/issues":
        number = _next_number(state)
        url = _url(target, "issue", number)
        created = {"number": number, "title": _field(fields, "title"), "body": body, "url": url}
        _record(state, args, body, fields, created)
        return 0, json.dumps({"number": number, "html_url": url}) + "\n", ""
    _record(state, args, body, fields)
    return 0, "{}\n", ""


def _api_fields(pairs) -> list[dict]:
    """Every -f / -F / --field value of an api call, in order; a `-F key=@file` value is the file's text."""
    fields = []
    for name, value in pairs:
        if name in ("field", "typed-field"):
            key, _, text = value.partition("=")
            if name == "typed-field" and text.startswith("@"):
                text = _read_body(text[1:])
            fields.append({"name": key, "value": text})
    return fields


def _field(fields: list[dict], name: str) -> str | None:
    values = [field["value"] for field in fields if field["name"] == name]
    return values[-1] if values else None


def _api_body(pairs, fields: list[dict]) -> str | None:
    """The request body: the --input file when given, else the last `body` field."""
    source = _last(pairs, "input")
    if source is not None:
        return _read_body(source)
    return _field(fields, "body")


def _body_text(pairs) -> str | None:
    body_file = _last(pairs, "body-file")
    if body_file is not None:
        return _read_body(body_file)
    return _last(pairs, "body")


def _read_body(path: str) -> str:
    """A body file's text, read at call time as bytes and decoded as UTF-8 (CRLF kept); ``-`` is stdin."""
    if path == "-":
        return sys.stdin.buffer.read().decode("utf-8")
    return Path(path).read_bytes().decode("utf-8")


def _unreadable(exc: Exception) -> tuple[int, str, str]:
    return 1, "", f"gh (replay stub): cannot read a body file: {exc}\n"


def _record(state: Path, args: list[str], body: str | None, fields: list[dict], created: dict | None = None) -> None:
    state.mkdir(parents=True, exist_ok=True)
    at = _utc_now()
    line = {"argv": list(args), "at": at, "body": body, "fields": fields}
    if created is not None:
        line["created"] = {**created, "createdAt": at}
    with (state / "writes.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(line, ensure_ascii=False) + "\n")


def _next_number(state: Path) -> int:
    state.mkdir(parents=True, exist_ok=True)
    path = state / "counter"
    last = int(path.read_text(encoding="utf-8").strip()) if path.exists() else FIRST_NUMBER - 1
    path.write_text(str(last + 1), encoding="utf-8")
    return last + 1


def _created_issue(state: Path, number: int) -> dict | None:
    """The recorded create of an issue the stub handed out under this number, or None."""
    path = state / "writes.jsonl"
    if not path.exists():
        return None
    found = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        created = json.loads(line).get("created")
        if isinstance(created, dict) and created.get("number") == number:
            found = created
    return found


# ---- reads --------------------------------------------------------------------------------------------------------

def _listing(args: list[str], pairs, moment: datetime, real_gh: RealGh, target: str, search: bool
             ) -> tuple[int, str, str]:
    """issue list / issue status / search issues / search prs: the answer without the issues created after the moment."""
    refusal = _formatted_refusal(pairs, "a filtered listing", "drop the flag and ask for --json with the fields you "
                                                              "need (the stub filters the JSON), then apply it yourself")
    if refusal:
        return refusal
    fields = _last(pairs, "json")
    if fields is not None:
        names = fields.split(",")
        extras = ["createdAt", *(["updatedAt", "number", *(["repository"] if search else [])] if "body" in names else [])]
        new_args, added = _request(args, names, extras)
        rc, out, err = real_gh(new_args)
        if rc != 0 or not out.strip():
            return rc, out, err
        restore = (lambda rows: _restore_rows(rows, moment, real_gh, target)) if "body" in names else None
        try:
            visible = _visible_part(_load(out), moment, added, restore)
        except _Refused as exc:
            return _refuse(str(exc))
        if visible is None:
            return _refuse("a listing answer that is not the JSON it asked for")
        return 0, json.dumps(visible) + "\n", err
    probe_rc, probe_out, probe_err = real_gh(list(args) + ["--json", "number,createdAt"])
    if probe_rc != 0:
        return probe_rc, probe_out, probe_err
    rows = _all_rows(_load(probe_out) if probe_out.strip() else [])
    if rows is None:
        return _refuse("a listing probe that is not the JSON it asked for")
    hidden = {int(row["number"]) for row in rows if "number" in row and _hidden_item(row, "createdAt", moment)}
    rc, out, err = real_gh(args)
    if rc != 0:
        return rc, out, err
    kept_lines = [line for line in out.splitlines(keepends=True) if _line_number(line) not in hidden]
    return 0, "".join(kept_lines), err


def _view(args: list[str], pairs, pos: list[str], moment: datetime, state: Path, real_gh: RealGh, target: str
          ) -> tuple[int, str, str]:
    number = _number(pos[2]) if len(pos) > 2 else None
    if number is None:
        return real_gh(args)
    fields = _last(pairs, "json")
    refusal = _formatted_refusal(pairs, "a filtered view", "drop the flag and ask for --json with the fields you need "
                                                          "(the stub filters the JSON), then apply it yourself")
    if refusal:
        return refusal
    created = _created_issue(state, number)
    if created is not None:
        return _stub_view(created, fields)
    repo_flag = ["-R", _last(pairs, "repo")] if _last(pairs, "repo") else []
    if fields is None:
        if "--comments" in args or "-c" in args:
            return _refuse("a plain issue view with --comments; use --json comments, which the stub filters")
        probe_rc, probe_out, probe_err = real_gh(["issue", "view", str(number), "--json", "createdAt,updatedAt",
                                                  *repo_flag])
        if probe_rc != 0:
            return probe_rc, probe_out, probe_err
        probe = _load(probe_out)
        if not isinstance(probe, dict):
            return _refuse("an issue probe that is not the JSON it asked for")
        if _created_after(probe.get("createdAt"), moment):
            return _not_found(number)
        if _edited_after(probe.get("updatedAt"), moment) and _body_changed_since(number, target, moment, real_gh):
            return _refuse(f"a plain issue view of #{number}, whose body was edited after the moment; use "
                           f"--json body,comments, which the stub serves as it stood at the moment")
        return real_gh(args)
    names = fields.split(",")
    extras = ["createdAt", *(["updatedAt"] if {"body", "comments"} & set(names) else [])]
    new_args, added = _request(args, names, extras)
    rc, out, err = real_gh(new_args)
    if rc != 0:
        return rc, out, err
    row = _load(out)
    if not isinstance(row, dict):
        return _refuse("an issue answer that is not the JSON it asked for")
    if _created_after(row.get("createdAt"), moment):
        return _not_found(number)
    if isinstance(row.get("comments"), list):
        row["comments"] = [c for c in row["comments"] if not _hidden_item(c, "createdAt", moment)]
    try:
        if _edited_after(row.get("updatedAt"), moment):
            refs = [_Ref(row, target, number)] if "body" in names else []
            if isinstance(row.get("comments"), list):
                refs += [_comment_ref(c, target, number) for c in row["comments"]]
            _restore(refs, moment, real_gh)
    except _Refused as exc:
        return _refuse(str(exc))
    return 0, json.dumps(_strip(row, added)) + "\n", err


def _stub_view(created: dict, fields: str | None) -> tuple[int, str, str]:
    """The view of an issue the stub created: built from the recorded create, never from the real gh."""
    row = {"number": created["number"], "title": created["title"], "body": created["body"], "url": created["url"],
           "state": "OPEN", "createdAt": created["createdAt"], "comments": []}
    if fields is None:
        return 0, f"title:\t{row['title']}\nstate:\tOPEN\nurl:\t{row['url']}\n\n{row['body'] or ''}\n", ""
    return 0, json.dumps({name: row[name] for name in fields.split(",") if name in row}) + "\n", ""


def _api_read(args: list[str], pairs, endpoint: str, moment: datetime, target: str, real_gh: RealGh
              ) -> tuple[int, str, str]:
    path = _api_path(endpoint, target)
    if not _is_filtered_api_path(path):
        return real_gh(args)
    refusal = _formatted_refusal(pairs, "a filtered api read",
                                 "drop the flag and read the JSON answer, which the stub filters")
    if refusal:
        return refusal
    rc, out, err = real_gh(args)
    if rc != 0 or not out.strip():
        return rc, out, err
    docs = _json_docs(out)
    if docs is None:
        return _refuse("an api answer that is not JSON")
    kept = []
    for doc in docs:
        if isinstance(doc, list):
            kept.append([item for item in doc if not _rest_hidden(item, moment)])
        elif isinstance(doc, dict) and isinstance(doc.get("items"), list):
            kept.append({**doc, "items": [item for item in doc["items"] if not _rest_hidden(item, moment)]})
        elif _rest_hidden(doc, moment):
            return 1, "", "gh: Not Found (HTTP 404)\n"
        else:
            kept.append(doc)
    try:
        _restore_rest([item for doc in kept for item in _rest_objects(doc)], moment, real_gh, target)
    except _Refused as exc:
        return _refuse(str(exc))
    return 0, "".join(json.dumps(doc) + "\n" for doc in kept), err


def _is_api_read(pairs, endpoint: str, target: str) -> bool:
    """A GET on a REST path, or a graphql query with no mutation; everything else is a write."""
    if _api_path(endpoint, target) == "graphql":
        return _is_query_read(pairs)
    method = (_last(pairs, "method") or "").upper()
    if method:
        return method == "GET"
    return not any(name in _API_BODY_FIELDS for name, _ in pairs)


def _is_query_read(pairs) -> bool:
    if _last(pairs, "input") is not None:
        return False
    try:
        queries = [field["value"] for field in _api_fields(pairs) if field["name"] == "query"]
    except (OSError, ValueError):
        return False
    return bool(queries) and not _MUTATION_RE.search(queries[-1])


def _is_filtered_api_path(path: str) -> bool:
    return _FILTERED_API_RE.match(path) is not None


def _not_found(number: int) -> tuple[int, str, str]:
    return 1, "", f"GraphQL: Could not resolve to an Issue with the number {number}.\n"


def _refuse(what: str) -> tuple[int, str, str]:
    return 1, "", f"gh (replay stub): refused, {what}\n"


def _formatted_refusal(pairs, what: str, workaround: str) -> tuple[int, str, str] | None:
    """The refusal of a filtered read whose output a --jq or --template would reshape, or None."""
    if _last(pairs, "jq") is None and _last(pairs, "template") is None:
        return None
    return _refuse(f"--jq or --template on {what}; {workaround}")


def _request(args: list[str], names: list[str], extras: list[str]) -> tuple[list[str], list[str]]:
    """The args with the ``extras`` among their --json fields, and the extras the stub added (to strip again)."""
    added = [name for name in extras if name not in names]
    return _with_json(args, names + added), added


def _strip(row: dict, added: list[str]) -> dict:
    for name in added:
        row.pop(name, None)
    return row


def _visible_part(data, moment: datetime, added: list[str], restore=None):
    """A listing's JSON without the issues created after the moment (a list, or a dict of lists such as status's
    sections); the rows are handed to ``restore`` before the stub's own fields are stripped. None when it is not such
    a listing."""
    if isinstance(data, list):
        if not all(isinstance(row, dict) for row in data):
            return None
        rows = [row for row in data if not _hidden_item(row, "createdAt", moment)]
        if restore is not None:
            restore(rows)
        return [_strip(row, added) for row in rows]
    if isinstance(data, dict):
        visible = {}
        for key, value in data.items():
            if isinstance(value, list):
                part = _visible_part(value, moment, added, restore)
                if part is None:
                    return None
                visible[key] = part
            else:
                visible[key] = value
        return visible
    return None


def _all_rows(data) -> list | None:
    """Every row of a listing (the list itself, or the lists of a dict's sections), or None when it has none."""
    if isinstance(data, list):
        lists = [data]
    elif isinstance(data, dict):
        lists = [value for value in data.values() if isinstance(value, list)]
    else:
        return None
    rows = [row for lst in lists for row in lst]
    return rows if all(isinstance(row, dict) for row in rows) else None


def _hidden_item(item, key: str, moment: datetime) -> bool:
    """A listing item (or comment) hidden from the run: not an object, or created after the moment."""
    return not isinstance(item, dict) or _created_after(item.get(key), moment)


def _rest_hidden(item, moment: datetime) -> bool:
    """A REST object hidden from the run: created after the moment; objects without a created_at stay."""
    return isinstance(item, dict) and "created_at" in item and _created_after(item["created_at"], moment)


def _api_path(endpoint: str, target: str) -> str:
    owner, _, name = target.partition("/")
    path = endpoint[len(_API_PREFIX):] if endpoint.startswith(_API_PREFIX) else endpoint
    path = path.replace("{owner}", owner).replace("{repo}", name).split("?", 1)[0]
    return path.lstrip("/")


def _json_docs(text: str) -> list | None:
    """Every JSON value of a text (`gh api --paginate` writes one per page), or None when it is not JSON."""
    decoder = json.JSONDecoder()
    docs: list = []
    index = 0
    text = text.strip()
    try:
        while index < len(text):
            doc, index = decoder.raw_decode(text, index)
            docs.append(doc)
            while index < len(text) and text[index].isspace():
                index += 1
    except ValueError:
        return None
    return docs


def _load(text: str):
    """The JSON value of a text, or ``_INVALID`` when it is not JSON (``null`` is a value)."""
    try:
        return json.loads(text)
    except ValueError:
        return _INVALID


def _with_json(args: list[str], names: list[str]) -> list[str]:
    """``args`` with its ``--json`` fields replaced by ``names`` (or ``--json`` added when there is none)."""
    out: list[str] = []
    replaced = False
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--json" and i + 1 < len(args):
            out += ["--json", ",".join(names)]
            replaced = True
            i += 2
            continue
        if arg.startswith("--json="):
            out.append("--json=" + ",".join(names))
            replaced = True
        else:
            out.append(arg)
        i += 1
    if not replaced:
        out += ["--json", ",".join(names)]
    return out


def _line_number(line: str) -> int | None:
    match = _LINE_NUMBER_RE.match(line.lstrip())
    if match is None:
        return None
    return int(match.group(1) or match.group(2))


# ---- bodies as they stood at the moment (#359) -------------------------------------------------------------------

class _Refused(Exception):
    """A body the stub cannot show as of the moment; the message completes ``refused, ...``."""


@dataclass
class _Ref:
    """A body to put back to its text at the moment: ``holder["body"]`` is an issue's (``comment_id`` None) or a
    comment's text."""
    holder: dict
    repo: str
    number: int
    comment_id: int | None = None


#: Issues per history query; first(100) edits of a body, first(100) comments with first(50) edits each.
_HISTORY_BATCH = 20
_COMMENT_URL_RE = re.compile(r"#issuecomment-(\d+)$")
_REST_ISSUE_URL_RE = re.compile(r"repos/([^/]+/[^/]+)/issues/(\d+)/?$")


def _edited_after(updated, moment: datetime) -> bool:
    """True when the issue (or PR) was touched after the moment, so a body of it may have been edited. The CLI has no
    ``lastEditedAt`` field, so ``updatedAt`` is the cheap test; a row without it (not asked, not answered) is left."""
    if not isinstance(updated, str):
        return False
    try:
        return _parse_time(updated) > moment
    except ValueError:
        return True


def _comment_ref(comment, repo: str, number: int) -> _Ref:
    match = _COMMENT_URL_RE.search(comment.get("url") or "") if isinstance(comment, dict) else None
    if match is None:
        raise _Refused(f"a comment of #{number} has no url to find its edit history by, so its text at the moment "
                       f"cannot be shown")
    return _Ref(comment, repo, number, int(match.group(1)))


def _body_changed_since(number: int, repo: str, moment: datetime, real_gh: RealGh) -> bool:
    """Whether the body of an issue differs from its text at the moment (or the history cannot be read)."""
    holder: dict = {"body": None}
    try:
        _restore([_Ref(holder, repo, number)], moment, real_gh)
    except _Refused:
        return True
    return holder["body"] is not None


def _restore_rows(rows: list, moment: datetime, real_gh: RealGh, target: str) -> None:
    """Put the rows of an issue listing that were touched after the moment back to their body at the moment."""
    refs = []
    for row in rows:
        if not _edited_after(row.get("updatedAt"), moment) or not isinstance(row.get("body"), str):
            continue
        if not isinstance(row.get("number"), int):
            raise _Refused("a listed body edited after the moment that the stub cannot trace to an issue number")
        repository = row.get("repository")
        name = repository.get("nameWithOwner") if isinstance(repository, dict) else None
        refs.append(_Ref(row, name if isinstance(name, str) and name else target, row["number"]))
    _restore(refs, moment, real_gh)


def _rest_objects(doc) -> list[dict]:
    """The REST objects of one parsed answer: a list's items, a search answer's ``items``, or the object itself."""
    if isinstance(doc, list):
        return [item for item in doc if isinstance(item, dict)]
    if isinstance(doc, dict) and isinstance(doc.get("items"), list):
        return [item for item in doc["items"] if isinstance(item, dict)]
    return [doc] if isinstance(doc, dict) else []


def _restore_rest(objects: list[dict], moment: datetime, real_gh: RealGh, target: str) -> None:
    """Put REST issue and comment objects (``body``, ``updated_at``) touched after the moment back to their text then."""
    refs = []
    for item in objects:
        if not isinstance(item.get("body"), str) or not _edited_after(item.get("updated_at"), moment):
            continue
        if "issue_url" in item and isinstance(item.get("id"), int):
            match = _REST_ISSUE_URL_RE.search(str(item["issue_url"]))
            if match is None:
                raise _Refused("a comment edited after the moment whose issue the stub cannot find")
            refs.append(_Ref(item, match.group(1), int(match.group(2)), item["id"]))
        elif isinstance(item.get("number"), int):
            match = re.search(r"repos/([^/]+/[^/]+)$", str(item.get("repository_url") or ""))
            refs.append(_Ref(item, match.group(1) if match else target, item["number"]))
        else:
            raise _Refused("a body touched after the moment that the stub cannot trace to an issue or a comment")
    _restore(refs, moment, real_gh)


def _restore(refs: list[_Ref], moment: datetime, real_gh: RealGh) -> None:
    """Replace the body of every ref by the text it had at the moment. One GraphQL read per batch of issues; nothing is
    read when ``refs`` is empty. Raises ``_Refused`` when a text at the moment cannot be shown."""
    by_repo: dict[str, dict[int, list[_Ref]]] = {}
    for ref in refs:
        by_repo.setdefault(ref.repo, {}).setdefault(ref.number, []).append(ref)
    for repo, issues in by_repo.items():
        numbers = list(issues)
        for start in range(0, len(numbers), _HISTORY_BATCH):
            chunk = {n: issues[n] for n in numbers[start:start + _HISTORY_BATCH]}
            history = _history(repo, chunk, real_gh)
            for number, group in chunk.items():
                for ref in group:
                    _apply(ref, history[number], moment)


def _history(repo: str, chunk: dict[int, list[_Ref]], real_gh: RealGh) -> dict[int, dict]:
    """The edit history of each issue or PR of ``chunk``: lastEditedAt and body versions where a body is wanted, the
    comments' ids, lastEditedAt and versions where a comment is. A read; the stub never writes."""
    owner, _, name = repo.partition("/")
    parts = []
    for number, group in chunk.items():
        selection = ""
        if any(ref.comment_id is None for ref in group):
            selection += "lastEditedAt userContentEdits(first:100){nodes{createdAt diff}} "
        if any(ref.comment_id is not None for ref in group):
            selection += ("comments(first:100){nodes{databaseId lastEditedAt "
                          "userContentEdits(first:50){nodes{createdAt diff}}}} ")
        parts.append(f"n{number}:issueOrPullRequest(number:{number}){{... on Issue{{{selection}}} "
                     f"... on PullRequest{{{selection}}}}}")
    query = "query($owner:String!,$name:String!){repository(owner:$owner,name:$name){" + " ".join(parts) + "}}"
    rc, out, err = real_gh(["api", "graphql", "-f", f"query={query}", "-f", f"owner={owner}", "-f", f"name={name}"])
    label = ", ".join(f"#{n}" for n in chunk)
    if rc != 0:
        raise _Refused(f"the edit history of {label} could not be read ({err.strip() or 'rc ' + str(rc)}), so the text "
                       f"at the moment cannot be shown")
    data = _load(out)
    inner = data.get("data") if isinstance(data, dict) else None
    repository = inner.get("repository") if isinstance(inner, dict) else None
    if not isinstance(repository, dict):
        raise _Refused(f"the edit history of {label} is not the JSON it asked for, so the text at the moment "
                       f"cannot be shown")
    history = {}
    for number in chunk:
        entry = repository.get(f"n{number}")
        if not isinstance(entry, dict):
            raise _Refused(f"the edit history of #{number} could not be found, so the text at the moment cannot be shown")
        history[number] = entry
    return history


def _apply(ref: _Ref, entry: dict, moment: datetime) -> None:
    if ref.comment_id is None:
        node, what = entry, f"the body of #{ref.number}"
    else:
        nodes = (entry.get("comments") or {}).get("nodes") or []
        node = next((c for c in nodes if isinstance(c, dict) and c.get("databaseId") == ref.comment_id), None)
        what = f"comment {ref.comment_id} of #{ref.number}"
        if node is None:
            raise _Refused(f"{what} is not in the edit history the stub reads, so its text at the moment cannot be shown")
    text = _text_at(node, moment, what)
    if text is not None:
        ref.holder["body"] = text


def _text_at(node: dict, moment: datetime, what: str) -> str | None:
    """The text a body had at the moment: the ``diff`` of the newest version made at or before it. None when the body
    was not edited after the moment (the text the CLI shows stands)."""
    last = node.get("lastEditedAt")
    if last is None or not _created_after(last, moment):
        return None
    versions = []
    for edit in (node.get("userContentEdits") or {}).get("nodes") or []:
        if isinstance(edit, dict) and isinstance(edit.get("diff"), str) and not _created_after(edit.get("createdAt"), moment):
            versions.append((_parse_time(edit["createdAt"]), edit["diff"]))
    if not versions:
        raise _Refused(f"{what} was edited after the moment and cannot be shown")
    return max(versions, key=lambda version: version[0])[1]


# ---- shared helpers -----------------------------------------------------------------------------------------------

def _parse(args: list[str], flags: dict[str, str]) -> tuple[list[tuple[str, str]], list[str]]:
    """Split gh arguments into (canonical value flag, value) pairs and positionals. Other flags are boolean."""
    pairs: list[tuple[str, str]] = []
    positional: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            positional.extend(args[i + 1:])
            break
        if arg.startswith("--") and "=" in arg and arg.split("=", 1)[0] in flags:
            name, value = arg.split("=", 1)
            pairs.append((flags[name], value))
        elif arg in flags:
            pairs.append((flags[arg], args[i + 1] if i + 1 < len(args) else ""))
            i += 1
        elif arg.startswith("-") and not arg.startswith("--") and len(arg) > 2 and arg[:2] in flags:
            pairs.append((flags[arg[:2]], arg[2:]))
        elif not arg.startswith("-") or arg == "-":
            positional.append(arg)
        i += 1
    return pairs, positional


def _last(pairs: list[tuple[str, str]], name: str) -> str | None:
    value = None
    for flag, text in pairs:
        if flag == name:
            value = text
    return value


def _number(token: str) -> int | None:
    match = re.fullmatch(r"#?(\d+)", token) or re.search(r"/(?:issues|pull)/(\d+)/?$", token)
    return int(match.group(1)) if match else None


def _url(repo: str, group: str, number: int) -> str:
    kind = "pull" if group == "pr" else "issues"
    return f"https://github.com/{repo}/{kind}/{number}"


def _parse_time(text: str) -> datetime:
    value = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _created_after(stamp, moment: datetime) -> bool:
    """True when a created time is later than the moment. A missing or unreadable time counts as later (fail closed)."""
    if not isinstance(stamp, str):
        return True
    try:
        return _parse_time(stamp) > moment
    except ValueError:
        return True


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


if __name__ == "__main__":
    raise SystemExit(main())
