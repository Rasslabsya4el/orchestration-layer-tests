"""One message to the brain chat when a session parks, closes or stops a ticket on a question.

``BrainNotifier.observe`` takes ``{issue number: "parked" | "closed" | "needs_info"}`` for one batch and sends ONE
message to the chat named by the brain title: one line per issue,

    <number> <title, at most 60 chars>: <state>: <first line of the checkpoint or closing comment, at most 160 chars>

A ``needs_info`` issue reads ``<state>`` as ``needs a decision (checkpoint <link>)`` (#213): the newest comment is the
question, and a newer one is a new event.

Standard library only. Every outside seam is injected (send, issue lookup, autopilot lookup, brain liveness, log,
clock), so the module is checked and tested with fakes and imports nothing of the repo that uses it.

Dedup lives in a JSON state file (issue number -> last state word, key, attempts, done, via). The key is the event
the word was told for: for "parked" and "needs_info" the id of the checkpoint (the newest comment), for "closed" the
close time. The same word AND the same key for the same issue is never sent twice, also across a restart with the same
file. A changed word, or the same word on a new key (a ticket parked, taken again, parked again), is sent once more.
A record without a key (the older shape) reads as "sent for whatever event is there now": its key is filled in from
the first read, nothing is sent for it.
A failed send is retried once, on a later call that passes the issue or still finds it pending; a second failure
drops it with one log line.

One more word, "unblocked" (#209): the line is written by the caller (``observe(..., lines={n: (key, text)})``), its
key is the unblocker's brain-answer comment, and the text is stored in a pending record, so a held or failed line is
sent as written by a later call and a restart. ``knows(n, key)`` tells the caller the notifier owns that line.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TITLE_MAX = 60
LINE_MAX = 160
#: a send is attempted at most this many times for one (issue, state word); the second failure drops it.
MAX_ATTEMPTS = 2
#: #209: the state word of the line an unblocker's brain-answer is told with (the caller writes the line).
UNBLOCKED = "unblocked"
#: #1113: a pending notice (held while its chat is away) is re-read from GitHub at most once per 1800 s, i.e. 30 minutes.
#: The dispatcher calls observe every 30 s tick, so a read per tick cost one ``gh`` call per pending notice per tick.
#: Inside the window the record's stored state word, event key and chat are reused; the window runs from the record's
#: own ``at`` stamp, which only a real read refreshes.
PENDING_REREAD_SECONDS = 1800
#: the leading "<!--" and the trailing "-->" of an HTML marker line, stripped before the first line is taken.
_MARKER = re.compile(r"^\s*<!--\s*|\s*-->\s*$")


def first_line(text: object) -> str:
    """The first non-empty line of ``text`` with its HTML marker brackets stripped, at most LINE_MAX chars.

    ``<!-- parked-on-run: x -->`` gives ``parked-on-run: x``. A line that is empty once the brackets are gone
    (a marker-only line) falls to the next non-empty line. No such line gives ``""``.
    """
    for raw in str(text or "").splitlines():
        line = _MARKER.sub("", raw).strip()
        if line:
            return line[:LINE_MAX]
    return ""


#: the state word of a question left for the brain (#213).
NEEDS_INFO_WORD = "needs_info"


def format_line(number: int, title: object, state: str, text: object, link: str = "") -> str:
    """One message line: ``<number> <title>: <state>: <first line>`` (title and first line are cut, not wrapped).
    The word ``needs_info`` reads ``needs a decision`` and, with a ``link`` to the checkpoint, names it."""
    head = " ".join(str(title or "").split())[:TITLE_MAX]
    if state == NEEDS_INFO_WORD:
        state = f"needs a decision (checkpoint {link})" if link else "needs a decision"
    return f"#{number} {head}: {state}: {first_line(text)}".rstrip()


class IssueRead(tuple):
    """The notifier's read of one issue: ``(title, newest comment body, park_id, close_id)``, the shape
    ``GhCli.issue_title_and_last_comment`` has always returned, with the issue's label names riding along as
    ``.labels`` (one ``gh`` call reads both; a plain 4-tuple has no labels and routes to the default chat) and the
    newest comment's URL as ``.link`` (#213: where a ``needs_info`` line points)."""

    labels: tuple[str, ...] = ()
    link: str = ""

    def __new__(cls, title: str, text: str, park_id: str | None, close_id: str | None,
                labels: Iterable[str] = (), link: str = "") -> "IssueRead":
        self = super().__new__(cls, (title, text, park_id, close_id))
        self.labels = tuple(labels)
        self.link = str(link or "")
        return self


