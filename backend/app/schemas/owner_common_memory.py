from typing import Literal
from pydantic import BaseModel,ConfigDict,Field

class StrictModel(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
class MemoryPolicyWrite(StrictModel):
    expected_revision:int=Field(ge=0)
    mode:Literal['canonical_only','owner_rooms']
class MemoryVisibilityWrite(StrictModel):
    expected_scope_revision:int=Field(ge=0)
    expected_revision:int=Field(ge=1)
    group_allowed:bool
class Origin(StrictModel):
    agent_id:int=Field(ge=1)
    source_kind:Literal['manual']
class OwnerCommonAdd(StrictModel):
    expected_scope_revision:int=Field(ge=0)
    kind:Literal['name','timezone']
    content:str=Field(min_length=1,max_length=2000)
    origin:Origin
class OwnerCommonEdit(StrictModel):
    expected_scope_revision:int=Field(ge=0)
    expected_revision:int=Field(ge=1)
    content:str=Field(min_length=1,max_length=2000)
class OwnerCommonDelete(StrictModel):
    expected_scope_revision:int=Field(ge=0)
    expected_revision:int=Field(ge=1)
