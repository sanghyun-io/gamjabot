from fastapi import APIRouter,Depends,Header
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from app.api.routes.auth import get_current_user
from app.core.database import get_db
from app.models.models import User
from app.schemas.owner_skills import SkillCreate,SkillDraft,SkillEdit,SkillEnable,SkillPackageManifest,SkillPackageInstall
from app.services import owner_skills as service, skill_packages
router=APIRouter()
HEADERS={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'}
def out(value):return JSONResponse(value,headers=HEADERS)

@router.get('/skill-packages')
def packages(q:str='',owner:User=Depends(get_current_user)):
    return out(skill_packages.catalog(q))
@router.post('/skill-packages/preview')
def package_preview(value:SkillPackageManifest,owner:User=Depends(get_current_user)):
    return out(skill_packages.preview(value))
@router.post('/skill-packages/install')
def package_install(value:SkillPackageInstall,key:str=Header(alias='Idempotency-Key'),db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    return out(skill_packages.install(db,owner.id,value,key))

@router.get('/skills')
def listing(q:str='',db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    return out(service.listing(db,owner.id,q))
@router.post('/skills')
def create(value:SkillCreate,key:str=Header(alias='Idempotency-Key'),db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    return out(service.create(db,owner.id,value,key))
@router.post('/skills/from-request')
def draft(value:SkillDraft,key:str=Header(alias='Idempotency-Key'),db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    return out(service.create(db,owner.id,value,key,draft=True))
@router.get('/skills/commands/{key}')
def command(key:str,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    return out(service.command(db,owner.id,key))
@router.get('/skills/{skill_id}')
def get(skill_id:str,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    return out(service.dto(service.owned(db,owner.id,skill_id)))
@router.patch('/skills/{skill_id}')
def edit(skill_id:str,value:SkillEdit,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    return out(service.edit(db,owner.id,skill_id,value))
@router.get('/skills/{skill_id}/versions')
def versions(skill_id:str,before_revision:int|None=None,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    return out(service.versions(db,owner.id,skill_id,before_revision))
@router.get('/bots/{agent_id}/skills')
def bindings(agent_id:int,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    return out(service.bindings(db,owner.id,agent_id))
@router.put('/bots/{agent_id}/skills/{skill_id}')
def enable(agent_id:int,skill_id:str,value:SkillEnable,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    return out(service.enable(db,owner.id,agent_id,skill_id,value))

@router.get('/bots/{agent_id}/skill-persistence/{receipt_id}')
def skill_receipt(agent_id:int,receipt_id:str,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    from app.services import skill_persistence
    return out(skill_persistence.get(db,owner.id,agent_id,receipt_id))
