from typing import Literal
from pydantic import Field
from app.schemas.bot_template_portable import Strict, Template

class PublisherConfig(Strict):
    origin:str=Field(min_length=1,max_length=2048)
    token:str=Field(min_length=32,max_length=1017)
    expected_revision:int=Field(ge=0)

class PublicationPreview(Strict):
    operation:Literal['publish','update','revoke']
    template:Template|None

class PublicationCommand(PublicationPreview):
    plan_hash:str=Field(pattern=r'^[0-9a-f]{64}$')

class RemoteAdopt(Strict):
    expected_local_revision:int=Field(ge=1)
    template_id:str=Field(min_length=36,max_length=36)
    observed_revision:int=Field(ge=1)
    observed_revoked:bool
    observed_snapshot_sha256:str|None=Field(pattern=r'^[0-9a-f]{64}$')
