from typing import Literal
from pydantic import BaseModel,ConfigDict,Field
class PageXlsxRequest(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    consent:Literal[True]
    expected_owner_epoch:str=Field(pattern=r'^[a-f0-9]{64}$')
    expected_document_sha256:str=Field(pattern=r'^[a-f0-9]{64}$')
    expected_sha256:str=Field(pattern=r'^[a-f0-9]{64}$')
