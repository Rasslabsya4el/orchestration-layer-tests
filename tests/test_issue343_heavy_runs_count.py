"""#343 unit 7: scripts/counts/heavy_runs_on_pc.py counts the heavy runs of the merge flow by place.

Seam: the script's command line, run on a fixture repo folder (var/agentops/merge.log and test_map/build-*.out).
A build-*.out is a legacy PC build only when its file time is older than the first map-build place line of the
window (or there is no such line in the window); a newer one is never counted on its own.
"""
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "counts" / "heavy_runs_on_pc.py"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # no console window per child process (Windows)


def run(repo, *args):
    return subprocess.run([sys.executable, "-I", str(SCRIPT), str(repo), *args],
                          capture_output=True, text=True, creationflags=NO_WINDOW)


def write_log(repo, lines):
    folder = repo / "var" / "agentops"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "merge.log").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_build(repo, name, mtime, text="pytest output\n"):
    """A build-*.out whose file time is set to ``mtime`` (``YYYY-MM-DD HH:MM:SS``, local time)."""
    folder = repo / "var" / "agentops" / "test_map"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(text, encoding="utf-8")
    stamp = datetime.strptime(mtime, "%Y-%m-%d %H:%M:%S").timestamp()
    os.utime(path, (stamp, stamp))


def test_row1_fixture_with_three_lines_of_each_place_and_two_legacy_outs_gives_exact_counts(tmp_path):
    write_log(tmp_path, [
        "2026-10-23 10:00:00 | map-run | map-build on github | aaaaaaaaaaaa | 3021 tests",
        "2026-10-23 11:00:00 | map-run | map-build on github | bbbbbbbbbbbb | 3021 tests",
        "2026-10-23 12:00:00 | map-run | map-build on github | cccccccccccc | 3021 tests",
        "2026-10-23 13:00:00 | map-run | map-build on pc (GitHub cannot start) | dddddddddddd | 3021 tests",
        "2026-10-23 14:00:00 | map-run | map-build on pc (GitHub cannot start) | eeeeeeeeeeee | 3021 tests",
        "2026-10-23 15:00:00 | map-run | map-build on pc (minutes spent) | ffffffffffff | 3021 tests",
    ])
    # older than the first place line: two PC builds of the older form
    write_build(tmp_path, "build-aaaaaaaaaaaa-11111111.out", "2026-10-22 09:00:00")
    write_build(tmp_path, "build-bbbbbbbbbbbb-22222222.out", "2026-10-22 09:30:00")
    proc = run(tmp_path)
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.splitlines()
    assert "map builds on the PC: 5" in lines  # 3 logged PC builds + 2 legacy outs
    assert "map builds on GitHub: 3" in lines


def test_row2_empty_folder_prints_zero(tmp_path):
    proc = run(tmp_path)
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.splitlines()
    assert "map builds on the PC: 0" in lines
    assert "map builds on GitHub: 0" in lines
    assert "prequeue on the PC: 0" in lines
    assert "prequeue on GitHub: 0" in lines
    assert "red-on-base on the PC: 0" in lines
    assert "red-on-base on GitHub: 0" in lines


def test_row3_build_out_with_its_own_place_line_is_counted_once_by_its_log_line(tmp_path):
    write_log(tmp_path, ["2026-10-23 10:00:00 | map-run | map-build on github | aaaaaaaaaaaa | 3021 tests"])
    write_build(tmp_path, "build-aaaaaaaaaaaa-11111111.out", "2026-10-23 10:00:30")  # newer than its line
    lines = run(tmp_path).stdout.splitlines()
    assert "map builds on GitHub: 1" in lines
    assert "map builds on the PC: 0" in lines


def test_row4_window_drops_lines_before_since(tmp_path):
    write_log(tmp_path, [
        "2026-10-20 10:00:00 | map-run | map-build on pc (old) | aaaaaaaaaaaa | 3021 tests",
        "2026-10-23 10:00:00 | map-run | map-build on github | bbbbbbbbbbbb | 3021 tests",
    ])
    lines = run(tmp_path, "--since", "2026-10-22").stdout.splitlines()
    assert "map builds on GitHub: 1" in lines
    assert "map builds on the PC: 0" in lines


def test_row5_prequeue_and_red_on_base_are_counted_by_place(tmp_path):
    write_log(tmp_path, [
        "2026-10-23 10:00:00 | i320 | prequeue on github | suite 4 min",
        "2026-10-23 10:05:00 | i321 | prequeue on pc (test copy refused) | suite 6 min",
        "2026-10-23 10:10:00 | i322 | red-on-base on github | red 0",
        "2026-10-23 10:15:00 | i323 | red-on-base on pc (no machine) | red 0",
    ])
    lines = run(tmp_path).stdout.splitlines()
    assert "prequeue on the PC: 1" in lines
    assert "prequeue on GitHub: 1" in lines
    assert "red-on-base on the PC: 1" in lines
    assert "red-on-base on GitHub: 1" in lines


