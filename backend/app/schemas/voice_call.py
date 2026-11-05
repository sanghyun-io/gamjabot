from pydantic import BaseModel, ConfigDict, Field

UUID_PATTERN = r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'

class VoiceCallCreate(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    client_call_id: str = Field(pattern=UUID_PATTERN)
    agent_id: int = Field(gt=0)

class VoiceCallTurnCreate(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    client_turn_id: str = Field(pattern=UUID_PATTERN)
    text: str = Field(min_length=1, max_length=100000)
