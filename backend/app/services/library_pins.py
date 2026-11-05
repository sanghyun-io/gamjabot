"""Durable references only: never copy, retain or delete the source data."""
import hashlib,json,re
from uuid import UUID
from fastapi import HTTPException
from sqlalchemy import text
from app.models.models import Conversation,Project
from app.models.room_result import RoomResult,RoomResultVersion
from app.services.bot_profile import owned_agent
from app.services.library_uploads import LibraryUploadUnavailable,LibraryUploadGone,LibraryUploadMissing

def fail(code,status=422):raise HTTPException(status,{'code':code})
def canonical_uuid(value):
 try:
  if not isinstance(value,str) or str(UUID(value))!=value:raise ValueError()
 except (ValueError,TypeError,AttributeError):fail('pin_invalid')
 return value

def source_value(value):
 if not isinstance(value,dict):fail('pin_invalid')
 kind=value.get('kind')
 if kind=='result' and set(value)=={'kind','room_id','result_id','version'}:
  if type(value['room_id']) is not int or not 1<=value['room_id']<=9223372036854775807 or type(value['version']) is not int or not 1<=value['version']<=9223372036854775807:fail('pin_invalid')
  canonical_uuid(value['result_id'])
 elif kind=='upload' and set(value)=={'kind','upload_id','sha256'}:
  canonical_uuid(value['upload_id'])
  if not isinstance(value['sha256'],str) or not re.fullmatch('[0-9a-f]{64}',value['sha256']):fail('pin_invalid')
 else:fail('pin_invalid')
 return value

def serial(value):return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True)
def key(value):return hashlib.sha256(serial(value).encode()).hexdigest()
def checked_body(body):
 if not isinstance(body,dict) or set(body)!={'operation_id','source','expected_revision','pinned','expected_owner_epoch'}:fail('pin_invalid')
 canonical_uuid(body['operation_id']);source_value(body['source'])
 if not isinstance(body['expected_owner_epoch'],str) or not re.fullmatch('[0-9a-f]{64}',body['expected_owner_epoch']):fail('pin_invalid')
 if type(body['expected_revision']) is not int or not 0<=body['expected_revision']<9223372036854775807 or type(body['pinned']) is not bool:fail('pin_invalid')
 return body

def availability(db,store,owner,bot,source):
 if source['kind']=='result':
  row=(db.query(RoomResultVersion).join(RoomResult,RoomResult.result_id==RoomResultVersion.result_id)
   .join(Conversation,Conversation.id==RoomResult.room_id).join(Project,Project.id==Conversation.project_id)
   .filter(RoomResult.owner_id==owner,RoomResult.room_id==source['room_id'],RoomResult.result_id==source['result_id'],
    RoomResultVersion.room_id==source['room_id'],RoomResultVersion.version==source['version'],RoomResultVersion.author_agent_id==bot,
    Conversation.user_id==owner,Conversation.is_active.is_(True),Project.user_id==owner,Project.is_active.is_(True)).first())
  if row is None:return 'unavailable'
  if row.kind=='file':
   from app.services.room_results import detail
   try:state=detail(db,owner,source['room_id'],source['result_id'],source['version']).get('file_status')
   except HTTPException:return 'unavailable'
   if state=='removed':return 'removed'
   if state=='unavailable':return 'unavailable'
   # File data can vanish independently of its database observation.
   return 'unknown'
  return 'available'
 if store is None:return 'unknown'
 try:meta=store.detail(owner,bot,source['upload_id'])
 except LibraryUploadGone:return 'removed'
 except LibraryUploadMissing:return 'unavailable'
 except LibraryUploadUnavailable:return 'unknown'
 if meta.get('sha256')!=source['sha256'] or meta.get('bot_id')!=bot or meta.get('upload_id')!=source['upload_id']:return 'unavailable'
 return 'removed' if meta.get('removed') is True else 'available'

def state_row(db,owner,bot,source):
 row=db.execute(text('SELECT source_json,revision,pinned FROM library_pins WHERE owner_id=:o AND bot_id=:b AND source_key=:k'),{'o':owner,'b':bot,'k':key(source)}).first()
 if row and row.source_json!=serial(source):fail('pin_source_conflict',409)
 return (row.revision,bool(row.pinned)) if row else (0,False)

def status(db,store,owner,bot,source):
 owned_agent(db,owner,bot);source_value(source);revision,pinned=state_row(db,owner,bot,source)
 return {'source':source,'revision':revision,'pinned':pinned,'availability':availability(db,store,owner,bot,source)}

