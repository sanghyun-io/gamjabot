"""Real owner CLI subprocess -> loopback HTTP -> disposable canonical workspace API."""

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from fastapi.testclient import TestClient

from app.api.routes.auth import get_password_hash
from app.main import RuntimeConfig, create_app
from app.models.collaboration import (
    ProjectRoomTemplate,
    ProjectRoomTemplateMember,
    ProviderConnection,
)
from app.models.models import Agent, Project, ProjectAgent, User


BACKEND = Path(__file__).resolve().parents[1]


def test_owner_cli_template_and_workspace_round_trip_over_real_http(sqlite_url, tmp_path):
    credential = "a" * 64
    app = create_app(runtime_config=RuntimeConfig(
        database_url=sqlite_url,
        cli_capability_hash=hashlib.sha256(credential.encode()).hexdigest(),
    ))
    with TestClient(app) as api:
        with app.state.session_factory() as db:
            owner = User(
                email="workspace-cli@example.com",
                username="workspace-cli",
                hashed_password=get_password_hash("synthetic-password"),
            )
            db.add(owner)
            db.flush()
            connection = ProviderConnection(
                user_id=owner.id,
                kind="openai",
                name="CLI source",
                default_model_id="gpt-test",
                secret_ref="sentinel-existing-secret-reference",
                enabled=True,
                auth_required=True,
                capabilities={"declared": {"tools": True}, "verified": {}},
            )
            typed = ProviderConnection(
                user_id=owner.id, kind="compatible", name="CLI typed source",
                base_url="https://openrouter.ai/api/v1", default_model_id="openrouter/test",
                secret_ref="sentinel-typed-secret-reference", enabled=True,
                auth_required=True, capabilities={"declared": {"vision": False},
                                                  "verified": {"sentinel": True}},
                readiness_revision=2, provider_id="openrouter",
                protocol_id="openai_chat_completions", auth_method_id="api_key",
                public_config_version=1, public_config={}, auth_grant_revision=1,
                credential_state="present",
            )
            db.add_all([connection, typed])
            db.flush()
            agent = Agent(
                user_id=owner.id,
                name="CLI Agent",
                description="fixture",
                system_prompt="Work on the fixture",
                model="gpt-test",
                connection_id=connection.id,
                model_id="gpt-test",
            )
            project = Project(user_id=owner.id, name="CLI Project", description="fixture")
            db.add_all([agent, project])
            db.flush()
            db.add(ProjectAgent(project_id=project.id, agent_id=agent.id))
            db.commit()
            source_connection_ids = {connection.id, typed.id}

        exchanges = []

        class Handler(BaseHTTPRequestHandler):
            def handle_request(self):
                response = api.request(
                    self.command,
                    self.path,
                    headers=dict(self.headers),
                    content=self.rfile.read(int(self.headers.get("Content-Length", 0))),
                )
                exchanges.append((self.command, self.path, response.status_code))
                self.send_response(response.status_code)
                self.send_header("Content-Length", str(len(response.content)))
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(response.content)

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = handle_request

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            endpoint = tmp_path / "owner-cli.json"
            endpoint.write_text(json.dumps({
                "version": 1,
                "api_url": f"http://127.0.0.1:{port}",
                "control_url": f"http://127.0.0.1:{port}",
                "capability": credential,
                "core_version": 1,
                "engine_version": 1,
            }))
            endpoint.chmod(0o600)

            def run(rows):
                source = tmp_path / "commands.ndjson"
                source.write_text("\n".join(json.dumps(row) for row in rows))
                environment = os.environ.copy()
                environment["PYTHONPATH"] = str(BACKEND)
                result = subprocess.run(
                    [sys.executable, "-m", "app.owner_cli", "--capability-file", str(endpoint),
                     "--batch", str(source)],
                    cwd=BACKEND,
                    env=environment,
                    input="synthetic-password\n",
                    capture_output=True,
                    text=True,
                    timeout=20,
                    check=False,
                )
                combined = result.stdout + result.stderr
                assert result.returncode == 0, (combined, exchanges)
                assert "synthetic-password" not in combined
                assert "sentinel-existing-secret-reference" not in combined
                assert credential not in combined
                return [json.loads(line) for line in result.stdout.splitlines()]

            login = {"command": ["login", "--secret-stdin"],
                     "input": {"email": "workspace-cli@example.com"}}
            blueprint_path = tmp_path / "workspace.json"
            created = run([
                login,
                {"command": ["template", "add", "1"], "input": {
                    "name": "CLI team",
                    "room_title": "CLI room",
                    "agent_ids": [1],
                    "max_rounds": 3,
                    "max_tool_calls": 12,
                    "max_executor_seconds": 180,
                }},
                {"command": ["workspace", "export", "--output", str(blueprint_path)]},
            ])
            assert created[1]["name"] == "CLI team"
            assert stat.S_IMODE(blueprint_path.stat().st_mode) == 0o600
            rendered = blueprint_path.read_text()
            assert "sentinel-existing-secret-reference" not in rendered
            assert "sentinel-typed-secret-reference" not in rendered
            assert "secret_ref" not in rendered
            assert json.loads(rendered)["version"] == 2

            previewed = run([
                login,
                {"command": ["workspace", "preview", "--file", str(blueprint_path)]},
            ])[-1]
            assert previewed["counts"] == {
                "connections": 2,
                "agents": 1,
                "projects": 1,
                "project_assignments": 1,
                "room_templates": 1,
                "template_members": 1,
            }
            applied = run([
                login,
                {"command": ["workspace", "apply", previewed["plan_hash"],
                             "--file", str(blueprint_path)]},
            ])[-1]
            assert applied["counts"] == previewed["counts"]
            assert applied["credentials_unset"] == 2

            with app.state.session_factory() as db:
                assert db.query(ProviderConnection).count() == 4
                imported = db.query(ProviderConnection).filter(
                    ProviderConnection.id.not_in(source_connection_ids),
                ).order_by(ProviderConnection.id).all()
                assert [row.secret_ref for row in imported] == [None, None]
                assert [row.provider_id for row in imported] == ["openai", "openrouter"]
                assert imported[0].capabilities == {"declared": {"tools": True}, "verified": {}}
                assert imported[1].capabilities == {"declared": {"vision": False}, "verified": {}}
                assert db.query(Agent).count() == 2
                assert db.query(Project).count() == 2
                assert db.query(ProjectAgent).count() == 2
                assert db.query(ProjectRoomTemplate).count() == 2
                assert db.query(ProjectRoomTemplateMember).count() == 2
                imported_template = db.query(ProjectRoomTemplate).filter(
                    ProjectRoomTemplate.id != created[1]["id"]).one()
                assert (imported_template.max_rounds, imported_template.max_tool_calls,
                        imported_template.max_executor_seconds) == (3, 12, 180)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            assert not thread.is_alive()
