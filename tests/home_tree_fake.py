"""A fake home's answer to `gh api repos/<home>/git/trees/<sha>?recursive=1` (#148): the blobs of ``files``
({path: bytes}) with the sha git would give each, shared by the core-update tests' fake `gh` runners."""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace


def blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def tree_reply(files: dict[str, bytes], truncated: bool = False) -> SimpleNamespace:
    body = {"truncated": truncated,
            "tree": [{"path": path, "type": "blob", "sha": blob_sha(data)} for path, data in sorted(files.items())]}
    return SimpleNamespace(returncode=0, stdout=json.dumps(body), stderr="")
