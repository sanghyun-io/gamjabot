"""Owner-supplied contact reference data, never passwords or credentials."""
import re
import unicodedata
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator

_LIMITS={'name':200,'email':320,'phone':128,'address_line1':512,'address_line2':512,
         'city':512,'region':512,'postal_code':512,'country':512,'nickname':200,'hint':2000}

class ContactData(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    name:StrictStr=Field(min_length=1,max_length=200)
    email:StrictStr=Field(default='',max_length=320)
    phone:StrictStr=Field(default='',max_length=128)
    address_line1:StrictStr=Field(default='',max_length=512)
    address_line2:StrictStr=Field(default='',max_length=512)
    city:StrictStr=Field(default='',max_length=512)
    region:StrictStr=Field(default='',max_length=512)
    postal_code:StrictStr=Field(default='',max_length=512)
    country:StrictStr=Field(default='',max_length=512)
    nickname:StrictStr=Field(default='',max_length=200)
    hint:StrictStr=Field(default='',max_length=2000)

    @field_validator(*_LIMITS)
    @classmethod
    def validate_text(cls,value,info):
        if len(value.encode('utf-8'))>_LIMITS[info.field_name] or any(unicodedata.category(c)=='Cc' for c in value):
            raise ValueError('invalid contact')
        if info.field_name=='name' and not value.strip():raise ValueError('invalid contact')
        if info.field_name=='email' and value and (value!=value.strip() or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',value)):
            raise ValueError('invalid contact')
        return value

class CreateContact(ContactData):pass
class UpdateContact(ContactData):
    expected_revision:StrictInt=Field(ge=1)
class DeleteContact(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    expected_revision:StrictInt=Field(ge=1)
