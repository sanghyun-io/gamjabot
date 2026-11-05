"""Pure starter planning and atomic creation from explicit readiness evidence."""

from hashlib import sha256
import json

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.collaboration import ConnectionTestHistory, ProviderConnection, StarterRequest
from app.models.models import Agent, Conversation, Project, ProjectAgent
from app.schemas.starter import StarterApply, StarterIntent
from app.services.readiness import test_fingerprint
from app.services.room_service import stage_room_from_setup


POLICY = {"max_rounds": 2, "max_tool_calls": 0, "max_executor_seconds": 60}
SHARED_ROLE_INSTRUCTION = (
    "You are one participant in a room; the application invokes each Agent separately. "
    "Produce only your own contribution. Do not invent peer or user messages, approvals, or actions. "
    "Follow the user's requested language, length, and format; do not force fixed headings."
)
ROLE_TEXT = {
    "proposer": (
        "Creates a concrete proposal for the room task.",
        f"{SHARED_ROLE_INSTRUCTION} "
        "Act as the proposer. Make a concrete recommendation. Include constraints, assumptions, and next steps only when useful or requested. "
        "In later rounds, respond to an actual peer detail.",
    ),
    "reviewer": (
        "Checks the proposal for mistakes and missing constraints.",
        f"{SHARED_ROLE_INSTRUCTION} "
        "Act as the reviewer. Critique only material issues, risks, or omissions supported by the provided conversation, and offer specific improvements. "
        "Before a peer proposal exists, discuss task criteria without inventing a proposal. If the proposal is sound, do not invent defects to fill a quota.",
    ),
}


def _canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _digest(value) -> str:
    return sha256(_canonical(value)).hexdigest()


def _normalized_intent(intent: StarterIntent) -> dict:
    value = intent.model_dump()
    value["agents"] = sorted(value["agents"], key=lambda agent: agent["role"])
    return value


def _allocate(requested: str, used: set[str], maximum: int) -> str:
    if requested not in used:
        used.add(requested)
        return requested
    index = 2
    while True:
        suffix = f" ({index})"
        candidate = requested[: maximum - len(suffix)].rstrip() + suffix
        if candidate not in used:
            used.add(candidate)
            return candidate
        index += 1


def preview(db: Session, owner_id: int, intent: StarterIntent) -> dict:
    used_projects = {row[0] for row in db.query(Project.name).filter_by(user_id=owner_id).all()}
    used_agents = {row[0] for row in db.query(Agent.name).filter_by(user_id=owner_id).all()}
    project_title = _allocate(intent.project_title, used_projects, 100)
    planned_agents = []
    for item in sorted(intent.agents, key=lambda row: row.role):
        connection = db.query(ProviderConnection).filter_by(
            id=item.connection_id, user_id=owner_id, enabled=True).first()
        if connection is None:
            raise HTTPException(404, {"code": "starter_connection_unavailable", "role": item.role})
        fingerprints = tuple(test_fingerprint(connection, item.model_id, profile)
                             for profile in ("full", "text"))
        evidence = db.query(ConnectionTestHistory).filter_by(
            owner_id=owner_id, connection_id=connection.id, model_id=item.model_id,
            config_revision=int(connection.readiness_revision or 1),
        ).filter(
            ConnectionTestHistory.config_fingerprint.in_(fingerprints),
        ).order_by(ConnectionTestHistory.tested_at.desc(), ConnectionTestHistory.id.desc()).first()
        if evidence is None or not evidence.ok or evidence.outcome_code != "connected":
            raise HTTPException(409, {"code": "starter_model_unverified", "role": item.role})
        planned_agents.append({
            "role": item.role,
            "requested_name": item.name,
            "name": _allocate(item.name, used_agents, 100),
            "connection_id": connection.id,
            "model_id": item.model_id,
            "test_history_id": evidence.id,
            "config_revision": int(connection.readiness_revision or 1),
            "config_fingerprint": evidence.config_fingerprint,
        })
    facts = {
        "owner_id": owner_id,
        "intent": _normalized_intent(intent),
        "project_title": project_title,
        "room_title": intent.room_title,
        "agents": planned_agents,
        "policy": POLICY,
        "roles": {item["role"]: list(ROLE_TEXT[item["role"]]) for item in planned_agents},
    }
    return {"plan_hash": _digest(facts), "project_title": project_title,
            "room_title": intent.room_title, "agents": planned_agents, "policy": POLICY.copy()}


def _request_digest(intent: StarterIntent) -> str:
    return _digest(_normalized_intent(intent))


def _result(row: StarterRequest, replayed: bool) -> dict:
    return {"project_id": row.project_id, "room_id": row.room_id,
            "agents": {"proposer": row.proposer_agent_id, "reviewer": row.reviewer_agent_id},
            "replayed": replayed}


def apply(db: Session, owner_id: int, value: StarterApply, idempotency_key: str) -> dict:
    if (not idempotency_key or len(idempotency_key) > 200
            or any(not char.isprintable() for char in idempotency_key)):
        raise HTTPException(422, {"code": "starter_idempotency_key_invalid"})
    intent = StarterIntent.model_validate(value.model_dump(exclude={"plan_hash"}))
    request_digest = _request_digest(intent)
    db.rollback()
    db.execute(text("BEGIN IMMEDIATE"))
    try:
        prior = db.query(StarterRequest).filter_by(
            owner_id=owner_id, idempotency_key=idempotency_key).first()
        if prior is not None:
            if prior.request_digest != request_digest:
                raise HTTPException(409, {"code": "starter_idempotency_conflict"})
            result = _result(prior, True)
            db.commit()
            return result

        try:
            plan = preview(db, owner_id, intent)
        except HTTPException as error:
            raise HTTPException(409, {"code": "starter_plan_stale"}) from error
        if plan["plan_hash"] != value.plan_hash:
            raise HTTPException(409, {"code": "starter_plan_stale"})

        agents_by_role = {}
        for planned in plan["agents"]:
            description, prompt = ROLE_TEXT[planned["role"]]
            agent = Agent(
                user_id=owner_id, name=planned["name"], description=description,
                system_prompt=prompt, model=planned["model_id"],
                connection_id=planned["connection_id"], model_id=planned["model_id"],
                temperature="auto", max_tokens=0, is_active=True,
            )
            db.add(agent)
            db.flush()
            agents_by_role[planned["role"]] = agent
        project = Project(user_id=owner_id, name=plan["project_title"], description=None, is_active=True)
        db.add(project)
        db.flush()
        db.add_all([ProjectAgent(project_id=project.id, agent_id=agent.id)
                    for agent in agents_by_role.values()])
        db.flush()
        room = stage_room_from_setup(
            db, owner_id, project.id, plan["room_title"],
            [agents_by_role["proposer"].id, agents_by_role["reviewer"].id], POLICY)
        db.flush()
        record = StarterRequest(
            owner_id=owner_id, idempotency_key=idempotency_key,
            request_digest=request_digest, plan_hash=value.plan_hash,
            project_id=project.id, room_id=room.id,
            proposer_agent_id=agents_by_role["proposer"].id,
            reviewer_agent_id=agents_by_role["reviewer"].id,
        )
        db.add(record)
        db.flush()
        result = _result(record, False)
        db.commit()
        return result
    except BaseException:
        db.rollback()
        raise


def last_result(db: Session, owner_id: int) -> dict | None:
    row = db.query(StarterRequest).filter_by(owner_id=owner_id).order_by(
        StarterRequest.id.desc()).first()
    if row is None:
        return None
    result = _result(row, False)
    result.pop("replayed")
    return result
