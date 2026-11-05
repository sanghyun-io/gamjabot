from pydantic import BaseModel,ConfigDict,Field

class PrimaryCheckinWrite(BaseModel):
    model_config=ConfigDict(extra='forbid')
    expected_revision:int=Field(strict=True,ge=0)
    expected_primary_revision:int=Field(strict=True,ge=0)
    enabled:bool=Field(strict=True)
    prompt:str=Field(strict=True,max_length=2000)
    interval_minutes:int=Field(strict=True,ge=1,le=10080)
