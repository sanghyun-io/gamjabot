"""Owner-scoped reusable room setup persistence and atomic instantiation."""

from datetime import datetime

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.collaboration import ProjectRoomTemplate, ProjectRoomTemplateMember
from app.models.models import Agent, Project
from app.schemas.room_template import RoomTemplateWrite
from app.services.room_service import create_room_from_setup


def _project(db: Session, project_id: int, owner_id: int) -> Project:
    row = db.query(Project).filter_by(id=project_id, user_id=owner_id, is_active=True).first()
    if row is None:
        raise HTTPException(404, "Project not found")
    return row


def _template(db: Session, project_id: int, template_id: int, owner_id: int) -> ProjectRoomTemplate:
    _project(db, project_id, owner_id)
    row = db.query(ProjectRoomTemplate).filter_by(
        id=template_id, project_id=project_id, owner_id=owner_id).first()
    if row is None:
        raise HTTPException(404, "Room template not found")
    return row


def _member_ids(db: Session, template_id: int) -> list[int]:
    return [row[0] for row in db.query(ProjectRoomTemplateMember.agent_id).filter_by(
        template_id=template_id).order_by(ProjectRoomTemplateMember.agent_id).all()]


def _response(db: Session, row: ProjectRoomTemplate) -> dict:
    return {
        "id": row.id, "user_id": row.owner_id, "project_id": row.project_id,
        "name": row.name, "room_title": row.room_title,
        "agent_ids": _member_ids(db, row.id),
        "max_rounds": row.max_rounds, "max_tool_calls": row.max_tool_calls,
        "max_executor_seconds": row.max_executor_seconds,
        "created_at": row.created_at, "updated_at": row.updated_at,
    }


def _validate_members(db: Session, owner_id: int, agent_ids: list[int]) -> None:
    valid = {row[0] for row in db.query(Agent.id).filter(
        Agent.id.in_(agent_ids), Agent.user_id == owner_id,
        Agent.is_active.is_(True)).all()} if agent_ids else set()
    unavailable = sorted(set(agent_ids) - valid)
    if unavailable:
        raise HTTPException(409, {
            "code": "room_template_members_unavailable", "agent_ids": unavailable,
        })


def _name_conflict(db: Session, owner_id: int, project_id: int, name: str,
                   *, exclude_id: int | None = None) -> None:
    query = db.query(ProjectRoomTemplate.id).filter_by(
        owner_id=owner_id, project_id=project_id, name=name)
    if exclude_id is not None:
        query = query.filter(ProjectRoomTemplate.id != exclude_id)
    if query.first() is not None:
        raise HTTPException(409, {"code": "room_template_name_conflict"})


def list_templates(db: Session, project_id: int, owner_id: int) -> list[dict]:
    _project(db, project_id, owner_id)
    rows = db.query(ProjectRoomTemplate).filter_by(
        project_id=project_id, owner_id=owner_id).order_by(
        ProjectRoomTemplate.updated_at.desc(), ProjectRoomTemplate.id.desc()).all()
    return [_response(db, row) for row in rows]


def get_template(db: Session, project_id: int, template_id: int, owner_id: int) -> dict:
    return _response(db, _template(db, project_id, template_id, owner_id))


def create_template(db: Session, project_id: int, owner_id: int,
                    value: RoomTemplateWrite) -> dict:
    _project(db, project_id, owner_id)
    _validate_members(db, owner_id, value.agent_ids)
    _name_conflict(db, owner_id, project_id, value.name)
    row = ProjectRoomTemplate(
        owner_id=owner_id, project_id=project_id, name=value.name,
        room_title=value.room_title, max_rounds=value.max_rounds,
        max_tool_calls=value.max_tool_calls,
        max_executor_seconds=value.max_executor_seconds,
    )
    try:
        db.add(row)
        db.flush()
        db.add_all([ProjectRoomTemplateMember(template_id=row.id, agent_id=agent_id)
                    for agent_id in value.agent_ids])
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, {"code": "room_template_name_conflict"}) from None
    db.refresh(row)
    return _response(db, row)


def update_template(db: Session, project_id: int, template_id: int, owner_id: int,
                    value: RoomTemplateWrite) -> dict:
    row = _template(db, project_id, template_id, owner_id)
    _validate_members(db, owner_id, value.agent_ids)
    _name_conflict(db, owner_id, project_id, value.name, exclude_id=row.id)
    row.name, row.room_title = value.name, value.room_title
    row.max_rounds, row.max_tool_calls = value.max_rounds, value.max_tool_calls
    row.max_executor_seconds, row.updated_at = value.max_executor_seconds, datetime.utcnow()
    try:
        db.query(ProjectRoomTemplateMember).filter_by(template_id=row.id).delete()
        db.add_all([ProjectRoomTemplateMember(template_id=row.id, agent_id=agent_id)
                    for agent_id in value.agent_ids])
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, {"code": "room_template_name_conflict"}) from None
    db.refresh(row)
    return _response(db, row)


def delete_template(db: Session, project_id: int, template_id: int, owner_id: int) -> None:
    row = _template(db, project_id, template_id, owner_id)
    db.delete(row)
    db.commit()


def instantiate_template(db: Session, project_id: int, template_id: int, owner_id: int):
    row = _template(db, project_id, template_id, owner_id)
    member_ids = _member_ids(db, row.id)
    _validate_members(db, owner_id, member_ids)
    return create_room_from_setup(db, owner_id, project_id, row.room_title, member_ids, {
        "max_rounds": row.max_rounds, "max_tool_calls": row.max_tool_calls,
        "max_executor_seconds": row.max_executor_seconds,
    })