def test_row6_last_landings_show_the_place_of_their_map_build(tmp_path):
    write_log(tmp_path, [
        "2026-10-23 09:00:00 | i300 | pushed | suite 3 min",
        "2026-10-23 09:01:00 | map-run | map-build on github | 111111111111 | 3021 tests",
        "2026-10-23 10:00:00 | i301 | pushed | suite 3 min",
        "2026-10-23 10:30:00 | map-run | safety run: green | 222222222222",  # older form: no place word, so PC
        "2026-10-23 11:00:00 | i302 | pushed | suite 3 min",  # no map build after it yet: none
    ])
    lines = run(tmp_path, "--last", "3").stdout.splitlines()
    assert "  2026-10-23 09:00:00 i300 -> github" in lines
    assert "  2026-10-23 10:00:00 i301 -> pc" in lines
    assert "  2026-10-23 11:00:00 i302 -> none" in lines
    short = run(tmp_path, "--last", "2").stdout.splitlines()
    assert "  2026-10-23 09:00:00 i300 -> github" not in short
    assert "  2026-10-23 10:00:00 i301 -> pc" in short
    assert "  2026-10-23 11:00:00 i302 -> none" in short


def test_row7_missing_merge_log_is_no_error(tmp_path):
    proc = run(tmp_path / "not-a-repo")
    assert proc.returncode == 0, proc.stderr
    assert "map builds on the PC: 0" in proc.stdout.splitlines()


def test_row8_one_new_form_pc_build_with_its_log_line_and_out_is_counted_once(tmp_path):
    write_log(tmp_path, ["2026-10-23 10:00:00 | map-run | map-build on pc (GitHub cannot start) | aaaaaaaaaaaa | 3021 tests"])
    write_build(tmp_path, "build-aaaaaaaaaaaa-11111111.out", "2026-10-23 10:00:30")  # newer than its line: not legacy
    write_build(tmp_path, "build-bbbbbbbbbbbb-22222222.out", "2026-10-22 09:00:00")  # older: a legacy PC build
    lines = run(tmp_path).stdout.splitlines()
    assert "map builds on the PC: 2" in lines  # the new-form build once, plus the one legacy out
    assert "map builds on GitHub: 0" in lines


def test_row9_outs_with_no_place_line_in_the_window_are_all_legacy(tmp_path):
    write_log(tmp_path, ["2026-10-23 10:00:00 | i300 | pushed | suite 3 min"])  # no map-build line at all
    write_build(tmp_path, "build-aaaaaaaaaaaa-11111111.out", "2026-10-23 10:00:30")
    write_build(tmp_path, "build-bbbbbbbbbbbb-22222222.out", "2026-10-23 11:00:00")
    lines = run(tmp_path).stdout.splitlines()
    assert "map builds on the PC: 2" in lines
    assert "map builds on GitHub: 0" in lines


def test_row10_a_newer_out_with_no_log_line_is_never_counted_alone(tmp_path):
    write_log(tmp_path, ["2026-10-23 10:00:00 | map-run | map-build on github | aaaaaaaaaaaa | 3021 tests"])
    write_build(tmp_path, "build-cccccccccccc-33333333.out", "2026-10-23 12:00:00")  # after the first line
    lines = run(tmp_path).stdout.splitlines()
    assert "map builds on the PC: 0" in lines
    assert "map builds on GitHub: 1" in lines


def test_row11_a_landing_whose_map_build_was_coalesced_prints_coalesced(tmp_path):
    write_log(tmp_path, [
        "2026-10-23 09:00:00 | i400 | pushed | suite 3 min",
        "2026-10-23 09:01:00 | map-run | map-build skipped (coalesced) | 444444444444 | newer landing wins",
        "2026-10-23 10:00:00 | i401 | pushed | suite 3 min",  # no map-build line after it: none
    ])
    proc = run(tmp_path, "--last", "2")
    lines = proc.stdout.splitlines()
    assert "  2026-10-23 09:00:00 i400 -> coalesced" in lines
    assert "  2026-10-23 10:00:00 i401 -> none" in lines
    assert "map builds on the PC: 0" in lines  # a skipped build is no build
    assert "map builds on GitHub: 0" in lines


def test_row11_the_line_map_run_writes_for_a_coalesced_build_prints_coalesced(tmp_path):
    write_log(tmp_path, [
        "2026-10-23 09:00:00 | i400 | pushed | suite 3 min",
        "2026-10-23 09:01:00 | map-run | map-build on skip (coalesced)",
    ])
    lines = run(tmp_path, "--last", "1").stdout.splitlines()
    assert "  2026-10-23 09:00:00 i400 -> coalesced" in lines
    assert "map builds on the PC: 0" in lines
