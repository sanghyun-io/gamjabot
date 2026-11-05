#!/usr/bin/env python3
"""Regenerate synthetic CLI-core compatibility fixtures without owner data."""

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[3]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


def _envelope(report_id, mode, occurred_at, component, stage, code):
    return {
        "schema_version": 1,
        "report_id": report_id,
        "mode": mode,
        "app_version": "1.0.0",
        "build_id": "fixture",
        "os": "macos",
        "os_version": "14.0",
        "arch": "aarch64",
        "occurred_at": occurred_at,
        "component": component,
        "stage": stage,
        "code": code,
        "occurrences": 1,
        "transitions": [],
    }


def _report(envelope, state, attempts, attempt_times, next_attempt_at, receipt=None):
    return {
        "draft": {
            "envelope": envelope,
            "body": json.dumps(envelope, separators=(",", ":")),
        },
        "state": state,
        "attempts": attempts,
        "attempt_times": attempt_times,
        "next_attempt_at": next_attempt_at,
        "receipt": receipt,
    }


def report_state():
    manual = _envelope(
        "2" * 32, "manual", "2026-09-30T00:00:00Z",
        "backend", "startup", "backend_init_failed",
    )
    pending = _envelope(
        "3" * 32, "automatic", "2026-09-30T00:01:00Z",
        "backend", "startup", "backend_not_ready",
    )
    accepted = _envelope(
        "4" * 32, "automatic", "2026-09-30T00:02:00Z",
        "host", "runtime", "unexpected_failure",
    )
    return {
        "events": [{
            "event_id": "1" * 32,
            "occurred_at": "2026-09-30T00:00:00Z",
            "component": "backend",
            "stage": "startup",
            "code": "backend_init_failed",
            "app_version": "1.0.0",
            "build_id": "fixture",
            "os": "macos",
            "os_version": "14.0",
            "arch": "aarch64",
        }],
        "reports": [
            _report(manual, "pending", 2, [1790726400, 1790726460], 1790726580),
            _report(pending, "pending", 1, [1790726460], 1790726520),
            _report(
                accepted,
                "accepted",
                1,
                [1790726520],
                0,
                {
                    "report_id": "4" * 32,
                    "receipt_id": "5" * 32,
                    "accepted_at": "2026-09-30T00:03:00Z",
                },
            ),
        ],
        "auto_reporting_enabled": True,
        "consent_generation": 4,
        "discarded": 2,
        "discarded_expired": 1,
        "discarded_capacity": 1,
        "auto_attempt_times": [1790726460],
    }


def generate(output: Path):
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="gamja-cli-core-fixture-") as scratch:
        temporary = Path(scratch)
        os.environ["GAMJA_APP_DATA_DIR"] = str(temporary / "appdata")
        os.environ["GAMJA_LOCAL_SESSION_TOKEN"] = "fixture-local-session"

        from fastapi.testclient import TestClient
        from app.main import create_app

        database = temporary / "gamja_bot.db"
        local = {"X-Gamja-Session": "fixture-local-session"}
        with TestClient(create_app(database_url=f"sqlite:///{database}"), headers=local) as client:
            response = client.post("/api/v1/auth/register", json={
                "email": "fixture-owner@example.com",
                "username": "fixture-owner",
                "password": "fixture-password",
            })
            response.raise_for_status()
            response = client.post("/api/v1/auth/login", json={
                "email": "fixture-owner@example.com",
                "password": "fixture-password",
                "remember_device": False,
                "preserve_remembered_login": True,
            })
            response.raise_for_status()
            authenticated = {
                **local,
                "Authorization": f"Bearer {response.json()['access_token']}",
            }
            response = client.post(
                "/api/v1/projects/",
                headers=authenticated,
                json={"name": "Fixture project", "description": None},
            )
            response.raise_for_status()
            response = client.post(
                "/api/v1/conversations/",
                headers=authenticated,
                json={"project_id": response.json()["id"], "title": "Fixture room"},
            )
            response.raise_for_status()

        shutil.copyfile(database, output / "gamja_bot.db")
        (output / "gamja_bot.db").chmod(0o600)

    reporting = output / "reporting"
    reporting.mkdir(mode=0o700, exist_ok=True)
    state_path = reporting / "state.json"
    state_path.write_text(json.dumps(report_state(), indent=2) + "\n", encoding="utf-8")
    state_path.chmod(0o600)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()
    generate(args.output.resolve())


if __name__ == "__main__":
    main()
