"""Authenticated Owner-only usage ledger; prices are public reference values."""
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from app.api.routes.auth import get_current_user
from app.models.models import User

router = APIRouter()

def store(request):
    ledger = getattr(request.app.state, 'model_usage', None)
    if ledger is None: raise HTTPException(503, detail='usage_recording_unavailable')
    return ledger

@router.get('')
def usage(request: Request, days: int = Query(30, ge=1, le=3660), owner: User = Depends(get_current_user)):
    return store(request).report(owner.id, days)

@router.post('/prices/refresh')
async def prices(request: Request, days: int = Query(30, ge=1, le=3660), owner: User = Depends(get_current_user)):
    ledger = store(request)
    await ledger.refresh(force=True)
    return ledger.report(owner.id, days)
