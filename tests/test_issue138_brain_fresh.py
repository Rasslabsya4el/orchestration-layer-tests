"""#138 unit 1: tools/agentops/brain_fresh.py awaiting_owner_message on written transcripts (no network)."""
import json
from pathlib import Path

from tools.agentops.brain_fresh import awaiting_owner_message

CLEAR = {
    "type": "user",
    "message": {
        "role": "user",
        "content": "<command-name>/clear</command-name>\n"
        "            <command-message>clear</command-message>\n"
        "            <command-args></command-args>",
    },
}

CROSS_SESSION = {
    "type": "user",
    "message": {
        "role": "user",
        "content": "Another Claude session sent a message:\n"
        '<cross-session-message from="uds:\\\\.\\pipe\\LOCAL\\cc-msg-68aa" from-name="dispatcher" from-mode="bypass">\n'
        "#133 A fresh worker copy can run the monitor tests without a manu: parked\n"
        "</cross-session-message>\n\nThis came from another Claude session.",
    },
}

QUEUED_OWNER_LINE = {
    "type": "attachment",
    "attachment": {
        "type": "queued_command",
        "prompt": "/mattpocock-skills:wayfinder 22 continue",
    },
}

OWNER_HELLO = {
    "type": "user",
    "message": {"role": "user", "content": "hello"},
}


def _write(path: Path, records: list[dict]) -> Path:
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8"
    )
    return path


def test_a_clear_then_cross_session_and_queued_line_is_false(tmp_path):
    # The owner's line arrived mid-turn as a queued_command attachment: not fresh.
    path = _write(tmp_path / "s.jsonl", [CLEAR, CROSS_SESSION, QUEUED_OWNER_LINE])
    assert awaiting_owner_message(path) is False


def test_b_clear_then_cross_session_only_is_true(tmp_path):
    # Only the dispatcher's cross-session record follows the clear: nothing from the owner.
    path = _write(tmp_path / "s.jsonl", [CLEAR, CROSS_SESSION])
    assert awaiting_owner_message(path) is True


def test_c_clear_cross_session_then_owner_hello_is_false(tmp_path):
    # A plain owner message after the clear settles it: not fresh.
    path = _write(tmp_path / "s.jsonl", [CLEAR, CROSS_SESSION, OWNER_HELLO])
    assert awaiting_owner_message(path) is False


def test_d_owner_message_before_clear_only_is_true(tmp_path):
    # The owner spoke before the /clear; nothing after it: the clear is still awaiting a message.
    path = _write(tmp_path / "s.jsonl", [OWNER_HELLO, CLEAR])
    assert awaiting_owner_message(path) is True


def test_e_no_clear_is_false(tmp_path):
    path = _write(tmp_path / "s.jsonl", [OWNER_HELLO, CROSS_SESSION])
    assert awaiting_owner_message(path) is False


def test_f_missing_file_is_false(tmp_path):
    assert awaiting_owner_message(tmp_path / "absent.jsonl") is False


def test_g_tool_result_records_of_the_dispatcher_turn_do_not_count(tmp_path):
    # The turn the dispatcher's message started runs tools: their tool_result user records hold no text.
    tool_result = {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}}
    path = _write(tmp_path / "s.jsonl", [CLEAR, CROSS_SESSION, tool_result])
    assert awaiting_owner_message(path) is True


def test_h_queued_command_with_a_peer_message_does_not_count(tmp_path):
    queued_peer = {"type": "attachment", "attachment": {
        "type": "queued_command", "prompt": '<cross-session-message from="x">#1 parked</cross-session-message>'}}
    path = _write(tmp_path / "s.jsonl", [CLEAR, CROSS_SESSION, queued_peer])
    assert awaiting_owner_message(path) is True


def test_i_the_last_clear_decides(tmp_path):
    assert awaiting_owner_message(_write(tmp_path / "a.jsonl", [CLEAR, OWNER_HELLO, CLEAR])) is True
    assert awaiting_owner_message(
        _write(tmp_path / "b.jsonl", [CLEAR, CLEAR, CROSS_SESSION, QUEUED_OWNER_LINE])) is False


def test_j_the_owners_expanded_slash_command_lifts_the_hold(tmp_path):
    # the harness expands the owner's start line into a <command-name> user record
    expanded = {"type": "user", "message": {"role": "user", "content": (
        "<command-message>wayfinder</command-message>\n"
        "<command-name>/mattpocock-skills:wayfinder</command-name>\n<command-args>22</command-args>")}}
    path = _write(tmp_path / "s.jsonl", [CLEAR, CROSS_SESSION, expanded])
    assert awaiting_owner_message(path) is False


def test_k_an_image_only_owner_message_lifts_the_hold(tmp_path):
    image = {"type": "user", "message": {"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "data": "x"}}]}}
    assert awaiting_owner_message(_write(tmp_path / "s.jsonl", [CLEAR, CROSS_SESSION, image])) is False
