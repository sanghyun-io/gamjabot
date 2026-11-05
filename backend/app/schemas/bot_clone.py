"""Owner-reviewed source and name; copied content is server derived."""
from pydantic import BaseModel, ConfigDict, Field, StrictStr


class ClonePreview(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    name:StrictStr=Field(min_length=1,max_length=100)


class CloneCommit(ClonePreview):
    plan_hash:StrictStr=Field(pattern=r'^[0-9a-f]{64}$')
