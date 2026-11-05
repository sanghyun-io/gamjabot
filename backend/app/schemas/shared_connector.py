from typing import Literal
from uuid import UUID
from pydantic import BaseModel,ConfigDict,Field,model_validator
class ConnectorCommand(BaseModel):
 model_config=ConfigDict(extra='forbid',strict=True)
 operation_id:str;session_generation:str;publication_id:str
 operation:Literal['link','unlink','decide','clear']
 server_id:str
 expected_revision:int=Field(ge=0)
 expected_preference_revision:int|None=Field(default=None,ge=0)
 choice:Literal['once','bot','all','skip']|None=None
 request_id:str|None=None
 @model_validator(mode='after')
 def valid(self):
  for value in (self.operation_id,self.session_generation,self.publication_id,self.server_id):
   if str(UUID(value))!=value:raise ValueError('Invalid identity')
  if self.operation=='decide':
   if self.choice is None or self.request_id is None or str(UUID(self.request_id))!=self.request_id:raise ValueError('Decision requires exact request')
  elif self.choice is not None or self.request_id is not None:raise ValueError('Unexpected decision')
  requires_preference=(self.operation in ('unlink','clear') or self.operation=='decide' and self.choice in ('bot','all'))
  if requires_preference != (self.expected_preference_revision is not None):raise ValueError('Invalid preference revision')
  return self


class ClearAllConnectors(BaseModel):
 model_config=ConfigDict(extra='forbid',strict=True)
 operation_id:str;session_generation:str;publication_id:str
 expected_snapshot_digest:str=Field(pattern=r'^[0-9a-f]{64}$')
 @model_validator(mode='after')
 def valid_identity(self):
  for value in (self.operation_id,self.session_generation,self.publication_id):
   if str(UUID(value))!=value:raise ValueError('Invalid identity')
  return self


class RecoverClearAll(ClearAllConnectors):
 original_operation_id:str
 @model_validator(mode='after')
 def valid_original(self):
  if str(UUID(self.original_operation_id)) != self.original_operation_id or self.original_operation_id == self.operation_id:raise ValueError('Invalid original operation')
  return self
