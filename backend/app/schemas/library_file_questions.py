from pydantic import BaseModel, ConfigDict, Field, field_validator,model_validator
from typing import Literal
from uuid import UUID

class FileQuestionPreview(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    bot_id:int=Field(gt=0)
    upload_id:str
    sha256:str=Field(pattern=r'^[a-f0-9]{64}$')
    content:str=Field(min_length=1,max_length=4000)
    mode:Literal['text','image','pdf_ocr']='text'
    full_access:bool=False
    @model_validator(mode='after')
    def full_access_mode(self):
        if self.mode=='image' and self.full_access:raise ValueError('Image full access unsupported')
        return self
    @field_validator('upload_id')
    @classmethod
    def canonical_id(cls,value):
        try:
            if str(UUID(value))!=value: raise ValueError()
        except (ValueError,TypeError,AttributeError): raise ValueError('Invalid upload ID') from None
        return value
    @field_validator('content')
    @classmethod
    def nonblank(cls,value):
        if not value.strip():raise ValueError('Question required')
        return value

class FileQuestionSend(FileQuestionPreview):
    basis:str=Field(pattern=r'^[a-f0-9]{64}$')
    consent:bool
    image_consent:bool=False
    full_access_consent:bool=False
    @field_validator('consent')
    @classmethod
    def explicit(cls,value):
        if value is not True:raise ValueError('Consent required')
        return value
    @model_validator(mode='after')
    def image_permission(self):
        if self.full_access_consent is not self.full_access:raise ValueError('Full-range consent required')
        if self.mode=='image' and self.image_consent is not True:raise ValueError('Image consent required')
        if self.mode!='image' and self.image_consent is not False:raise ValueError('Unexpected image consent')
        return self
