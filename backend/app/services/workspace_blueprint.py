"""Canonical non-secret workspace export, deterministic planning, and atomic apply."""

from hashlib import sha256
import json
from urllib.parse import urlsplit

from fastapi import HTTPException
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.collaboration import (
    ProjectRoomTemplate,
    ProjectRoomTemplateMember,
    ProviderConnection,
)
from app.models.models import Agent, Project, ProjectAgent
from app.schemas.provider_catalog import PublicConfigEnvelope
from app.schemas.workspace import BlueprintConnectionV1, BlueprintConnectionV2, WorkspaceBlueprint
from app.services import provider_catalog
from app.services.connections import validate_url


MAX_BLUEPRINT_BYTES = 2 * 1024 * 1024
PUBLIC_CAPABILITIES = ("max_tokens", "temperature", "tools", "vision")
BLUEPRINT_ADAPTER = TypeAdapter(WorkspaceBlueprint)


def _canonical(blueprint: WorkspaceBlueprint) -> bytes:
    return json.dumps(blueprint.model_dump(mode="json"), ensure_ascii=False,
                      sort_keys=True, separators=(",", ":")).encode("utf-8")


def _check_size(blueprint: WorkspaceBlueprint) -> None:
    if len(_canonical(blueprint)) > MAX_BLUEPRINT_BYTES:
        raise HTTPException(413, {"code": "workspace_blueprint_too_large"})


def _validated_url(item) -> str | None:
    try:
        return validate_url(item.provider_kind, item.base_url, item.allow_local_http,
                            item.auth_required)[0]
    except HTTPException:
        raise HTTPException(422, {"code": "workspace_invalid_blueprint"}) from None


def _export_url(row: ProviderConnection) -> tuple[str | None, bool]:
    """Validate legacy storage before placing a URL in a portable blueprint."""
    local_allowed = bool(row.base_url and urlsplit(row.base_url).scheme == "http")
    try:
        return validate_url(row.kind, row.base_url, local_allowed,
                            bool(row.auth_required))[0], local_allowed
    except HTTPException:
        raise HTTPException(409, {"code": "workspace_source_invalid"}) from None


def _validated_public_config(item: BlueprintConnectionV2):
    try:
        return provider_catalog.validate_public_config(item.public_config, metadata_only=True)
    except Exception:
        raise HTTPException(422, {"code": "workspace_invalid_blueprint"}) from None


def _export_public_config(row: ProviderConnection) -> dict:
    try:
        if row.provider_id is None:
            normalized = provider_catalog.normalize_legacy_connection(row)
            envelope = PublicConfigEnvelope(
                version=normalized.public_config_version,
                provider_id=normalized.provider_id,
                protocol_id=normalized.protocol_id,
                auth_method_id=normalized.auth_method_id,
                fields=normalized.public_config,
            )
        else:
            envelope = PublicConfigEnvelope(
                version=row.public_config_version,
                provider_id=row.provider_id,
                protocol_id=row.protocol_id,
                auth_method_id=row.auth_method_id,
                fields=row.public_config,
            )
        validated = provider_catalog.validate_public_config(envelope, metadata_only=True)
        auth_required = validated.auth_method_id != "local_none"
        if bool(row.auth_required) != auth_required:
            raise ValueError()
        if (row.provider_id is not None
                and ((validated.auth_method_id == "local_none")
                     != (row.credential_state == "not_required"))):
            raise ValueError()
        if (row.provider_id is not None
                and row.base_url != provider_catalog.resolve_origin(
                    validated.provider_id, validated.fields, protocol_id=validated.protocol_id)):
            raise ValueError()
        return validated.model_dump(mode="json")
    except Exception:
        raise HTTPException(409, {"code": "workspace_source_invalid"}) from None


def _legacy_kind(provider_id: str) -> str:
    return provider_id if provider_id in {"openai", "anthropic", "gemini", "xai"} else "compatible"


def _needs_credential(item: BlueprintConnectionV1 | BlueprintConnectionV2) -> bool:
    return (item.auth_required if isinstance(item, BlueprintConnectionV1)
            else item.public_config.auth_method_id != "local_none")


