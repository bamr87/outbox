#!/usr/bin/env python3
"""Copy a sibling repo's working tree for the e2e proof, or fingerprint it.

The proof tests what is on disk, not the last commit: tracked files plus
untracked, non-ignored ones, so a fix that is not committed yet is what runs.
Deleted files, directories standing in for tracked symlinks, and submodule
contents are skipped, as `git archive` skips them. E2E_SNAPSHOT=head copies
HEAD instead. The repo itself is only read.

    python3 e2e/snapshot.py copy REPO DEST [PATH ...]
    python3 e2e/snapshot.py id REPO
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path


def _git(repo: Path, *args: str) -> bytes:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True).stdout


def working_files(repo: Path, paths: tuple[str, ...] = ()) -> list[str]:
    staged = _git(repo, "ls-files", "-s", "-z", "--", *paths).split(b"\0")
    submodules = {e.split(b"\t", 1)[1].decode() for e in staged if e.startswith(b"160000 ")}
    seen: dict[str, None] = {}
    for raw in _git(repo, "ls-files", "-co", "--exclude-standard", "-z", "--", *paths).split(b"\0"):
        rel = raw.decode()
        if not rel or rel in submodules:
            continue
        src = repo / rel
        if src.is_symlink() or src.is_file():
            seen.setdefault(rel, None)
    return list(seen)


def copy(repo: Path, dest: Path, paths: tuple[str, ...] = ()) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    if os.environ.get("E2E_SNAPSHOT") == "head":
        archive = _git(repo, "archive", "HEAD", *paths)
        subprocess.run(["tar", "-x", "-C", str(dest)], input=archive, check=True)
        return dest
    for rel in working_files(repo, paths):
        src, dst = repo / rel, dest / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_symlink():
            os.symlink(os.readlink(src), dst)
        else:
            shutil.copy2(src, dst)
    return dest


def state(repo: Path) -> dict[str, object]:
    """HEAD plus what differs from it on disk; `id` changes whenever the copy would."""
    head = _git(repo, "rev-parse", "HEAD").decode().strip()
    changed = [line[3:] for line in _git(repo, "status", "--porcelain").decode().splitlines()]
    digest = hashlib.sha1(head.encode())
    if os.environ.get("E2E_SNAPSHOT") != "head":
        digest.update(_git(repo, "diff", "HEAD", "--binary"))
        for rel in _git(repo, "ls-files", "-o", "--exclude-standard", "-z").split(b"\0"):
            path = repo / rel.decode()
            if rel and path.is_file():
                digest.update(rel + b"\0" + hashlib.sha1(path.read_bytes()).digest())
    return {"head": head[:12], "uncommitted": changed, "id": digest.hexdigest()[:16]}


def main(argv: list[str]) -> int:
    if len(argv) >= 3 and argv[0] == "copy":
        copy(Path(argv[1]).resolve(), Path(argv[2]).resolve(), tuple(argv[3:]))
        return 0
    if len(argv) == 2 and argv[0] == "id":
        print(state(Path(argv[1]).resolve())["id"])
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
