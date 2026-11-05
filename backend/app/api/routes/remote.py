"""
Remote Access API - 기기 등록 및 원격 연결 관리
"""
from fastapi import APIRouter, Depends, HTTPException, status, Request
from sqlalchemy.orm import Session
from typing import List
import secrets

from app.core.database import get_db
from app.api.routes.auth import get_current_user
from app.models.models import User, RemoteDevice

router = APIRouter()


@router.post("/devices", status_code=status.HTTP_201_CREATED)
async def register_device(
    device_name: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """새 기기 등록"""
    # 고유한 접근 토큰 생성
    access_token = secrets.token_urlsafe(32)
    
    device = RemoteDevice(
        user_id=current_user.id,
        name=device_name,
        access_token=access_token,
        is_active=True,
        last_connected_at=None
    )
    
    db.add(device)
    db.commit()
    db.refresh(device)
    
    return {
        "device_id": device.id,
        "device_name": device.name,
        "access_token": access_token,
        "message": "기기 기록이 저장되었습니다. 원격 연결은 이 릴리스에서 사용할 수 없습니다."
    }


@router.get("/devices", response_model=List[dict])
async def list_devices(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """등록된 기기 목록"""
    devices = db.query(RemoteDevice).filter(
        RemoteDevice.user_id == current_user.id,
        RemoteDevice.is_active == True
    ).all()
    
    return [
        {
            "device_id": d.id,
            "device_name": d.name,
            "last_connected_at": d.last_connected_at,
            "is_active": d.is_active
        }
        for d in devices
    ]


@router.delete("/devices/{device_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_device(
    device_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """기기 제거"""
    device = db.query(RemoteDevice).filter(
        RemoteDevice.id == device_id,
        RemoteDevice.user_id == current_user.id
    ).first()
    
    if not device:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="기기를 찾을 수 없습니다"
        )
    
    device.is_active = False
    db.commit()
    
    return None


@router.post("/devices/{device_id}/connect")
async def connect_device(
    device_id: int,
    access_token: str,
    db: Session = Depends(get_db)
):
    """Validate a legacy token; remote transport is not available."""
    device = db.query(RemoteDevice).filter(
        RemoteDevice.id == device_id,
        RemoteDevice.access_token == access_token,
        RemoteDevice.is_active == True
    ).first()
    
    if not device:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="유효하지 않은 기기 또는 토큰"
        )
    
    return {
        "status": "unavailable",
        "device_name": device.name,
        "message": "Remote continuation is not available in this release; the legacy token was validated only."
    }


# Personal mobile access is separate from the legacy device token table and
# from provider/API-key/OAuth authentication. Desktop local-session + Owner only.
from pydantic import BaseModel, ConfigDict, StrictBool, Field
class MobileSettings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: StrictBool
    origin: str = Field(default='', max_length=250)

def mobile_manager(request):
    manager = getattr(request.app.state, 'mobile_access', None)
    if manager is None:raise HTTPException(503, 'Mobile gateway unavailable')
    return manager

@router.get('/mobile/status')
def mobile_status(request: Request, owner: User = Depends(get_current_user)):
    return mobile_manager(request).status(owner.id)

@router.put('/mobile/settings')
async def mobile_settings(value: MobileSettings, request: Request, owner: User = Depends(get_current_user)):
    try:return await mobile_manager(request).configure(owner.id, value.enabled, value.origin)
    except OSError:raise HTTPException(409, 'Mobile listener port is already in use or unavailable.') from None

@router.post('/mobile/pairing')
def mobile_pairing(request: Request, owner: User = Depends(get_current_user)):
    return mobile_manager(request).pairing(owner.id)

@router.delete('/mobile/devices/{device_id}', status_code=204)
def mobile_revoke(device_id: str, request: Request, owner: User = Depends(get_current_user)):
    mobile_manager(request).revoke(owner.id, device_id)


class MobileLibraryCommand(BaseModel):
    model_config=ConfigDict(extra='forbid')
    operation_id:str
    enabled:StrictBool
    expected_revision:int=Field(ge=0,lt=9223372036854775807,strict=True)
    expected_owner_epoch:str

@router.post('/mobile/devices/{device_id}/library/operations')
def mobile_library_operation(device_id:str,value:MobileLibraryCommand,request:Request,owner:User=Depends(get_current_user)):
    return mobile_manager(request).library_scope_command(owner.id,device_id,value.model_dump())

@router.post('/mobile/devices/{device_id}/library/resolve')
def mobile_library_resolve(device_id:str,value:MobileLibraryCommand,request:Request,owner:User=Depends(get_current_user)):
    return mobile_manager(request).library_scope_command(owner.id,device_id,value.model_dump(),resolve=True)
