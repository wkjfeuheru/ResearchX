"""Shared Web transport fixture."""

from tests.test_web.test_app import workspace as workspace

import pytest


@pytest.fixture(autouse=True)
def private_file_credentials(monkeypatch, tmp_path):
    """Fake-model Web tests must not open the user's desktop keyring or D-Bus."""
    from pathlib import Path

    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    import researchx.auth.storage as storage

    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.fail.Keyring")
    monkeypatch.setattr(storage, "_keyring_checked", True)
    monkeypatch.setattr(storage, "_keyring_usable", False)
