"""Issue #349 unit 2: the leak scan of a session transcript and its helpers.

A transcript is a .jsonl file, one JSON record per line. Only what a tool
returned (a user record whose message.content holds tool_result blocks) is
scanned. Assistant text and the owner's plain user message are not. A run's
helpers are separate transcripts, in the subagents folder next to the session.
"""

import json
from pathlib import Path

import pytest

from tools.replays.leak_scan import ScanResult, is_void, scan_run, scan_transcript, transcript_files

CYRILLIC_MARKER = "Ты уверен что это то что я просил"


def _tool_result_record(tool_use_id: str, content) -> dict:
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}
            ],
        },
    }


def _write(path: Path, lines: list) -> Path:
    """Write lines as a jsonl file. bytes are written raw, str as UTF-8, other values as JSON."""
    chunks = []
    for line in lines:
        if isinstance(line, bytes):
            chunks.append(line)
        elif isinstance(line, str):
            chunks.append(line.encode("utf-8"))
        else:
            chunks.append(json.dumps(line, ensure_ascii=False).encode("utf-8"))
    path.write_bytes(b"\n".join(chunks) + b"\n")
    return path


@pytest.mark.parametrize(
    "content, marker, tool_use_id",
    [
        ("the parity check passed", "parity", "tu_string"),
        (
            [
                {"type": "text", "text": "clean line"},
                {"type": "text", "text": "a line with parity in it"},
            ],
            "parity",
            "tu_list",
        ),
        (f"owner said: {CYRILLIC_MARKER}, really", CYRILLIC_MARKER, "tu_cyrillic"),
    ],
    ids=["string", "list-of-text", "cyrillic"],
)
def test_marker_in_tool_result_is_a_hit(tmp_path, content, marker, tool_use_id):
    transcript = _write(tmp_path / "t.jsonl", [_tool_result_record(tool_use_id, content)])

    result = scan_transcript(transcript, [marker])

    assert result.hits == [{"marker": marker, "line": 0, "tool_use_id": tool_use_id}]
    assert result.skipped_lines == []
    assert is_void(result) is True


def test_list_plain_strings_are_read_and_other_block_kinds_are_ignored(tmp_path):
    plain = _tool_result_record("tu_plain", ["a plain parity string"])
    image_only = _tool_result_record(
        "tu_image", [{"type": "image", "source": {"type": "base64", "data": "parity"}}]
    )
    transcript = _write(tmp_path / "t.jsonl", [plain, image_only])

    result = scan_transcript(transcript, ["parity"])

    assert result.hits == [{"marker": "parity", "line": 0, "tool_use_id": "tu_plain"}]


def test_matching_is_case_insensitive_and_reports_the_marker_as_given(tmp_path):
    transcript = _write(tmp_path / "t.jsonl", [_tool_result_record("tu_case", "«Parity» check")])

    result = scan_transcript(transcript, ["parity"])

    assert result.hits == [{"marker": "parity", "line": 0, "tool_use_id": "tu_case"}]


def test_marker_only_in_assistant_or_owner_message_is_no_hit(tmp_path):
    assistant = {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": "parity"}]},
    }
    owner = {"type": "user", "message": {"role": "user", "content": "parity"}}
    transcript = _write(tmp_path / "t.jsonl", [assistant, owner])

    result = scan_transcript(transcript, ["parity"])

    assert result.hits == []
    assert result.skipped_lines == []
    assert is_void(result) is False


def test_malformed_line_is_reported_and_line_numbers_stay_physical(tmp_path):
    transcript = _write(
        tmp_path / "t.jsonl",
        [
            _tool_result_record("tu_first", "clean"),
            "{this is not json",
            _tool_result_record("tu_third", "parity after the broken line"),
        ],
    )

    result = scan_transcript(transcript, ["parity"])

    assert result.hits == [{"marker": "parity", "line": 2, "tool_use_id": "tu_third"}]
    assert result.skipped_lines == [1]


def test_unreadable_line_is_reported_not_passed(tmp_path):
    not_utf8 = b'{"type":"user","note":"\xff\xfe"}'
    transcript = _write(
        tmp_path / "t.jsonl",
        [_tool_result_record("tu_ok", "clean"), not_utf8],
    )

    result = scan_transcript(transcript, ["parity"])

    assert result.hits == []
    assert result.skipped_lines == [1]


def test_no_markers_gives_no_hits_and_not_void(tmp_path):
    transcript = _write(
        tmp_path / "t.jsonl",
        [_tool_result_record("tu_c", "nothing to see here")],
    )

    result = scan_transcript(transcript, ["parity", CYRILLIC_MARKER])

    assert result.hits == []
    assert is_void(result) is False


def test_transcript_files_lists_session_then_sorted_subagent_transcripts(tmp_path):
    session = _write(tmp_path / "abc.jsonl", [])
    subagents = tmp_path / "abc" / "subagents"
    subagents.mkdir(parents=True)
    _write(subagents / "agent-2.jsonl", [])
    _write(subagents / "agent-1.jsonl", [])
    (subagents / "agent-1.meta.json").write_text("{}", encoding="utf-8")

    files = transcript_files(session)

    assert files == [session, subagents / "agent-1.jsonl", subagents / "agent-2.jsonl"]


def test_transcript_files_without_subagents_folder_is_the_session_only(tmp_path):
    session = _write(tmp_path / "abc.jsonl", [])

    assert transcript_files(session) == [session]


def test_scan_run_scans_session_and_subagents_and_tags_each_hit_with_its_file(tmp_path):
    session = _write(
        tmp_path / "abc.jsonl",
        [_tool_result_record("tu_s", "parity in session"), "{broken"],
    )
    subagents = tmp_path / "abc" / "subagents"
    subagents.mkdir(parents=True)
    sub = _write(
        subagents / "agent-1.jsonl",
        [
            _tool_result_record("tu_sub_clean", "clean"),
            _tool_result_record("tu_sub", "parity in subagent"),
        ],
    )

    result = scan_run(session, ["parity"])

    assert isinstance(result, ScanResult)
    assert result.hits == [
        {"marker": "parity", "line": 0, "tool_use_id": "tu_s", "file": session},
        {"marker": "parity", "line": 1, "tool_use_id": "tu_sub", "file": sub},
    ]
    assert result.skipped_lines == [(session, 1)]
    assert is_void(result) is True
