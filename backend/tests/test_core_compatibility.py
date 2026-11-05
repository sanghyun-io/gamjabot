import json
import shutil
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from app.core.migrate import REVISIONS, migrate_database
from app.owner_cli import capability


FIXTURES = Path(__file__).parent / "fixtures" / "cli_core"
LEGACY_DATABASE = FIXTURES / "gamja_bot.db"
LEGACY_REPORT_STATE = FIXTURES / "reporting" / "state.json"
CAPABILITY_HEX = "ab" * 32


def _write_private(path: Path, value: dict) -> bytes:
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)
    return path.read_bytes()


def test_legacy_endpoint_is_read_only_compatible(tmp_path):
    endpoint_path = tmp_path / "owner-cli.json"
    original = _write_private(
        endpoint_path,
        {"base_url": "http://127.0.0.1:41001", "capability": CAPABILITY_HEX},
    )

    assert capability(endpoint_path) == {
        "base_url": "http://127.0.0.1:41001",
        "capability": CAPABILITY_HEX,
    }
    assert endpoint_path.read_bytes() == original
    assert endpoint_path.stat().st_mode & 0o777 == 0o600

    endpoint_path.chmod(0o644)
    with pytest.raises(RuntimeError, match="capability is unavailable"):
        capability(endpoint_path)

    endpoint_path.chmod(0o600)
    current = {
        "version": 1,
        "api_url": "http://127.0.0.1:41001",
        "control_url": "http://127.0.0.1:41002",
        "capability": CAPABILITY_HEX,
        "core_version": 1,
        "engine_version": 1,
    }
    _write_private(endpoint_path, current)
    assert capability(endpoint_path) == {**current, "base_url": current["api_url"]}
    explicit_default_port = {**current, "api_url": "http://127.0.0.1:80"}
    _write_private(endpoint_path, explicit_default_port)
    assert capability(endpoint_path) == {
        **explicit_default_port,
        "base_url": explicit_default_port["api_url"],
    }
    for invalid_url in (
        "http://127.0.0.1:41001/",
        "http://127.0.0.1",
        "http://127.0.0.1:041001",
    ):
        _write_private(endpoint_path, {**current, "api_url": invalid_url})
        with pytest.raises(RuntimeError, match="capability is unavailable"):
            capability(endpoint_path)
    _write_private(endpoint_path, {**current, "unknown": "rejected"})
    with pytest.raises(RuntimeError, match="capability is unavailable"):
        capability(endpoint_path)


def test_legacy_database_opens_without_copy(tmp_path, monkeypatch):
    app_data = tmp_path / "appdata"
    app_data.mkdir()
    database_path = app_data / "gamja_bot.db"
    shutil.copyfile(LEGACY_DATABASE, database_path)
    database_path.chmod(0o600)
    monkeypatch.setenv("GAMJA_APP_DATA_DIR", str(app_data))
    inode = database_path.stat().st_ino

    engine = create_engine(f"sqlite:///{database_path}")
    migrate_database(engine)
    engine.dispose()

    assert database_path.stat().st_ino == inode
    backups = list((app_data / "backups").glob("*.sqlite"))
    assert len(backups) == 1 and backups[0].stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(database_path) as db:
        assert db.execute("SELECT version FROM schema_version ORDER BY version").fetchall() == [
            (version,) for version in range(1, len(REVISIONS) + 1)
        ]
        assert db.execute("SELECT email, username FROM users").fetchall() == [
            ("fixture-owner@example.com", "fixture-owner")
        ]
        assert db.execute("SELECT title FROM conversations").fetchall() == [("Fixture room",)]
        assert db.execute("SELECT COUNT(*) FROM messages").fetchone() == (0,)
        assert db.execute("SELECT COUNT(*) FROM room_events").fetchone() == (0,)
        assert db.execute("SELECT COUNT(*) FROM provider_connections").fetchone() == (0,)
        assert db.execute("SELECT COUNT(*) FROM remembered_login").fetchone() == (0,)


def test_legacy_report_state_is_sanitized():
    decoded = json.loads(LEGACY_REPORT_STATE.read_bytes())
    assert decoded["consent_generation"] == 4
    assert decoded["auto_reporting_enabled"] is True
    assert {report["state"] for report in decoded["reports"]} == {"pending", "accepted"}
    assert {report["draft"]["envelope"]["mode"] for report in decoded["reports"]} == {
        "manual",
        "automatic",
    }
    assert all(report["draft"]["envelope"].get("description") is None for report in decoded["reports"])
    assert any(report["attempts"] for report in decoded["reports"])
    assert any(report.get("receipt") for report in decoded["reports"])
