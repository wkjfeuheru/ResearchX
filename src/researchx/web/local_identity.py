"""Stable local operator identity. The browser never supplies actor_id or role."""

from __future__ import annotations

from uuid import uuid4

from researchx.config.paths import get_data_dir
from researchx.state.deliveries import TrustedReviewer
from researchx.storage.filesystem import atomic_write_text

LOCAL_ISSUER = "local_workstation"


def local_operator_id() -> str:
    path = get_data_dir() / "local_operator_id"
    if path.is_file():
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    identifier = "local_" + uuid4().hex
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, identifier + "\n")
    return identifier


def local_reviewer() -> TrustedReviewer:
    return TrustedReviewer(local_operator_id(), "researcher", LOCAL_ISSUER)