def export_blueprint(db: Session, owner_id: int) -> dict:
    connections = db.query(ProviderConnection).filter_by(user_id=owner_id).order_by(
        ProviderConnection.id).all()
    agents = db.query(Agent).filter_by(user_id=owner_id, is_active=True).order_by(Agent.id).all()
    projects = db.query(Project).filter_by(user_id=owner_id, is_active=True).order_by(Project.id).all()
    connection_refs = {row.id: f"connection-{index}" for index, row in enumerate(connections, 1)}
    agent_refs = {row.id: f"agent-{index}" for index, row in enumerate(agents, 1)}
    template_index = 0
    version = 2 if any(row.provider_id is not None for row in connections) else 1
    raw = {"version": version, "connections": [], "agents": [], "projects": []}
    for row in connections:
        declared = (row.capabilities or {}).get("declared", {})
        common = {
            "ref": connection_refs[row.id], "label": row.name,
            "default_model_id": row.default_model_id, "enabled": bool(row.enabled),
            "capabilities": {key: declared[key] for key in PUBLIC_CAPABILITIES
                             if isinstance(declared.get(key), bool)},
        }
        if version == 1:
            base_url, allow_local_http = _export_url(row)
            raw["connections"].append({
                **common, "provider_kind": row.kind, "base_url": base_url,
                "allow_local_http": allow_local_http,
                "auth_required": bool(row.auth_required),
            })
        else:
            raw["connections"].append({**common, "public_config": _export_public_config(row)})
    for row in agents:
        if row.connection_id not in connection_refs:
            raise HTTPException(409, {"code": "workspace_source_invalid"})
        raw["agents"].append({
            "ref": agent_refs[row.id], "name": row.name, "description": row.description,
            "system_prompt": row.system_prompt, "connection_ref": connection_refs[row.connection_id],
            "model_id": row.model_id or row.model, "temperature": row.temperature,
            "max_tokens": row.max_tokens,
        })
    for project in projects:
        assignments = [row[0] for row in db.query(ProjectAgent.agent_id).filter_by(
            project_id=project.id).order_by(ProjectAgent.agent_id).all()]
        if any(agent_id not in agent_refs for agent_id in assignments):
            raise HTTPException(409, {"code": "workspace_source_invalid"})
        templates = []
        rows = db.query(ProjectRoomTemplate).filter_by(
            owner_id=owner_id, project_id=project.id).order_by(ProjectRoomTemplate.id).all()
        for row in rows:
            members = [item[0] for item in db.query(ProjectRoomTemplateMember.agent_id).filter_by(
                template_id=row.id).order_by(ProjectRoomTemplateMember.agent_id).all()]
            if any(agent_id not in agent_refs for agent_id in members):
                raise HTTPException(409, {"code": "workspace_source_invalid"})
            template_index += 1
            templates.append({
                "ref": f"template-{template_index}", "name": row.name,
                "room_title": row.room_title,
                "agent_refs": [agent_refs[agent_id] for agent_id in members],
                "max_rounds": row.max_rounds, "max_tool_calls": row.max_tool_calls,
                "max_executor_seconds": row.max_executor_seconds,
            })
        raw["projects"].append({
            "ref": f"project-{len(raw['projects']) + 1}", "name": project.name,
            "description": project.description,
            "agent_refs": [agent_refs[agent_id] for agent_id in assignments],
            "room_templates": templates,
        })
    try:
        blueprint = BLUEPRINT_ADAPTER.validate_python(raw)
    except ValidationError:
        raise HTTPException(409, {"code": "workspace_source_invalid"}) from None
    _check_size(blueprint)
    return blueprint.model_dump(mode="json")


def _resolved_name(source: str, used: set[str], maximum: int) -> str:
    if source not in used:
        used.add(source)
        return source
    index = 2
    while True:
        suffix = f" (imported {index})"
        candidate = source[:maximum - len(suffix)].rstrip() + suffix
        if candidate not in used:
            used.add(candidate)
            return candidate
        index += 1


def _plan_without_hash(db: Session, owner_id: int, blueprint: WorkspaceBlueprint) -> dict:
    _check_size(blueprint)
    for item in blueprint.connections:
        if isinstance(item, BlueprintConnectionV1):
            _validated_url(item)
        else:
            _validated_public_config(item)
    connection_names = {row[0] for row in db.query(ProviderConnection.name).filter_by(
        user_id=owner_id).all()}
    agent_names = {row[0] for row in db.query(Agent.name).filter_by(user_id=owner_id).all()}
    project_names = {row[0] for row in db.query(Project.name).filter_by(user_id=owner_id).all()}
    connections = [{"ref": item.ref, "source_name": item.label,
                    "target_name": _resolved_name(item.label, connection_names, 100)}
                   for item in blueprint.connections]
    agents = [{"ref": item.ref, "source_name": item.name,
               "target_name": _resolved_name(item.name, agent_names, 100)}
              for item in blueprint.agents]
    projects = []
    templates = []
    for project in blueprint.projects:
        projects.append({"ref": project.ref, "source_name": project.name,
                         "target_name": _resolved_name(project.name, project_names, 100)})
        template_names: set[str] = set()
        for template in project.room_templates:
            templates.append({
                "ref": template.ref, "project_ref": project.ref,
                "source_name": template.name,
                "target_name": _resolved_name(template.name, template_names, 100),
            })
    counts = {
        "connections": len(blueprint.connections), "agents": len(blueprint.agents),
        "projects": len(blueprint.projects),
        "project_assignments": sum(len(item.agent_refs) for item in blueprint.projects),
        "room_templates": sum(len(item.room_templates) for item in blueprint.projects),
        "template_members": sum(len(template.agent_refs) for project in blueprint.projects
                                for template in project.room_templates),
    }
    return {
        "version": 1, "counts": counts, "connections": connections,
        "agents": agents, "projects": projects, "room_templates": templates,
        "warnings": ([{"code": "connections_need_credentials",
                        "count": sum(1 for item in blueprint.connections
                                     if _needs_credential(item))}]
                     if any(_needs_credential(item) for item in blueprint.connections) else []),
    }


