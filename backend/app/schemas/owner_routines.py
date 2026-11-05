from typing import Literal,Annotated,Union
from zoneinfo import ZoneInfo,ZoneInfoNotFoundError
from pydantic import BaseModel,ConfigDict,Field,field_validator,model_validator

class Strict(BaseModel):model_config=ConfigDict(extra='forbid',strict=True)
class Interval(Strict):
    kind:Literal['interval'];interval_minutes:int=Field(ge=5,le=10080);timezone:str=Field(min_length=1,max_length=100)
    @field_validator('timezone')
    @classmethod
    def zone(cls,v):
        try:ZoneInfo(v)
        except (ZoneInfoNotFoundError,ValueError):raise ValueError('Invalid IANA timezone')
        return v
class Weekly(Strict):
    kind:Literal['weekly'];weekdays:list[int]=Field(min_length=1,max_length=7);hour:int=Field(ge=0,le=23);minute:int=Field(ge=0,le=59);timezone:str=Field(min_length=1,max_length=100)
    @field_validator('weekdays')
    @classmethod
    def days(cls,v):
        if any(type(x) is not int or x<0 or x>6 for x in v) or v!=sorted(set(v)):raise ValueError('Weekdays must be distinct and sorted')
        return v
    @field_validator('timezone')
    @classmethod
    def zone(cls,v):return Interval.zone(v)
class Webhook(Strict):kind:Literal['webhook']
Schedule=Annotated[Union[Interval,Weekly,Webhook],Field(discriminator='kind')]
class RoutineWrite(Strict):
    name:str=Field(min_length=1,max_length=120);instruction:str=Field(min_length=1,max_length=4000)
    schedule:Schedule;enabled:bool
    @field_validator('name','instruction')
    @classmethod
    def nonblank(cls,v):
        if not v.strip():raise ValueError('Must not be blank')
        return v
class RoutineEdit(RoutineWrite):expected_revision:int=Field(ge=1)
class RoutineTest(Strict):expected_revision:int=Field(ge=1)
class RoutineDelete(Strict):expected_revision:int=Field(ge=1)
class RoutineWebhookKey(Strict):
    expected_revision:int=Field(ge=1);expected_generation:int=Field(ge=0)
