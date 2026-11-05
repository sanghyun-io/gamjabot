"""Owner card reference data; the independent Core owns all card secrets."""
import hashlib,json,re
from datetime import datetime,timezone
from uuid import uuid4
from fastapi import HTTPException
from sqlalchemy import text
from app.models.models import User
_ID=re.compile(r'[0-9a-f]{32}\Z')
def fail(code,status=422):raise HTTPException(status,{'code':'vault_card_'+code})
def current(provider,owner,expected=None):
    try:epoch=provider.current(owner) if provider else None
    except Exception:epoch=None
    if not isinstance(epoch,str) or re.fullmatch(r'[0-9a-f]{64}',epoch) is None or (expected is not None and epoch!=expected):fail('unavailable',503)
    return epoch
def check_id(v):
    if not isinstance(v,str) or _ID.fullmatch(v) is None:fail('invalid')
    return v
def row_for(db,owner,item):
    row=db.execute(text('SELECT * FROM vault_cards WHERE owner_id=:o AND item_id=:i AND is_active=1'),{'o':owner,'i':check_id(item)}).mappings().first()
    if row is None:fail('not_found',404)
    result=dict(row);result['billing']=json.loads(result['billing']);return result
def status(provider,owner,row):
    unavailable={'version':1,'status':'unavailable','revision':None,'command_id':None,'last4':None}
    try:
        value=provider.vault_card_metadata_status(owner,row['item_id'],row['cardholder'],row['expiry_month'],row['expiry_year'],row['billing'],row['last4'])
        if not isinstance(value,dict) or set(value)!={'version','status','revision','command_id','last4'} or value['version']!=1 or value['status'] not in ('present','missing','deleted','metadata_mismatch','unavailable'):return unavailable
        rev,cmd,last=value['revision'],value['command_id'],value['last4']
        if rev is not None and (type(rev) is not int or rev<1):return unavailable
        if cmd is not None and (not isinstance(cmd,str) or _ID.fullmatch(cmd) is None):return unavailable
        if value['status']=='present':
            if rev is None or cmd is None or not isinstance(last,str) or re.fullmatch(r'[0-9]{4}',last) is None:return unavailable
        elif last is not None:return unavailable
        return value
    except Exception:return unavailable
def public(provider,owner,row):
    result={k:row[k] for k in ('item_id','cardholder','expiry_month','expiry_year','billing','nickname','hint','last4','revision','created_at','updated_at')}
    result['secret_status']=status(provider,owner,row);return result
def read(db,provider,owner,item):
    epoch=current(provider,owner);result=public(provider,owner,row_for(db,owner,item));current(provider,owner,epoch);return result
def list_items(db,provider,owner,after):
    epoch=current(provider,owner)
    if after is not None:check_id(after)
    rows=db.execute(text('SELECT item_id FROM vault_cards WHERE owner_id=:o AND is_active=1 AND item_id>:a ORDER BY item_id LIMIT 21'),{'o':owner,'a':after or ''}).scalars().all()
    result=[public(provider,owner,row_for(db,owner,i)) for i in rows[:20]];current(provider,owner,epoch)
    return {'version':1,'items':result,'next_cursor':rows[19] if len(rows)>20 else None}
def receipt(db,provider,owner,key):
    epoch=current(provider,owner);check_id(key);v=db.execute(text('SELECT response FROM vault_card_commands WHERE owner_id=:o AND command_key=:k'),{'o':owner,'k':key}).scalar_one_or_none();current(provider,owner,epoch)
    if v is None:fail('not_found',404)
    return json.loads(v)
