from typing import Literal
from pydantic import BaseModel,ConfigDict,Field,StrictBool

class Epoch(BaseModel):
    model_config=ConfigDict(extra='forbid')
    expected_owner_epoch:str=Field(pattern=r'^[a-f0-9]{64}$')

class SettingWrite(Epoch):
    operation_id:str=Field(min_length=36,max_length=36)
    expected_revision:int=Field(ge=0)
    completion:StrictBool
    failure:StrictBool
    approval:StrictBool

class Finish(Epoch):
    state:Literal['accepted','failed','unknown']