def brain_target(settings: Mapping[str, Any], area_labels: Iterable[str]) -> str:
    """The brain chat an issue's news goes to: ``settings["brain_chats"][<its area label>]`` when one of its
    labels is mapped there, else ``settings["brain_chat_title"]``. An empty or missing table is the title."""

    chats = settings.get("brain_chats") or {}
    for label in area_labels:
        title = chats.get(label)
        if title:
            return str(title)
    return str(settings["brain_chat_title"])


def _gh_read_labels(issue: int, repo: str) -> tuple[str, ...]:
    """The label names of one issue: `gh issue view N -R <repo> --json labels`. A failed read raises."""
    from tools.agentops.gh_call import run_gh  # lazy: the module's top level stays standard library only

    result = run_gh(["issue", "view", str(issue), "-R", repo, "--json", "labels"])
    if result.returncode != 0:
        raise RuntimeError((result.stderr or "").strip()[:200] or f"gh exit {result.returncode}")
    data = json.loads(result.stdout)
    return tuple(str(label.get("name") or "") for label in data.get("labels") or [])


def run_brain_chat_command(issue: int, *, settings: Mapping[str, Any] | None = None,
                           read_labels: Callable[[int, str], Sequence[str]] | None = None) -> tuple[int, str]:
    """`python -m tools.agentops brain-chat --issue N` (#24): ORCH's report goes to the chat the dispatcher's
    notices for the issue go to. Returns `(rc, text)`: rc 0 with the chat title `brain_target` gives for the
    issue's label names (settings from `config/orchestration.json`, labels from `gh` for the settings' repo);
    rc 1 with the reason when the settings or the `gh` read failed. Never guesses a default title."""
    try:
        if settings is None:
            from tools.agentops import core_settings
            settings = core_settings.load()
        reader = read_labels if read_labels is not None else _gh_read_labels
        labels = reader(issue, str(settings["repo"]))
        title = brain_target(settings, labels)
    except Exception as exc:  # a failed read is reported, never a crash of the caller's turn
        return 1, f"brain-chat: #{issue} not read ({type(exc).__name__}: {str(exc)[:200]})"
    return 0, title


def _labels_of(info: Sequence[Any] | None) -> tuple[str, ...]:
    return tuple(getattr(info, "labels", ()) or ()) if info is not None else ()


