"""The brain's purpose tag: the one parser of ``# purpose: <word>`` and the one state step (issues #135, #178).

``purposes`` (the dollar split) and the untagged-call hook both read the tag through ``TagState``, so what
counts as a tag, as an unknown word and as a reset can never drift apart.

A tag is the first line of a Bash or PowerShell command: ``# purpose: <word>``. It holds until the next tag or
the next owner (human user) message. A word outside ``PURPOSE_WORDS`` is no tag: it puts the state back to
``untagged`` and is remembered as the unknown word.

Stdlib only; imports nothing from the rest of ``tools``, so a hook can load it in any target.
"""

from __future__ import annotations

import re

#: the closed list of words a ``# purpose:`` line may carry
PURPOSE_WORDS = ("closure-review", "grill", "ticket", "notice", "owner", "research")
UNTAGGED = "untagged"
SHELL_TOOLS = ("Bash", "PowerShell")

_TAG_LINE = re.compile(r"^#\s*purpose:\s*(.*?)\s*$")


def tag_of(tool_input: object) -> str | None:
    """The word after ``# purpose:`` when the command starts with that line, else None (known or not)."""
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str):
        return None
    lines = command.lstrip().splitlines()
    match = _TAG_LINE.match(lines[0]) if lines else None
    return match.group(1) if match else None


def _message_text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(block.get("text", "") for block in content
                        if isinstance(block, dict) and block.get("type") == "text")
    return ""


def is_owner_message(obj: dict) -> bool:
    """A human user line: not a meta line, not a task notification, with some text of its own."""
    if obj.get("isMeta"):
        return False
    message = obj.get("message")
    text = _message_text(message if isinstance(message, dict) else {}).strip()
    return bool(text) and not text.startswith("<task-notification>")


class TagState:
    """The purpose in force while a transcript is read record by record.

    ``purpose`` is a word of ``PURPOSE_WORDS`` or ``UNTAGGED``; ``unknown`` is the word of the last tag line
    that was not on the list, until the next tag or reset.
    """

    def __init__(self) -> None:
        self.purpose = UNTAGGED
        self.unknown: str | None = None

    @property
    def in_force(self) -> bool:
        return self.purpose != UNTAGGED

    def see_user(self, obj: dict) -> bool:
        """Feed a ``user`` record; an owner message resets the state. True when it was one."""
        if not is_owner_message(obj):
            return False
        self.purpose, self.unknown = UNTAGGED, None
        return True

    def see_tool_use(self, name: object, tool_input: object) -> None:
        """Feed an assistant ``tool_use``; only a Bash or PowerShell call whose first line is a tag sets one."""
        if name not in SHELL_TOOLS:
            return
        word = tag_of(tool_input)
        if word is None:
            return
        if word in PURPOSE_WORDS:
            self.purpose, self.unknown = word, None
        else:
            self.purpose, self.unknown = UNTAGGED, word