def write(db,provider,owner,operation,item,value,key):
    check_id(key)
    if item is not None:check_id(item)
    epoch=current(provider,owner);payload=value.model_dump(mode='json');digest=hashlib.sha256(json.dumps([operation,item,payload],sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
    # Resolve already-completed commands without asking Core to repeat any effect.
    prior=db.execute(text('SELECT request_digest,response FROM vault_card_commands WHERE owner_id=:o AND command_key=:k'),{'o':owner,'k':key}).first()
    if prior:
        if prior.request_digest!=digest:fail('conflict',409)
        current(provider,owner,epoch);return json.loads(prior.response)
    proof=None;proof_row=None
    if operation in ('confirm','delete'):
        proof_row=row_for(db,owner,item)
        if proof_row['revision']!=value.expected_revision:fail('conflict',409)
        proof=status(provider,owner,proof_row);current(provider,owner,epoch)
        if operation=='confirm' and (proof['status']!='present' or proof['revision']!=value.secret_revision or proof['command_id']!=value.secret_command or (proof_row['last4'] is not None and proof_row['last4']!=proof['last4'])):fail('unavailable',503)
        if operation=='delete' and proof['status']!='deleted':fail('unavailable',503)
    db.rollback()
    try:
        db.execute(text('BEGIN IMMEDIATE'));user=db.get(User,owner)
        if user is None or not user.is_active:fail('unavailable',503)
        current(provider,owner,epoch)
        prior=db.execute(text('SELECT request_digest,response FROM vault_card_commands WHERE owner_id=:o AND command_key=:k'),{'o':owner,'k':key}).first()
        if prior:
            if prior.request_digest!=digest:fail('conflict',409)
            current(provider,owner,epoch);db.rollback();return json.loads(prior.response)
        now=datetime.now(timezone.utc).isoformat()
        if operation=='create':
            item=uuid4().hex;revision=1
            db.execute(text('INSERT INTO vault_cards(owner_id,item_id,cardholder,expiry_month,expiry_year,billing,nickname,hint,last4,revision,is_active,created_at,updated_at) VALUES(:o,:i,:cardholder,:expiry_month,:expiry_year,:billing,:nickname,:hint,NULL,1,1,:now,:now)'),{'o':owner,'i':item,'now':now,**{**payload,'billing':json.dumps(payload['billing'],ensure_ascii=False,separators=(',',':'))}})
        else:
            row=row_for(db,owner,item)
            if row['revision']!=value.expected_revision:fail('conflict',409)
            if proof_row is not None and any(row[k]!=proof_row[k] for k in ('revision','cardholder','expiry_month','expiry_year','billing','last4')):fail('conflict',409)
            revision=row['revision']+1
            if operation=='update':
                fields={k:payload[k] for k in ('cardholder','expiry_month','expiry_year','billing','nickname','hint')};fields['billing']=json.dumps(fields['billing'],ensure_ascii=False,separators=(',',':'))
                changed=db.execute(text('UPDATE vault_cards SET cardholder=:cardholder,expiry_month=:expiry_month,expiry_year=:expiry_year,billing=:billing,nickname=:nickname,hint=:hint,revision=:r,updated_at=:now WHERE owner_id=:o AND item_id=:i AND revision=:old AND is_active=1'),{'o':owner,'i':item,'r':revision,'old':row['revision'],'now':now,**fields}).rowcount
            elif operation=='confirm':
                changed=db.execute(text('UPDATE vault_cards SET last4=:last4,revision=:r,updated_at=:now WHERE owner_id=:o AND item_id=:i AND revision=:old AND is_active=1'),{'o':owner,'i':item,'r':revision,'old':row['revision'],'now':now,'last4':proof['last4']}).rowcount
            elif operation=='delete':
                changed=db.execute(text('UPDATE vault_cards SET is_active=0,revision=:r,updated_at=:now WHERE owner_id=:o AND item_id=:i AND revision=:old AND is_active=1'),{'o':owner,'i':item,'r':revision,'old':row['revision'],'now':now}).rowcount
            else:fail('invalid')
            if changed!=1:fail('conflict',409)
        result={'version':1,'operation':operation,'command_key':key,'item_id':item,'revision':revision}
        db.execute(text('INSERT INTO vault_card_commands(owner_id,command_key,request_digest,response) VALUES(:o,:k,:d,:v)'),{'o':owner,'k':key,'d':digest,'v':json.dumps(result,separators=(',',':'))});current(provider,owner,epoch);db.commit();return result
    except BaseException:db.rollback();raise