class BrainNotifier:
    """Sends a brain chat one message per observed batch of parked, closed or needs-info issues, each state once.

    The batch is split by target (#1109): an issue goes to ``brain_chats[its area label]`` when mapped, else to
    ``brain_title``; one message per target, each with its own liveness, hold and retry. With no table
    (``brain_chats`` empty or missing) there is one target, the title: the single-message behaviour.

    Seams (all injected):
      send(brain_title, message) -- may raise; the batch is then kept pending for one retry.
      issue_info(n) -> (title, last_comment_text[, park_id[, close_id]]) | None -- None leaves the issue for a
          later call, unrecorded. park_id names the parking checkpoint, close_id the close; a missing or None id
          cannot tell one event from another, so the state word alone decides. A read with ``.labels`` (see
          ``IssueRead``) routes the issue to its area's chat.
      autopilot_live_for(n) -> autopilot name | None -- a name skips the issue (logged once, recorded as handled).
      brain_live() -> bool -- the liveness of the default title, and of every target when ``brain_live_for`` is
          not given. False sends nothing to that target this call: each candidate is kept pending (no attempt
          spent) and sent by the first call that finds the chat live; one log line per away period.
      brain_live_for(title) -> bool -- optional: the liveness of one target's chat.
      log(text) -- one call per log line.
      now() -> datetime -- stamps the state records (default: UTC now).
    """

    def __init__(
        self,
        *,
        state_path: str | Path,
        brain_title: str,
        send: Callable[[str, str], Any],
        issue_info: Callable[[int], Sequence[Any] | None],
        autopilot_live_for: Callable[[int], str | None],
        brain_live: Callable[[], bool],
        log: Callable[[str], Any],
        now: Callable[[], datetime] | None = None,
        brain_chats: Mapping[str, str] | None = None,
        brain_live_for: Callable[[str], bool] | None = None,
    ) -> None:
        self._path = Path(state_path)
        self._brain_title = brain_title
        self._brain_chats = dict(brain_chats or {})
        self._send = send
        self._issue_info = issue_info
        self._autopilot_live_for = autopilot_live_for
        self._brain_live = brain_live
        self._brain_live_for = brain_live_for
        self._log = log
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._records: dict[int, dict[str, Any]] = self._load()
        self._dirty = False
        #: #209: this call's lines written by the caller, issue -> (event key, line); a pending record's own is restored.
        self._custom: dict[int, tuple[str, str]] = {}
        #: the targets whose "not live" line is logged, until each is seen live again (one line per away period).
        self._away_targets: set[str] = set()

    # ----- state file -------------------------------------------------------------------------------------------

    def _load(self) -> dict[int, dict[str, Any]]:
        if not self._path.exists():
            return {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self._log(f"brain notice: state file unreadable ({type(exc).__name__}), starting empty")
            return {}
        if not isinstance(raw, dict):
            return {}
        records: dict[int, dict[str, Any]] = {}
        for key, value in raw.items():
            try:
                number = int(key)
            except (TypeError, ValueError):
                continue
            if isinstance(value, dict):
                records[number] = value
        return records

    def _save(self) -> None:
        data = {str(number): record for number, record in sorted(self._records.items())}
        tmp = self._path.with_name(self._path.name + ".tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError as exc:
            self._log(f"brain notice: state file not written ({type(exc).__name__}: {exc})")

    # ----- decisions --------------------------------------------------------------------------------------------

    def _done_for(self, number: int, word: str) -> bool:
        record = self._records.get(number)
        return bool(record and record.get("done") and record.get("state") == word)

    @staticmethod
    def _unpack(info: Sequence[Any]) -> tuple[str, str, str | None, str | None]:
        title, text, *ids = info
        park_id = ids[0] if len(ids) > 0 else None
        close_id = ids[1] if len(ids) > 1 else None
        return title, text, (str(park_id) if park_id else None), (str(close_id) if close_id else None)

    def _event_key(self, info: Sequence[Any], word: str) -> str | None:
        """The id of the event ``word`` is told for: the parking checkpoint, or the close."""
        _, _, park_id, close_id = self._unpack(info)
        return close_id if word == "closed" else park_id

    def _is_new_event(self, number: int, word: str, info_for: Callable[[int], Sequence[Any] | None]) -> bool:
        """True when the issue's record is done for ``word`` but the event it is observed on is another one.
        An unreadable issue, an unknown id on either side, or a record of the older shape (its key is filled in
        from this read) is not a new event."""
        if number in self._custom:
            key: str | None = self._custom[number][0]
        else:
            info = info_for(number)
            if info is None:
                return False
            key = self._event_key(info, word)
        if key is None:
            return False
        record = self._records[number]
        stored = record.get("key")
        if not stored:
            record["key"] = key
            self._dirty = True
            return False
        return str(stored) != key

    def _stamp(self) -> str:
        return self._now().isoformat(timespec="seconds")

    def _reusable(self, number: int, word: str) -> bool:
        """#1113: True when a pending record can be held with no read: same state word, a stored key and chat, and
        its last real read (its ``at`` stamp) less than PENDING_REREAD_SECONDS old. Anything else is read as before."""
        record = self._records.get(number)
        if not record or record.get("done") or record.get("state") != word:
            return False
        if not record.get("key") or not record.get("target"):
            return False
        try:
            age = self._now().timestamp() - datetime.fromisoformat(str(record.get("at"))).timestamp()
        except (TypeError, ValueError):
            return False
        return 0 <= age < PENDING_REREAD_SECONDS

    def _hold(self, candidates: Sequence[int], items: Mapping[int, str], target: str,
              info_for: Callable[[int], Sequence[Any] | None]) -> None:
        """Keep each candidate pending while the chat is away: state word and event key recorded, not done, no
        attempt counted (a failed send is what counts). An issue with no readable info keeps only its state word.
        A candidate read inside the re-read window (#1113) is kept as it is: no read, no new stamp."""
        for number in candidates:
            word = items[number]
            if self._reusable(number, word):
                continue
            info = info_for(number)
            prior = self._records.get(number, {})
            same_word = prior.get("state") == word
            key = self._event_key(info, word) if info is not None else (prior.get("key") if same_word else None)
            if number in self._custom:
                key = self._custom[number][0]
            self._records[number] = {"state": word, "key": key,
                                     "attempts": int(prior.get("attempts", 0)) if same_word else 0,
                                     "done": False, "via": "away", "at": self._stamp(),
                                     # the chat is stored only from a read that answered (#1113)
                                     "target": target if info is not None else None}
            self._keep_line(number)
        self._save()

    def _keep_line(self, number: int) -> None:
        """A pending record of a caller-written line keeps the line, so a retry and a restart send it as written."""
        if number in self._custom:
            self._records[number]["line"] = self._custom[number][1]

    def knows(self, number: int, key: str) -> bool:
        """True when the notifier owns the line told for ``(number, key)``: sent, pending a retry, dropped or skipped."""
        record = self._records.get(number)
        if not record or record.get("state") != UNBLOCKED:
            return False
        return str(record.get("key")) == key or (bool(record.get("done")) and record.get("via") == "autopilot")

    def observe(self, outcomes: Mapping[int, str], lines: Mapping[int, tuple[str, str]] | None = None) -> list[str]:
        """Send one message for the issues of ``outcomes`` whose state word is new; never raises.

        Pending retries (a failed send, not yet dropped) are included even when ``outcomes`` does not name them.
        ``lines`` (#209) gives, for an issue told as "unblocked", its event key and the line to send.
        Returns the message lines sent, or ``[]``.
        """
        items: dict[int, str] = {int(n): str(word) for n, word in outcomes.items()}
        for number, record in self._records.items():
            if not record.get("done") and number not in items and record.get("state"):
                items[number] = str(record["state"])
        self._custom = {int(n): (str(kt[0]), str(kt[1])) for n, kt in (lines or {}).items()
                        if items.get(int(n)) == UNBLOCKED}
        for number, word in items.items():
            record = self._records.get(number, {})
            if (number not in self._custom and word == UNBLOCKED and record.get("line") and not record.get("done")
                    and record.get("key")):
                self._custom[number] = (str(record["key"]), str(record["line"]))
        reads: dict[int, Sequence[Any] | None] = {}

        def info_for(number: int) -> Sequence[Any] | None:
            """One read of the issue per call of ``observe``; a failed read is not cached (logged, None)."""
            if number not in reads:
                try:
                    reads[number] = self._issue_info(number)
                except Exception as exc:
                    self._log(f"brain notice: #{number} issue lookup failed ({type(exc).__name__}: {exc})")
                    return None
            return reads[number]

        self._dirty = False
        candidates = sorted(n for n, word in items.items()
                            if not self._done_for(n, word) or self._is_new_event(n, word, info_for))
        if not candidates:
            if self._dirty:
                self._save()
            return []

        groups: dict[str, list[int]] = {}
        for number in candidates:
            groups.setdefault(self._target_of(number, items[number], info_for), []).append(number)
        returned: list[str] = []
        for target in sorted(groups):
            returned.extend(self._notify_target(target, groups[target], items, info_for))
        return returned

    def _target_of(self, number: int, word: str, info_for: Callable[[int], Sequence[Any] | None]) -> str:
        """The chat the issue's news goes to (#1109): its area's chat when the table maps one, else the title.
        Inside the re-read window (#1113) the chat stored at the last real read is used, with no read."""

        if self._reusable(number, word):
            return str(self._records[number]["target"])
        settings = {"brain_chat_title": self._brain_title, "brain_chats": self._brain_chats}
        return brain_target(settings, _labels_of(info_for(number)))

    def _live(self, target: str) -> bool:
        """The liveness of one target's chat; a failed probe counts as not live (one log line)."""

        try:
            if self._brain_live_for is not None:
                return bool(self._brain_live_for(target))
            return bool(self._brain_live())
        except Exception as exc:  # a failed liveness probe counts as not live
            self._log(f"brain notice: liveness check failed ({type(exc).__name__}: {exc})")
            return False

    def _notify_target(self, target: str, numbers: Sequence[int], items: Mapping[int, str],
                       info_for: Callable[[int], Sequence[Any] | None]) -> list[str]:
        """One target's share of a batch (#1109): held while its chat is away, else one message to that chat."""

        if not self._live(target):
            # #946: a notice seen while the chat is away is kept, not dropped: pending, no attempt spent,
            # sent by the first call that finds the chat live (the dispatcher calls observe every tick).
            self._hold(numbers, items, target, info_for)
            if target not in self._away_targets:
                self._away_targets.add(target)
                self._log(f"brain notice: brain chat {target} not live, nothing sent for {len(numbers)} issue(s); "
                          f"kept until it is live")
            return []
        self._away_targets.discard(target)

        lines: list[str] = []
        sent: list[int] = []
        keys: dict[int, str | None] = {}
        for number in numbers:
            word = items[number]
            try:
                autopilot = self._autopilot_live_for(number)
            except Exception as exc:
                self._log(f"brain notice: #{number} autopilot lookup failed ({type(exc).__name__}: {exc})")
                continue
            if autopilot:
                self._log(f"brain notice: #{number} skipped, autopilot {autopilot} is live")
                self._records[number] = {"state": word, "attempts": 0, "done": True, "via": "autopilot",
                                         "at": self._stamp()}
                continue
            info = info_for(number)
            if info is None:
                continue
            title, text, _, _ = self._unpack(info)
            custom = self._custom.get(number)
            lines.append(custom[1] if custom else format_line(number, title, word, text,
                                                              str(getattr(info, "link", "") or "")))
            sent.append(number)
            keys[number] = custom[0] if custom else self._event_key(info, word)

        if not lines:
            self._save()
            return []

        message = "\n".join(lines)
        try:
            self._send(target, message)
        except Exception as exc:
            dropped: list[int] = []
            for number in sent:
                prior = self._records.get(number, {})
                attempts = (int(prior.get("attempts", 0)) if prior.get("state") == items[number]
                            and not prior.get("done") else 0) + 1
                is_dropped = attempts >= MAX_ATTEMPTS
                if is_dropped:
                    dropped.append(number)
                self._records[number] = {"state": items[number], "key": keys[number], "attempts": attempts,
                                         "done": is_dropped, "via": "dropped" if is_dropped else "pending",
                                         "at": self._stamp()}
                self._keep_line(number)
            kept = len(sent) - len(dropped)
            self._log(f"brain notice: send failed ({type(exc).__name__}: {exc}); {kept} kept for one retry, "
                      f"{len(dropped)} dropped after the retry")
            self._save()
            return []

        for number in sent:
            self._records[number] = {"state": items[number], "key": keys[number], "attempts": 0, "done": True,
                                     "via": "sent", "at": self._stamp()}
        self._save()
        return lines
