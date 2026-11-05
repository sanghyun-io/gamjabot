"""Owner reaction writes use an absolute value and the last observed sequence."""
from typing import Literal
from pydantic import BaseModel,ConfigDict,Field

class MessageReactionWrite(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    emoji:Literal['👍','❤️','😂','🎉','😮','👀']|None
    expected_seq:int=Field(ge=0)
