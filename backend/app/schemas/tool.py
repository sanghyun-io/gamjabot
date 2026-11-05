"""Owner consent requests. Action details always come from the server ledger."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr


class DecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["approve", "deny"]
    scope: Literal["once", "room_session"] | None = None
    review_id: StrictStr | None = Field(default=None, max_length=36)
    acknowledge_unverified_context: StrictBool = False
    send_image_to_model: StrictBool = False


class BroadModeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool
    expires_minutes: int | None = Field(default=None, ge=1, le=60)
    always: bool = False
