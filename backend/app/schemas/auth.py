"""
Authentication schemas
"""
from pydantic import AfterValidator, BaseModel, ConfigDict, EmailStr, Field
from typing import Annotated, Optional
from datetime import datetime


def password_bytes(value: str) -> str:
    if not value or len(value.encode('utf-8')) > 72 or '\x00' in value:
        raise ValueError('Password must be 1–72 UTF-8 bytes without NUL')
    return value


Password = Annotated[str, AfterValidator(password_bytes)]


class UserCreate(BaseModel):
    """Schema for user registration"""
    email: EmailStr
    username: str
    password: Password


class RestoreHint(BaseModel):
    expected_owner_id: int = Field(strict=True, gt=0)
    model_config = ConfigDict(extra='forbid')


class UserLogin(BaseModel):
    email: EmailStr
    password: Password
    remember_device: bool = False
    # Owner CLI sessions are deliberately ephemeral, but must not revoke the
    # desktop's existing remembered login as a side effect of authentication.
    preserve_remembered_login: bool = False
    preserve_saved_login_for_session: bool = Field(default=False, strict=True)


class UserResponse(BaseModel):
    """Schema for user response"""
    id: int
    email: EmailStr
    username: str
    is_active: bool
    created_at: datetime
    
    class Config:
        from_attributes = True


class Token(BaseModel):
    """Schema for JWT token"""
    access_token: str
    token_type: str
