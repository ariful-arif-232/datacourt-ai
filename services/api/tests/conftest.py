"""Test configuration.

Tests run against a real PostgreSQL database (TEST_DATABASE_URL), because the job queue
relies on `FOR UPDATE SKIP LOCKED` and JSONB. Environment is set before any datacourt
module is imported so the cached settings pick it up.
"""

from __future__ import annotations

import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="dc-test-objects-")
os.environ["ENVIRONMENT"] = "test"
os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://postgres@127.0.0.1:5432/datacourt_test"
)
os.environ["LOCAL_STORAGE_ROOT"] = _tmp
os.environ["STORAGE_BACKEND"] = "local"
os.environ["PUBLIC_API_URL"] = "http://testserver"
os.environ["ALLOWED_ORIGINS"] = "http://localhost:3000"
os.environ["RUN_EMBEDDED_WORKER"] = "false"
os.environ.pop("GEMINI_API_KEY", None)

import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from sqlalchemy import text  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def database():
    from datacourt.db.session import get_engine

    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    cfg = Config(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    cfg.set_main_option(
        "script_location", os.path.join(os.path.dirname(__file__), "..", "datacourt", "migrations")
    )
    command.upgrade(cfg, "head")
    yield engine


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from datacourt.main import create_app

    with TestClient(create_app()) as c:
        c.headers.update({"x-datacourt-csrf": "1"})
        yield c


def register(client, email: str, name: str = "Tester") -> dict:
    r = client.post(
        "/api/v1/auth/register", json={"email": email, "password": "correct-horse-battery", "name": name}
    )
    assert r.status_code == 200, r.text
    return r.json()
