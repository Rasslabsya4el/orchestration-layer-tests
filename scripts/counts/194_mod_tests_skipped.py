"""Count landings on main that changed a mod's files while the merge ran no test of that mod.

Usage: python -I scripts/counts/<issue>_mod_tests_skipped.py <repo> <since YYYY-MM-DD> [until YYYY-MM-DD]
A landing = a first-parent merge commit on main; it changed a mod when its diff touches mods/.
It ran the mod's tests when a merge.log line naming its branch also names "mod tests".
Prints: landings, landings that changed a mod, of them with no mod test.
"""
import os
import re
import subprocess
import sys
from pathlib import Path


NO_WINDOW = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW: no console window per child process


def git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, check=True,
                          creationflags=NO_WINDOW).stdout


def main():
    repo, since = sys.argv[1], sys.argv[2]
    until = sys.argv[3] if len(sys.argv) > 3 else None
    rng = ["--since", since] + (["--until", until] if until else [])
    log_path = Path(repo) / "var" / "agentops" / "merge.log"
    log = log_path.read_text(encoding="utf-8", errors="replace").splitlines() if log_path.exists() else []
    tested = {b for line in log if "mod tests" in line for b in re.findall(r"[\w./-]+", line.split("|")[1] if "|" in line else "")}
    merges = git(repo, "log", "--first-parent", "--merges", "--format=%h %s", *rng, "main").splitlines()
    touched = skipped = 0
    for row in merges:
        sha, _, subject = row.partition(" ")
        files = git(repo, "diff", "--name-only", f"{sha}^1", sha).splitlines()
        if not any(f.startswith("mods/") for f in files):
            continue
        touched += 1
        m = re.search(r"Merge branch '([^']+)'", subject)
        if not (m and m.group(1) in tested):
            skipped += 1
    print(f"landings {len(merges)}, changed a mod {touched}, with no mod test {skipped}")


if __name__ == "__main__":
    main()
