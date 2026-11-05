"""Business diagnostic must report the selected Core's profile, not another DB."""
import json
import sqlite3

import pytest

from app import owner_cli
from app.services.diagnostic_detail import make_diagnostic


def seed(path, correlation):
    path.parent.mkdir(parents=True, exist_ok=True)
    detail = make_diagnostic(RuntimeError('private fixture text'), stage='context_build',
        failure_code='invalid_configuration', room_id=1, agent_id=1,
        connection_id=1, trigger_event_id=None, action_id=None)
    detail['correlation_id'] = correlation
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE room_events(room_id,actor_id,kind,payload,created_at)')
        db.execute('INSERT INTO room_events VALUES(1,1,?,?,?)',
            ('failure', json.dumps({'diagnostic': detail}), '2026-10-11T00:00:00'))


@pytest.fixture
def profiles(tmp_path, monkeypatch):
    owner = tmp_path / 'canonical/gamja_bot.db'
    selected = tmp_path / 'selected/gamja_bot.db'
    seed(owner, 'a' * 32)
    seed(selected, 'b' * 32)
    monkeypatch.setattr(owner_cli.desktop_diagnostics, 'DEFAULT_DATABASE', owner)
    monkeypatch.setattr(owner_cli.desktop_diagnostics, 'app_status', lambda path: {})
    monkeypatch.setattr(owner_cli.desktop_diagnostics, 'database_status', lambda path: {'present': path.is_file()})
    return owner, selected


def test_selected_profile_returns_its_failure_not_canonical_failure(profiles, monkeypatch, capsys):
    owner, selected = profiles
    original_owner = owner.read_bytes()
    monkeypatch.setenv('GAMJA_APP_DATA_DIR', str(selected.parent))
    owner_cli.diagnostics()
    output = json.loads(capsys.readouterr().out)
    assert [row['correlation_id'] for row in output['error_details']] == ['b' * 32]
    assert 'private fixture text' not in json.dumps(output)
    assert owner.read_bytes() == original_owner


def test_missing_selected_database_does_not_fall_back_to_owner(profiles, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('GAMJA_APP_DATA_DIR', str(tmp_path / 'missing'))
    owner_cli.diagnostics()
    output = json.loads(capsys.readouterr().out)
    assert output['database_snapshot'] == {'present': False}
    assert output['error_details'] == []


def test_unconfigured_legacy_cli_keeps_canonical_default(profiles, monkeypatch, capsys):
    monkeypatch.delenv('GAMJA_APP_DATA_DIR', raising=False)
    owner_cli.diagnostics()
    output = json.loads(capsys.readouterr().out)
    assert [row['correlation_id'] for row in output['error_details']] == ['a' * 32]


def test_correlation_fallback_searches_only_selected_profile(profiles, monkeypatch, capsys):
    owner, selected = profiles
    monkeypatch.setenv('GAMJA_APP_DATA_DIR', str(selected.parent))
    owner_cli.diagnostics('a' * 32)
    output = json.loads(capsys.readouterr().out)
    assert output['error_details'] == []


@pytest.mark.parametrize("inherited_profile", [None, "canonical"])
def test_main_explicit_capability_profile_overrides_shell_environment(
        profiles, monkeypatch, capsys, inherited_profile):
    owner, selected = profiles
    original_owner = owner.read_bytes()
    if inherited_profile is None:
        monkeypatch.delenv("GAMJA_APP_DATA_DIR", raising=False)
    else:
        monkeypatch.setenv("GAMJA_APP_DATA_DIR", str(owner.parent))
    selected_capability = selected.parent / "owner-cli.json"
    observed = []
    def fake_capability(path):
        observed.append(path)
        return {"base_url": "http://127.0.0.1:1", "capability": "0" * 64}
    monkeypatch.setattr(owner_cli, "capability", fake_capability)
    monkeypatch.setattr(owner_cli.sys, "argv", ["gamja-engine",
        "--capability-file", str(selected_capability), "diagnostic"])
    owner_cli.main()
    output = json.loads(capsys.readouterr().out)
    assert observed == [selected_capability]
    assert [row["correlation_id"] for row in output["error_details"]] == ["b" * 32]
    assert owner.read_bytes() == original_owner