def listing(db,store,owner,bot,after=None,limit=50):
 owned_agent(db,owner,bot)
 if after is not None and (not isinstance(after,str) or not re.fullmatch('[0-9a-f]{64}',after)):fail('pin_cursor_invalid')
 rows=db.execute(text('SELECT source_key,source_json,revision FROM library_pins WHERE owner_id=:o AND bot_id=:b AND pinned=1 AND source_key>:a ORDER BY source_key LIMIT :n'),{'o':owner,'b':bot,'a':after or '', 'n':limit+1}).all()
 items=[]
 for row in rows[:limit]:
  try:source=source_value(json.loads(row.source_json));assert key(source)==row.source_key
  except (ValueError,TypeError,AssertionError,HTTPException):fail('pin_record_invalid',503)
  items.append({'source':source,'revision':row.revision,'pinned':True,'availability':availability(db,store,owner,bot,source)})
 return {'items':items,'next_cursor':rows[limit-1].source_key if len(rows)>limit else None}

def owner_epoch(provider,owner):
 try:value=provider.current(owner) if provider else None
 except Exception:fail('pin_owner_unavailable',503)
 if not isinstance(value,str) or not re.fullmatch('[0-9a-f]{64}',value):fail('pin_owner_unavailable',503)
 return value

def operate(db,store,owner,bot,body,provider,owner_lock,expected_epoch,resolve=False):
 checked_body(body);digest=hashlib.sha256(serial(body).encode()).hexdigest();operation=body['operation_id'];source=body['source'];params={'o':owner,'b':bot,'id':operation}
 # Slow filesystem validation is outside both SQLite writer and Owner locks.
 upload_status=None
 if not resolve and body['pinned'] and source['kind']=='upload':
  owned_agent(db,owner,bot);db.rollback()
  upload_status=availability(db,store,owner,bot,source)
 db.rollback()
 # Keep the same checked-out connection while changing/restoring its timeout.
 # Session.rollback() alone can return a different connection to the pool.
 from sqlalchemy.orm import Session
 from sqlalchemy.exc import OperationalError
 conn=db.get_bind().connect();work=None;old_timeout=None
 try:
  old_timeout=conn.exec_driver_sql('PRAGMA busy_timeout').scalar_one()
  conn.exec_driver_sql('PRAGMA busy_timeout=0');conn.commit()
  work=Session(bind=conn)
  with owner_lock:
   if owner_epoch(provider,owner)!=expected_epoch or (not resolve and expected_epoch!=body['expected_owner_epoch']):fail('pin_owner_changed',409)
   try:work.execute(text('BEGIN IMMEDIATE'))
   except OperationalError:
    work.rollback();fail('pin_busy',503)
   try:
    owned_agent(work,owner,bot)
    prior=work.execute(text('SELECT request_digest,state,revision,pinned,code FROM library_pin_operations WHERE owner_id=:o AND bot_id=:b AND operation_id=:id'),params).first()
    if prior:
     if prior.request_digest!=digest:fail('pin_operation_conflict',409)
     result={'state':prior.state,'revision':prior.revision,'pinned':None if prior.pinned is None else bool(prior.pinned),'code':prior.code}
    else:
     revision,pinned=state_row(work,owner,bot,source);code=None
     source_status=(upload_status if source['kind']=='upload' else availability(work,store,owner,bot,source)) if not resolve and revision==body['expected_revision'] and body['pinned'] else None
     if resolve:state='fenced';code='pin_not_applied'
     elif revision!=body['expected_revision']:state='rejected';code='pin_revision_changed'
     elif body['pinned'] and not (source_status=='available' or source['kind']=='result' and source_status=='unknown'):state='rejected';code='pin_source_unavailable'
     else:
      state='applied'
      if pinned!=body['pinned']:
       revision+=1;pinned=body['pinned']
       work.execute(text('INSERT INTO library_pins(owner_id,bot_id,source_key,source_json,revision,pinned) VALUES(:o,:b,:k,:s,:r,:p) ON CONFLICT(owner_id,bot_id,source_key) DO UPDATE SET revision=excluded.revision,pinned=excluded.pinned'),{'o':owner,'b':bot,'k':key(source),'s':serial(source),'r':revision,'p':int(pinned)})
     result={'state':state,'revision':revision,'pinned':pinned,'code':code}
     work.execute(text('INSERT INTO library_pin_operations(owner_id,bot_id,operation_id,request_digest,state,revision,pinned,code) VALUES(:o,:b,:id,:d,:s,:r,:p,:c)'),{**params,'d':digest,'s':state,'r':revision,'p':int(pinned),'c':code})
    if owner_epoch(provider,owner)!=expected_epoch:fail('pin_owner_changed',409)
    work.commit();return {**result,'operation_id':operation,'source':source,'expected_revision':body['expected_revision'],'requested_pinned':body['pinned'],'expected_owner_epoch':body['expected_owner_epoch']}
   except BaseException:work.rollback();raise
 finally:
  if work is not None:work.close()
  try:
   if conn.in_transaction():conn.rollback()
   if old_timeout is not None:
    conn.exec_driver_sql('PRAGMA busy_timeout='+str(int(old_timeout)));conn.commit()
  finally:conn.close()
