"""Shared fixtures for research and retained infrastructure tests."""

import os

import pytest

from researchx.storage.database import Database, bind_database


@pytest.fixture(autouse=True)
async def postgres_scope(monkeypatch, tmp_path):
    """A real, explicitly configured test database; no test-only SQLite backend."""
    url = os.environ.get("RESEARCHX_TEST_DATABASE_URL")
    if not url:
        pytest.fail("Set RESEARCHX_TEST_DATABASE_URL to a disposable migrated PostgreSQL database")
    from sqlalchemy.engine import make_url
    name = make_url(url).database or ""
    if not (name.endswith("_test") or name.startswith("test_")):
        pytest.fail("Use a disposable database named test_* or *_test")
    monkeypatch.setenv("RESEARCHX_CONTENT_BACKEND", "local")
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.fail.Keyring")
    monkeypatch.delenv("RESEARCHX_CONTENT_ROOT", raising=False)
    monkeypatch.setenv("RESEARCHX_DATABASE_URL", url)
    monkeypatch.setenv("RESEARCHX_DATA_DIR", str(tmp_path / "data"))
    database = Database(url)
    with bind_database(database):
        try:
            yield database
        finally:
            await database.close()
