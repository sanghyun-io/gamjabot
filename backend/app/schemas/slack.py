from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

class SlackConfigWrite(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_owner_epoch: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_core_epoch: str = Field(pattern=r"^[a-f0-9]{64}$")
    operation_id: str = Field(min_length=36, max_length=36)
    expected_revision: int = Field(ge=0)
    account_id: str = Field(min_length=36, max_length=36)
    credential_revision: int = Field(ge=1)
    enabled: bool
    workspace_id: str = Field(min_length=1, max_length=64)
    app_id: str = Field(min_length=1, max_length=64)
    channel_id: str = Field(min_length=1, max_length=64)
    sender_ids: list[str] = Field(min_length=1, max_length=64)
    agent_id: int = Field(gt=0)
    project_id: int = Field(gt=0)
    connection_id: int = Field(gt=0)
    model_id: str = Field(min_length=1, max_length=200)

class SlackReplyResolutionWrite(BaseModel):
    model_config = ConfigDict(extra='forbid')
    operation_id: str = Field(min_length=36, max_length=36)
    attempt_id: str | None = Field(default=None, min_length=36, max_length=36)
    expected_state: Literal['unknown', 'blocked', 'failed_before_dispatch', 'failed', 'cancelled', 'interrupted']
    decision: Literal['owner_confirmed_sent', 'owner_confirmed_not_sent', 'owner_skip_no_reply', 'owner_hold_unknown']
    expected_config_revision: int = Field(ge=0)
    expected_run_revision: int = Field(ge=1)
    expected_owner_epoch: str = Field(pattern=r'^[a-f0-9]{64}$')
    expected_core_epoch: str = Field(pattern=r'^[a-f0-9]{64}$')
