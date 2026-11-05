"""Authenticated import requests for the shared portable template schema."""
from pydantic import Field, field_validator
from app.schemas.bot_template_portable import Strict, Template
from app.schemas.provider_models import MODEL_ID_PATTERN

class Export(Strict):
    source_id:int=Field(gt=0)
class ImportPreview(Strict):
    template:Template
    name:str=Field(min_length=1,max_length=100)
    connection_id:int=Field(gt=0)
    model_id:str=Field(min_length=1,max_length=200,pattern=MODEL_ID_PATTERN)
    @field_validator('name')
    @classmethod
    def name_valid(cls,value):
        if value!=value.strip() or not value.isprintable(): raise ValueError('invalid')
        return value
class ImportCommit(ImportPreview):
    plan_hash:str=Field(pattern=r'^[0-9a-f]{64}$')

class PreviewLink(Strict):
    url:str=Field(min_length=1,max_length=2048)
