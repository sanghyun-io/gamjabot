"""SQLite migration runner with an explicit write transaction and preflight snapshot."""

import importlib
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

from sqlalchemy import event
from sqlalchemy.engine import Engine


REVISIONS = ("0001_collaboration", "0002_room_state", "0003_local_event_device", "0004_provider_connections", "0005_agent_connection", "0006_scheduler", "0007_action_call_scope", "0008_tool_consent", "0009_action_scope", "0010_desktop_lease_owner", "0011_remembered_login", "0012_room_drafts", "0013_room_read_state", "0014_project_room_templates", "0015_project_archive", "0016_room_launch_requests", "0017_room_pins", "0018_connection_test_history", "0019_request_retry_records", "0020_starter_requests", "0021_request_finalize_records", "0022_remembered_login_rotation", "0023_provider_connections_v1", "0024_connection_model_cache", "0025_connection_auth_operations", "0026_action_reviews", "0027_room_results", "0028_room_memory", "0029_agent_avatar", "0030_teammate_handoffs", "0031_bot_profile", "0032_bot_memory", "0033_bot_messages", "0034_execution_scopes", "0035_bot_batches", "0036_bot_creation", "0037_primary_checkins", "0038_cross_chat_memory", "0039_owner_skills", "0040_owner_routines", "0041_skill_packages", "0042_mcp", "0043_image_consent", "0044_browser_demonstrations", "0045_routine_webhooks", "0046_room_reply", "0047_mcp_drafts", "0048_pages", "0049_bot_directory", "0050_bot_clone", "0051_bot_templates", "0052_template_publications", "0053_vault_accounts", "0054_vault_logins", "0055_vault_contacts", "0056_vault_cards", "0057_voice_calls", "0058_shared_bot_foundation", "0059_shared_text_runtime", "0060_shared_private_memory", "0061_shared_connector", "0062_shared_connector_clear_all", "0063_shared_connector_recovery", "0064_slack", "0065_local_notifications", "0066_slack_queue_index", "0067_routine_reference_fence", "0068_library_pins", "0069_teammate_delivery_receipts")


def _backup_directory() -> Path:
    configured = os.environ.get("GAMJA_APP_DATA_DIR")
    if configured:
        root = Path(configured)
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support" / "Gamja Bot"
    elif sys.platform == "win32":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "Gamja Bot"
    else:
        root = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "gamja-bot"
    return root / "backups"


def _backup(path: Path) -> Path:
    directory = _backup_directory()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".migration-", suffix=".sqlite", dir=directory)
    os.close(fd)
    destination = directory / f"{path.stem}-{uuid4().hex}.sqlite"
    try:
        with sqlite3.connect(path) as source, sqlite3.connect(temporary) as copy:
            source.backup(copy)
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return destination


def _foreign_keys_on(dbapi_connection, connection_record):
    dbapi_connection.execute("PRAGMA foreign_keys=ON")


def _validate_schema(raw, version: int) -> None:
    tables = {row[0] for row in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for revision_number in range(1, version + 1):
        revision = importlib.import_module(f"app.core.migrations.{REVISIONS[revision_number - 1]}")
        for table, columns in revision.REQUIRED_COLUMNS.items():
            if table not in tables:
                raise RuntimeError(f"Incomplete schema: missing {table} from revision {revision_number}")
            present = {row[1] for row in raw.execute(f'PRAGMA table_info("{table}")')}
            missing = columns - present
            if missing:
                raise RuntimeError(f"Incomplete schema: missing {table} columns {sorted(missing)}")
        for name, (table, columns) in getattr(revision, "REQUIRED_UNIQUE_INDEXES", {}).items():
            indexes = {row[1]: row for row in raw.execute(f'PRAGMA index_list("{table}")')}
            index = indexes.get(name)
            actual = tuple(row[2] for row in raw.execute(f'PRAGMA index_info("{name}")'))
            if index is None or not index[2] or index[4] or actual != columns:
                raise RuntimeError(f"Incomplete schema: missing or invalid unique index {name}")


def migrate_database(engine: Engine) -> None:
    if engine.dialect.name != "sqlite":
        raise RuntimeError("Desktop migrations require SQLite")
    if not event.contains(engine, "connect", _foreign_keys_on):
        event.listen(engine, "connect", _foreign_keys_on)

    path = Path(engine.url.database) if engine.url.database not in (None, ":memory:") else None
    raw = engine.raw_connection()
    try:
        raw.execute("PRAGMA foreign_keys=ON")
        tables = {row[0] for row in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "schema_version" in tables:
            history = [row[0] for row in raw.execute("SELECT version FROM schema_version ORDER BY version")]
            version = history[-1] if history else 0
        else:
            history = []
            version = 0
        if version > len(REVISIONS):
            raise RuntimeError(f"Database schema version {version} is newer than this app")
        if history != list(range(1, version + 1)):
            raise RuntimeError("Incomplete schema version history")
        if version:
            _validate_schema(raw, version)
        if version == len(REVISIONS):
            return

        if path and path.exists() and path.stat().st_size:
            _backup(path)

        # sqlite3 executes DDL transactionally only after an explicit BEGIN.
        raw.execute("BEGIN IMMEDIATE")
        try:
            raw.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, applied_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)")
            for revision_number in range(version + 1, len(REVISIONS) + 1):
                revision = importlib.import_module(f"app.core.migrations.{REVISIONS[revision_number - 1]}")
                revision.upgrade(raw)
                raw.execute("INSERT INTO schema_version (version) VALUES (?)", (revision_number,))
            _validate_schema(raw, len(REVISIONS))
            raw.commit()
        except BaseException:
            raw.rollback()
            raise
    finally:
        raw.close()
