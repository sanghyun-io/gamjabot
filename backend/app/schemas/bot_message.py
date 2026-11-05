"""Owner-only explicit retry of one observed terminal Bot message."""
from pydantic import BaseModel, ConfigDict, Field


class BotMessageRetryWrite(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_updated_at: str = Field(min_length=20, max_length=64)
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
