"""Explicitly selected share data, never inferred from private Bot context."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

class Closed(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

class Identity(Closed):
    issuer: str = Field(min_length=1, max_length=512)
    subject: str = Field(min_length=1, max_length=512)

class CreateTeam(Closed):
    team_id: str = Field(min_length=36, max_length=36)

class Invite(Closed):
    invite_id: str = Field(min_length=36, max_length=36)
    subject: Identity
    role: Literal['manager','member']
    expires_at: str = Field(min_length=20, max_length=40)
    expected_revision: int = Field(ge=1)

class SharePerson(Identity):
    role: Literal['manager','collaborator']

class Publish(Closed):
    share_id: str = Field(min_length=36, max_length=36)
    agent_id: int = Field(gt=0)
    role_text: str = Field(min_length=1, max_length=16000)
    knowledge_text: str = Field(max_length=64000)
    audience: Literal['team','selected']
    people: list[SharePerson] = Field(max_length=100)
    expected_revision: int = Field(ge=0)

class RecipientRoom(Closed):
    room_id: int = Field(gt=0)
    connection_id: int = Field(gt=0)
    model_id: str = Field(min_length=1,max_length=200)
    expected_share_revision: int = Field(ge=1)
    expected_revocation_epoch: int = Field(ge=0)

class ExpectedRevision(Closed):
    expected_revision: int = Field(ge=1)

class RevokeMember(Closed):
    identity: Identity
    expected_revision: int = Field(ge=1)
