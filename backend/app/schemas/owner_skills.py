from pydantic import BaseModel, ConfigDict, Field, field_validator

class Strict(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
class SkillCreate(Strict):
    name:str=Field(min_length=1,max_length=120)
    description:str=Field(max_length=500)
    content:str=Field(min_length=1,max_length=8000)
    @field_validator('name','content')
    @classmethod
    def nonblank(cls,v):
        if not v.strip():raise ValueError('Must not be blank')
        return v
class SkillDraft(SkillCreate):
    room_id:int=Field(gt=0)
    agent_id:int=Field(gt=0)
    root_event_id:str=Field(min_length=36,max_length=36)
    result_event_id:str=Field(min_length=36,max_length=36)
class SkillEdit(SkillCreate):
    expected_revision:int=Field(ge=1)
    active:bool
class SkillEnable(Strict):
    expected_revision:int=Field(ge=0)
    enabled:bool

class SkillPackageManifest(SkillCreate):
    package_id:str=Field(min_length=1,max_length=100,pattern=r'^[A-Za-z0-9][A-Za-z0-9._-]*$')
    package_version:str=Field(min_length=1,max_length=80,pattern=r'^[A-Za-z0-9][A-Za-z0-9._+-]*$')
    author:str=Field(min_length=1,max_length=120)
    license:str=Field(min_length=1,max_length=120)
    @field_validator('author','license')
    @classmethod
    def nonblank_metadata(cls,v):
        if not v.strip():raise ValueError('Must not be blank')
        return v

class SkillPackageInstall(Strict):
    manifest:SkillPackageManifest
    digest:str=Field(pattern=r'^[a-f0-9]{64}$')
