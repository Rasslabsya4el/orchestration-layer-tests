"""real_process_table keeps every row when one live command line carries a raw control character
(Windows PowerShell 5.1 ConvertTo-Json leaves e.g. \x15 unescaped)."""

from __future__ import annotations

import types

from tools.agentops import orphans


def test_row_survives_a_raw_control_char_in_another_command_line(monkeypatch):
    stdout = ('[{"ProcessId":10,"ParentProcessId":1,"CreationDate":"20261016120000.000000+120",'
              '"CommandLine":"grep -c \x15 file"},'
              '{"ProcessId":11,"ParentProcessId":10,"CreationDate":"20261016120001.000000+120",'
              '"CommandLine":"python -c ok"}]')
    monkeypatch.setattr(orphans.subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(returncode=0, stdout=stdout))
    monkeypatch.setattr(orphans, "_merge_msys", lambda rows, strict=False: rows)
    assert sorted(r.pid for r in orphans.real_process_table()) == [10, 11]
