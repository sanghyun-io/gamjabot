"""Portable template v1. Standalone schema shared with a potential registry."""
import json
from hashlib import sha256
from typing import Annotated, Literal, Union
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_TEMPLATE_BYTES=128*1024
class TemplateTooLarge(ValueError): pass
class Strict(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
class Avatar(Strict):
    version:Literal[1]
    head:int=Field(ge=0,lt=10);hair:int=Field(ge=0,lt=12);eyes:int=Field(ge=0,lt=12)
    nose:int=Field(ge=0,lt=8);mouth:int=Field(ge=0,lt=12);skin:int=Field(ge=0,lt=12);accent:int=Field(ge=0,lt=10)
    @field_validator('version',mode='before')
    @classmethod
    def exact_version(cls,v):
        if type(v) is not int: raise ValueError('invalid')
        return v
class Settings(Strict):
    temperature:str=Field(pattern=r'^(auto|(?:[0-1](?:\.\d+)?|2(?:\.0+)?))$')
    max_tokens:int=Field(ge=0,le=100000)
class Skill(Strict):
    name:str=Field(min_length=1,max_length=120);description:str=Field(max_length=500);content:str=Field(min_length=1,max_length=8000)
    @field_validator('name','content')
    @classmethod
    def nonblank(cls,v):
        if not v.strip():raise ValueError('blank')
        return v
class Interval(Strict):
    kind:Literal['interval'];interval_minutes:int=Field(ge=5,le=10080);timezone:str=Field(min_length=1,max_length=100)
    @field_validator('timezone')
    @classmethod
    def zone(cls,v):
        try:ZoneInfo(v)
        except (ZoneInfoNotFoundError,ValueError):raise ValueError('invalid timezone')
        return v
class Weekly(Strict):
    kind:Literal['weekly'];weekdays:list[int]=Field(min_length=1,max_length=7);hour:int=Field(ge=0,le=23);minute:int=Field(ge=0,le=59);timezone:str=Field(min_length=1,max_length=100)
    @field_validator('weekdays')
    @classmethod
    def days(cls,v):
        if any(type(x) is not int or x<0 or x>6 for x in v) or v!=sorted(set(v)):raise ValueError('invalid days')
        return v
    @field_validator('timezone')
    @classmethod
    def zone(cls,v):return Interval.zone(v)
class Webhook(Strict):kind:Literal['webhook']
class Routine(Strict):
    name:str=Field(min_length=1,max_length=120);instruction:str=Field(min_length=1,max_length=4000)
    schedule:Annotated[Union[Interval,Weekly,Webhook],Field(discriminator='kind')];enabled:bool
    @field_validator('name','instruction')
    @classmethod
    def nonblank(cls,v):
        if not v.strip():raise ValueError('blank')
        return v
class Template(Strict):
    version:Literal[1]
    name:str=Field(min_length=1,max_length=100);description:str=Field(min_length=1,max_length=8000);system_prompt:str=Field(min_length=1,max_length=16000)
    avatar:Avatar;settings:Settings;skills:list[Skill]=Field(max_length=8);routines:list[Routine]=Field(max_length=50)
    @field_validator('version',mode='before')
    @classmethod
    def exact_version(cls,v):
        if type(v) is not int:raise ValueError('invalid')
        return v
    @field_validator('name','description','system_prompt')
    @classmethod
    def nonblank(cls,v):
        if not v.strip():raise ValueError('blank')
        return v

def portable_bytes(value):
    template=Template.model_validate(value).model_dump(mode='json')
    raw=json.dumps(template,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf-8')
    if len(raw)>MAX_TEMPLATE_BYTES:raise TemplateTooLarge('template too large')
    return template,raw,sha256(raw).hexdigest()
