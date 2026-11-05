"""Strict card reference data; raw payment credentials are never accepted."""
import unicodedata
from pydantic import BaseModel,ConfigDict,Field,StrictInt,StrictStr,field_validator
class Billing(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    address_line1:StrictStr=Field(default='',max_length=512)
    address_line2:StrictStr=Field(default='',max_length=512)
    city:StrictStr=Field(default='',max_length=512)
    region:StrictStr=Field(default='',max_length=512)
    postal_code:StrictStr=Field(default='',max_length=512)
    country:StrictStr=Field(default='',max_length=512)
    @field_validator('*')
    @classmethod
    def safe(cls,v):
        if len(v.encode('utf-8'))>512 or any(unicodedata.category(c)=='Cc' for c in v):raise ValueError('invalid card metadata')
        return v
class CardData(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    cardholder:StrictStr=Field(min_length=1,max_length=200)
    expiry_month:StrictInt=Field(ge=1,le=12)
    expiry_year:StrictInt=Field(ge=2000,le=9999)
    billing:Billing
    nickname:StrictStr=Field(default='',max_length=200)
    hint:StrictStr=Field(default='',max_length=2000)
    @field_validator('cardholder','nickname','hint')
    @classmethod
    def safe(cls,v,info):
        if len(v.encode('utf-8'))>(2000 if info.field_name=='hint' else 200) or any(unicodedata.category(c)=='Cc' for c in v) or (info.field_name=='cardholder' and not v.strip()):raise ValueError('invalid card metadata')
        return v
class CreateCard(CardData):pass
class UpdateCard(CardData):expected_revision:StrictInt=Field(ge=1)
class DeleteCard(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    expected_revision:StrictInt=Field(ge=1)
class ConfirmCard(DeleteCard):
    secret_revision:StrictInt=Field(ge=1)
    secret_command:StrictStr=Field(pattern=r'^[0-9a-f]{32}$')
