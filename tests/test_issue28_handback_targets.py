from tests.test_agentops_merge_handback import make, rec, writes
from tools.agentops.merge_handback import post_merge_result

LANDED_LINE = ("Merge of orch-28 landed on main abcdef123456 after 2.1 min in line and a suite of 17 min, "
               "0 failed. Result r.json; suite output out.txt.")


def test_targets_line_holds_every_record_line_in_order(tmp_path):
    gh, calls = make(tmp_path)
    lines = ["o/t1: taken", "o/t2: failed: core update refused: dirty tree"]
    assert post_merge_result(rec(core_targets=lines), "r.json", "orch-28", gh) == [5]
    body = writes(calls)[0][1]
    assert body.splitlines() == [
        "<!-- merge result -->",
        LANDED_LINE,
        "targets: o/t1: taken; o/t2: failed: core update refused: dirty tree",
    ]


def test_no_key_or_empty_list_leaves_comment_byte_for_byte(tmp_path):
    expected = f"<!-- merge result -->\n{LANDED_LINE}\n"
    for record in (rec(), rec(core_targets=[])):
        gh, calls = make(tmp_path)
        post_merge_result(record, "r.json", "orch-28", gh)
        assert writes(calls)[0][1] == expected