def preview(db: Session, owner_id: int, blueprint: WorkspaceBlueprint) -> dict:
    plan = _plan_without_hash(db, owner_id, blueprint)
    digest_input = {"owner_id": owner_id, "blueprint": blueprint.model_dump(mode="json"),
                    "plan": plan}
    plan_hash = sha256(json.dumps(digest_input, ensure_ascii=False, sort_keys=True,
                                  separators=(",", ":")).encode("utf-8")).hexdigest()
    return {**plan, "plan_hash": plan_hash}


def apply(db: Session, owner_id: int, blueprint: WorkspaceBlueprint, plan_hash: str) -> dict:
    # Authentication has already read through this Session. Restart it as a SQLite
    # reserved transaction so plan recheck and every insert share one write boundary.
    db.rollback()
    db.execute(text("BEGIN IMMEDIATE"))
    try:
        plan = preview(db, owner_id, blueprint)
        if plan["plan_hash"] != plan_hash:
            raise HTTPException(409, {"code": "workspace_plan_stale"})
        connection_names = {item["ref"]: item["target_name"] for item in plan["connections"]}
        agent_names = {item["ref"]: item["target_name"] for item in plan["agents"]}
        project_names = {item["ref"]: item["target_name"] for item in plan["projects"]}
        template_names = {item["ref"]: item["target_name"] for item in plan["room_templates"]}
        connection_ids, agent_ids, project_ids = {}, {}, {}
        connection_results, agent_results, project_results, template_results = [], [], [], []
        for item in blueprint.connections:
            if isinstance(item, BlueprintConnectionV1):
                row = ProviderConnection(
                    user_id=owner_id, kind=item.provider_kind, name=connection_names[item.ref],
                    base_url=_validated_url(item), secret_ref=None,
                    default_model_id=item.default_model_id, enabled=item.enabled,
                    auth_required=item.auth_required,
                    capabilities={"declared": dict(item.capabilities), "verified": {}},
                )
            else:
                contract = _validated_public_config(item)
                auth_required = contract.auth_method_id != "local_none"
                row = ProviderConnection(
                    user_id=owner_id, kind=_legacy_kind(contract.provider_id),
                    name=connection_names[item.ref],
                    base_url=provider_catalog.resolve_origin(
                        contract.provider_id, contract.fields, protocol_id=contract.protocol_id),
                    secret_ref=None, default_model_id=item.default_model_id,
                    enabled=item.enabled, auth_required=auth_required,
                    capabilities={"declared": dict(item.capabilities), "verified": {}},
                    readiness_revision=1, provider_id=contract.provider_id,
                    protocol_id=contract.protocol_id, auth_method_id=contract.auth_method_id,
                    public_config_version=contract.version, public_config=dict(contract.fields),
                    auth_grant_revision=0,
                    credential_state="missing" if auth_required else "not_required",
                )
            db.add(row)
            db.flush()
            connection_ids[item.ref] = row.id
            connection_results.append({"ref": item.ref, "id": row.id, "label": row.name})
        for item in blueprint.agents:
            row = Agent(
                user_id=owner_id, name=agent_names[item.ref], description=item.description,
                system_prompt=item.system_prompt, model=item.model_id,
                connection_id=connection_ids[item.connection_ref], model_id=item.model_id,
                temperature=item.temperature, max_tokens=item.max_tokens, is_active=True,
            )
            db.add(row)
            db.flush()
            agent_ids[item.ref] = row.id
            agent_results.append({"ref": item.ref, "id": row.id, "name": row.name})
        for item in blueprint.projects:
            row = Project(user_id=owner_id, name=project_names[item.ref],
                          description=item.description, is_active=True)
            db.add(row)
            db.flush()
            project_ids[item.ref] = row.id
            project_results.append({"ref": item.ref, "id": row.id, "name": row.name})
            db.add_all([ProjectAgent(project_id=row.id, agent_id=agent_ids[reference])
                        for reference in item.agent_refs])
        db.flush()
        for project in blueprint.projects:
            for item in project.room_templates:
                row = ProjectRoomTemplate(
                    owner_id=owner_id, project_id=project_ids[project.ref],
                    name=template_names[item.ref], room_title=item.room_title,
                    max_rounds=item.max_rounds, max_tool_calls=item.max_tool_calls,
                    max_executor_seconds=item.max_executor_seconds,
                )
                db.add(row)
                db.flush()
                db.add_all([ProjectRoomTemplateMember(
                    template_id=row.id, agent_id=agent_ids[reference])
                    for reference in item.agent_refs])
                template_results.append({"ref": item.ref, "project_ref": project.ref,
                                         "id": row.id, "name": row.name})
        db.commit()
        return {
            "plan_hash": plan_hash, "counts": plan["counts"],
            "connections": connection_results, "agents": agent_results,
            "projects": project_results, "room_templates": template_results,
            "credentials_unset": (sum(1 for item in blueprint.connections
                                      if _needs_credential(item))
                                  if blueprint.version == 2 else len(connection_results)),
        }
    except Exception:
        db.rollback()
        raise
