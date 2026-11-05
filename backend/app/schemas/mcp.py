from typing import Literal,Union,Annotated
from pydantic import BaseModel,ConfigDict,Field,field_validator,model_validator
from urllib.parse import urlsplit
import os,re
class Strict(BaseModel):model_config=ConfigDict(extra='forbid',strict=True)
class HTTPTransport(Strict):
    type:Literal['http'];url:str=Field(min_length=1,max_length=2048)
    @field_validator('url')
    @classmethod
    def valid_url(cls,v):
        p=urlsplit(v)
        if p.scheme not in ('http','https') or not p.hostname or p.username or p.password or p.query or p.fragment or p.scheme=='http' and p.hostname not in ('127.0.0.1','localhost','::1') or any(ord(c)<32 for c in v):raise ValueError('Invalid MCP endpoint')
        return v
class StdioTransport(Strict):
    type:Literal['stdio'];executable:str=Field(min_length=1,max_length=4096);argv:list[str]=Field(max_length=32)
    @model_validator(mode='after')
    def valid_command(self):
        if not os.path.isabs(self.executable) or '\x00' in self.executable or any(len(x)>1024 or '\x00' in x or re.search(r'(?i)(?:token|password|secret|api[_-]?key|authorization)\s*=|(?:^|--?)(?:token|password|secret|api[_-]?key|authorization)(?:\s|$)',x) for x in self.argv):raise ValueError('Invalid command; credentials belong in the secure connector')
        return self
Transport=Annotated[Union[HTTPTransport,StdioTransport],Field(discriminator='type')]
class MCPCreate(Strict):
    name:str=Field(min_length=1,max_length=120);enabled:bool;transport:Transport
    @field_validator('name')
    @classmethod
    def nonblank(cls,v):
        if not v.strip():raise ValueError('Name required')
        return v
class MCPEdit(MCPCreate):expected_revision:int=Field(ge=1)
class MCPRevision(Strict):expected_revision:int=Field(ge=1)
class MCPDiscover(MCPRevision):preview_id:str=Field(min_length=36,max_length=36)
class MCPBindingWrite(Strict):
    expected_revision:int=Field(ge=0);expected_server_revision:int=Field(ge=1);enabled:bool
    tool_ids:list[str]|None=Field(default=None,max_length=100)
    @field_validator('tool_ids')
    @classmethod
    def validate_tools(cls,v):
        if v is not None and (len(set(v))!=len(v) or any(not re.fullmatch(r'[a-f0-9]{64}',x) for x in v)):raise ValueError('Invalid tool selection')
        return sorted(v) if v is not None else None
class MCPCredential(MCPRevision):
    credential_ref:str=Field(min_length=75,max_length=200)
