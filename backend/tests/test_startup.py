from importlib import import_module

import pytest

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect


def test_import_and_startup(sqlite_url, tmp_path, monkeypatch):
    monkeypatch.setenv("GAMJA_LOCAL_SESSION_TOKEN", "test-only-token")
    main = import_module("app.main")
    assert not (tmp_path / "startup.sqlite").exists()

    app = main.create_app(database_url=sqlite_url)
    with TestClient(app, headers={"X-Gamja-Session": "test-only-token"}) as client:
        assert client.get("/health").json() == {"status": "healthy"}

        from app.api.routes.auth import get_current_user
        from app.models.models import User

        app.dependency_overrides[get_current_user] = lambda: User(id=1)
        assert client.get("/api/v1/agents/").json() == []

    engine = create_engine(sqlite_url)
    assert {"users", "agents", "projects"} <= set(inspect(engine).get_table_names())
    engine.dispose()


def test_startup_refuses_unrecognized_sqlite_schema_without_changing_it(sqlite_url, monkeypatch):
    monkeypatch.setenv("GAMJA_LOCAL_SESSION_TOKEN", "test-only-token")
    engine = create_engine(sqlite_url)
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE legacy_data (id INTEGER PRIMARY KEY)")
    engine.dispose()

    from app.main import create_app

    with pytest.raises(RuntimeError, match="Unrecognized SQLite schema"):
        with TestClient(create_app(database_url=sqlite_url), headers={"X-Gamja-Session": "test-only-token"}):
            pass

    engine = create_engine(sqlite_url)
    assert inspect(engine).get_table_names() == ["legacy_data"]
    engine.dispose()
