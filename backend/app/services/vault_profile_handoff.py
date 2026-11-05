"""Durable exclusive custody for a reviewed Vault login and a Bot profile.

Only public bindings are persisted. Proxy authentication remains in memory.
"""
import asyncio,hashlib,json,os,re,shutil,stat,subprocess,time
from datetime import datetime,timedelta,timezone
from pathlib import Path
from uuid import uuid4
from fastapi import HTTPException
from app.services.action_review import ReviewChanged
from app.services.tools.browser import OriginProxy,origin
from app.services.tools.browser_profiles import UserAuthority,assigned_agents
from app.services.vault_logins import row_for

_HEX32=re.compile(r'[0-9a-f]{32}\Z');_HEX64=re.compile(r'[0-9a-f]{64}\Z')
_FIELDS={'version','handoff_id','owner_id','owner_epoch','profile_id','profile_revision','room_id','agent_id','action_id','review_id','root_request_event_id','execution_binding_sha256','item_id','item_revision','secret_revision','origin','username','profile_label','account_label','created_at','expires_at','state','prior_outcome_unknown'}
def fail(code='unavailable',status=409):raise HTTPException(status,{'code':'vault_handoff_'+code})
def canonical(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8')
def record(store,profile_id):
    path=store.directory(profile_id)/'handoff.json'
    try:
        st=path.lstat()
    except FileNotFoundError:return None
    if not stat.S_ISREG(st.st_mode) or st.st_uid!=os.getuid() or st.st_nlink!=1 or stat.S_IMODE(st.st_mode)!=0o600 or st.st_size>32768:fail('unsafe_storage')
    fd=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
    try:
        with os.fdopen(fd,'rb') as f:raw=f.read(32769)
        data=json.loads(raw)
        if len(raw)>32768 or not isinstance(data,dict) or set(data)!=_FIELDS or data['version']!=1 or data['profile_id']!=profile_id or data['state'] not in ('prepared','returned','recovered') or _HEX32.fullmatch(data['handoff_id']) is None:fail('unsafe_storage')
        if (any(type(data[k]) is not int or data[k]<1 for k in ('owner_id','profile_revision','room_id','agent_id','item_revision','secret_revision')) or type(data['prior_outcome_unknown']) is not bool or any(not isinstance(data[k],str) or not _HEX32.fullmatch(data[k]) for k in ('profile_id','item_id')) or any(not isinstance(data[k],str) or not _HEX64.fullmatch(data[k]) for k in ('owner_epoch','execution_binding_sha256')) or any(not isinstance(data[k],str) or len(data[k])>2048 for k in ('action_id','review_id','root_request_event_id','origin','username','profile_label','account_label','created_at','expires_at'))):fail('unsafe_storage')
        return data
    except HTTPException:raise
    except Exception:fail('unsafe_storage')
def hold(store,profile_id):
    r=record(store,profile_id)
    return bool(_pending_transition(store,profile_id) or r and r['state']=='prepared')
def write_record(store,data):
    raw=canonical(data)
    if len(raw)>32768 or set(data)!=_FIELDS:fail('unsafe_storage')
    directory=store.directory(data['profile_id']);target=directory/'handoff.json';tmp=directory/('.handoff-'+uuid4().hex)
    fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o600)
    try:
        with os.fdopen(fd,'wb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
        os.replace(tmp,target);dfd=os.open(directory,os.O_RDONLY)
        try:os.fsync(dfd)
        finally:os.close(dfd)
    finally:tmp.unlink(missing_ok=True)
    return hashlib.sha256(raw).hexdigest()
def _transition(store,profile_id,value):
    directory=store.directory(profile_id);target=directory/'handoff-transition.json'
    if value is None:
        target.unlink(missing_ok=True)
    else:
        raw=canonical(value)
        if len(raw)>32768:fail('unsafe_storage')
        temp=directory/('.transition-'+uuid4().hex)
        fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o600)
        try:
            with os.fdopen(fd,'wb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
            os.replace(temp,target)
        finally:temp.unlink(missing_ok=True)
    fd=os.open(directory,os.O_RDONLY)
    try:os.fsync(fd)
    finally:os.close(fd)
def _private_file_hash(path,*,optional=False):
    try:before=path.lstat()
    except FileNotFoundError:
        if optional:return None
        fail('unsafe_storage')
    if not stat.S_ISREG(before.st_mode) or before.st_uid!=os.getuid() or before.st_nlink!=1 or stat.S_IMODE(before.st_mode)!=0o600 or before.st_size>32768:fail('unsafe_storage')
    fd=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
    with os.fdopen(fd,'rb') as f:
        opened=os.fstat(f.fileno());raw=f.read(32769)
        if (opened.st_dev,opened.st_ino)!=(before.st_dev,before.st_ino) or len(raw)>32768:fail('unsafe_storage')
    return hashlib.sha256(raw).hexdigest()

def _recovery_hashes(store,profile_id):
    path=store.directory(profile_id)
    return (_metadata_hash(store,profile_id),_private_file_hash(path/'handoff.json'),
            _private_file_hash(path/'handoff-transition.json',optional=True))

def _planned_metadata_hash(metadata):
    return hashlib.sha256(json.dumps(metadata,ensure_ascii=False).encode('utf-8')).hexdigest()
def _record_hash(value):return hashlib.sha256(canonical(value)).hexdigest()

def _pending_transition(store,profile_id):
    path=store.directory(profile_id)/'handoff-transition.json'
    try:st=path.lstat()
    except FileNotFoundError:return None
    if not stat.S_ISREG(st.st_mode) or st.st_uid!=os.getuid() or st.st_nlink!=1 or stat.S_IMODE(st.st_mode)!=0o600 or st.st_size>32768:fail('unsafe_storage')
    fd=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
    try:
        with os.fdopen(fd,'rb') as f:data=json.loads(f.read(32769))
        if not isinstance(data,dict) or set(data)!={'version','handoff_id','phase','old_metadata_sha256','new_metadata_sha256','old_record_sha256','new_record_sha256'} or data['version']!=1 or data['phase'] not in ('close','release') or not _HEX32.fullmatch(data['handoff_id']) or any(not isinstance(data[k],str) or not _HEX64.fullmatch(data[k]) for k in ('old_metadata_sha256','new_metadata_sha256','old_record_sha256','new_record_sha256')):fail('unsafe_storage')
        return data
    except HTTPException:raise
    except Exception:fail('unsafe_storage')

def _begin_transition(store,old_record,new_record,old_metadata,new_metadata,phase):
    if _pending_transition(store,old_record['profile_id']):fail('changed')
    _transition(store,old_record['profile_id'],{'version':1,'handoff_id':old_record['handoff_id'],'phase':phase,'old_metadata_sha256':_metadata_hash(store,old_record['profile_id']),'new_metadata_sha256':_planned_metadata_hash(new_metadata),'old_record_sha256':_record_hash(old_record),'new_record_sha256':_record_hash(new_record)})

def _reconcile_transition(store,metadata,current,journal,*,commit=True):
    if journal['handoff_id']!=current['handoff_id']:fail('changed')
    mh=_metadata_hash(store,metadata['id']);rh=_record_hash(current)
    if mh not in (journal['old_metadata_sha256'],journal['new_metadata_sha256']) or rh not in (journal['old_record_sha256'],journal['new_record_sha256']):fail('changed')
    if journal['phase']=='close':
        old_revision=metadata['revision']-(mh==journal['new_metadata_sha256'])
        old=dict(current,profile_revision=old_revision,prior_outcome_unknown=True)
        new=dict(current,profile_revision=old_revision+1,prior_outcome_unknown=False)
        if _record_hash(old)!=journal['old_record_sha256'] or _record_hash(new)!=journal['new_record_sha256']:fail('changed')
        if mh==journal['old_metadata_sha256']:
            if rh!=journal['old_record_sha256']:fail('changed')
            result=old
        else:
            if commit and rh==journal['old_record_sha256']:write_record(store,new)
            result=new
    else:
        old_candidates=[dict(current,state='prepared',prior_outcome_unknown=x) for x in (False,True)]
        new_candidates=[dict(current,state=state,prior_outcome_unknown=x) for state in ('returned','recovered') for x in (False,True)]
        old=next((v for v in old_candidates if _record_hash(v)==journal['old_record_sha256']),None)
        new=next((v for v in new_candidates if _record_hash(v)==journal['new_record_sha256']),None)
        if old is None or new is None:fail('changed')
        if mh==journal['old_metadata_sha256']:
            if rh!=journal['old_record_sha256']:fail('changed')
            result=old
        else:
            if commit and rh==journal['old_record_sha256']:write_record(store,new)
            result=new
    if commit:_transition(store,metadata['id'],None)
    return result

def _review_failure(reason):
    error=ReviewChanged('Vault handoff review is unavailable')
    error.vault_handoff_reason=reason
    return error

def observed(executor,action):
    browser=getattr(executor,'browser',None);store=getattr(browser,'profiles',None)
    if store is None or browser.sessions is None or browser.owner_epochs is None:raise _review_failure('profile_unavailable')
    try:profile=store.selected(action)
    except ReviewChanged:raise _review_failure('profile_selection') from None
    except HTTPException:raise _review_failure('profile_storage') from None
    if not profile:raise _review_failure('profile_selection')
    try:held=hold(store,profile['id'])
    except HTTPException:raise _review_failure('profile_storage') from None
    if held:raise _review_failure('profile_held')
    from app.models.models import Conversation
    with browser.sessions() as db:
        room=db.get(Conversation,action.room_id)
        if not room or room.user_id!=profile['owner_id']:raise _review_failure('owner_changed')
        try:row=row_for(db,room.user_id,action.arguments['item_id'])
        except HTTPException:raise _review_failure('item_unavailable') from None
    epoch=browser.owner_epochs.current(room.user_id)
    if not isinstance(epoch,str) or not _HEX64.fullmatch(epoch):raise _review_failure('owner_epoch')
    secret=browser.owner_epochs.vault_metadata_status(room.user_id,row['item_id'],row['website'],row['username'])
    if browser.owner_epochs.current(room.user_id)!=epoch:raise _review_failure('owner_epoch')
    if not isinstance(secret,dict) or secret.get('status')!='present' or type(secret.get('revision')) is not int or secret['revision']<1:raise _review_failure('secret_unavailable')
    try:target=origin(row['website'])
    except Exception:raise _review_failure('origin_invalid') from None
    if target[0]!='https' or list(target) not in profile['origins']:raise _review_failure('origin_mismatch')
    pair=store.session(profile['id']);session=pair[1] if pair else None
    parked=browser.parked.get(profile['id']) if browser.parked_valid(profile) else None
    if session and (session.controller!='agent' or session.closing or session.page.is_closed() or session.room_id!=action.room_id or session.agent_id!=action.agent_id):raise _review_failure('profile_busy')
    if not session and ((profile['id'] in store.claims and not parked) or profile['id'] in store.transitions):raise _review_failure('profile_busy')
    return {'profile_id':profile['id'],'profile_revision':profile['revision'],'profile_label':profile['name'],'profile_origins':profile['origins'],'account_label':profile.get('account_label') or '', 'item_id':row['item_id'],'item_revision':row['revision'],'secret_revision':secret['revision'],'origin':row['website'],'username':row['username'],'context_id':session.context_id if session else (parked['park_id'] if parked else None)}
class HandoffProxy(OriginProxy):
    def __init__(self,authority):
        super().__init__(authority);self.connections=set()
    async def _serve(self,reader,writer):
        task=asyncio.current_task();self.connections.add(task)
        try:await super()._serve(reader,writer)
        finally:self.connections.discard(task)
    async def close(self):
        await super().close()
        tasks=tuple(self.connections)
        for task in tasks:task.cancel()
        if tasks:await asyncio.gather(*tasks,return_exceptions=True)

class HandoffAuthority(UserAuthority):
    def __init__(self,store,metadata,epoch,room_id,handoff_id):
        super().__init__(store,metadata,epoch,room_id);self.handoff_id=handoff_id
    def active(self):
        if not super().active():return False
        try:
            r=record(self.store,self.metadata['id']);expiry=datetime.fromisoformat(r['expires_at'])
            return bool(r['state']=='prepared' and r['handoff_id']==self.handoff_id and datetime.now(timezone.utc)<expiry)
        except Exception:return False

def _metadata_hash(store,profile_id):
    path=store.directory(profile_id)/'metadata.json';fd=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
    with os.fdopen(fd,'rb') as f:
        st=os.fstat(f.fileno());raw=f.read(16385)
        if not stat.S_ISREG(st.st_mode) or st.st_uid!=os.getuid() or st.st_nlink!=1 or stat.S_IMODE(st.st_mode)!=0o600 or len(raw)>16384:fail('unsafe_storage')
    return hashlib.sha256(raw).hexdigest()
def _match_target(target,value):
    return all(getattr(target,k)==v for k,v in value.items())
def _record_for(action,ref,target,owner,epoch,revision,unknown):
    binding=getattr(action,'execution_binding',None)
    root=binding.get('root_request_event_id') if isinstance(binding,dict) else action.trigger_event_id
    now=datetime.now(timezone.utc)
    return {'version':1,'handoff_id':uuid4().hex,'owner_id':owner,'owner_epoch':epoch,'profile_id':target.profile_id,'profile_revision':revision,'room_id':action.room_id,'agent_id':action.agent_id,'action_id':action.action_id,'review_id':ref.review_id,'root_request_event_id':root,'execution_binding_sha256':hashlib.sha256(canonical(binding)).hexdigest(),'item_id':target.item_id,'item_revision':target.item_revision,'secret_revision':target.secret_revision,'origin':target.origin,'username':target.username,'profile_label':target.profile_label,'account_label':target.account_label,'created_at':now.isoformat(),'expires_at':(now+timedelta(seconds=1800)).isoformat(),'state':'prepared','prior_outcome_unknown':unknown}
def _live(browser,action,ref,cancel):
    from app.services.resource_authority import require_scope_start,resource_live
    from app.services.review_execution import execution_supported
    if cancel.is_set() or ref.scope!='once' or ref.review is None or action.review_id!=ref.review_id:raise ReviewChanged('Review changed')
    execution_supported(ref.review);require_scope_start(browser.sessions,action,ref)
    if not resource_live(browser.sessions,ref,ref.run_generation,owner_epochs=browser.owner_epochs,allow_completed=False):raise ReviewChanged('Authority changed')
async def prepare(executor,action,ref,cancel):
    from app.services.tool_broker import ActionResult
    browser=executor.browser;store=browser.profiles;target=ref.review.targets[0];owner=ref.review.authority.owner_id;epoch=ref.review.authority.proposer.owner_epoch
    proxy=None
    async with store.operations:
        _live(browser,action,ref,cancel)
        before=observed(executor,action)
        if not _match_target(target,before):raise ReviewChanged('Reviewed Vault profile changed')
        m=store.read(target.profile_id,owner,action.room_id);old=record(store,m['id'])
        if old:
            if old['state']=='prepared':raise ReviewChanged('Profile is held')
            from app.models.collaboration import ToolAction
            with browser.sessions() as db:
                prior=db.get(ToolAction,old['action_id'])
                if not prior or prior.state not in ('completed','failed','denied','cancelled','uncertain'):raise ReviewChanged('Prior audit unavailable')
        # Fence first: a crash or uncertain close must not release the profile.
        r=_record_for(action,ref,target,owner,epoch,m['revision'],True);write_record(store,r)
        pair=store.session(m['id'])
        parked=browser.parked.get(m['id']) if browser.parked_valid(m) else None
        next_record=dict(r);next_metadata=dict(m);close_time=None
        if pair or parked:
            close_time=datetime.now(timezone.utc).isoformat();next_metadata['revision']+=1;next_metadata['last_used_at']=close_time
            next_record['profile_revision']=next_metadata['revision'];next_record['prior_outcome_unknown']=False
            _begin_transition(store,r,next_record,m,next_metadata,'close')
        try:
            if pair:
                key,session=pair
                if session.context_id!=target.context_id:raise ReviewChanged('Browser context changed')
                async with session.lock:await browser._close(key,session,handoff_last_used_at=close_time)
            elif parked:
                if parked['park_id']!=target.context_id:raise ReviewChanged('Browser context changed')
                if not await browser.close_parked(m['id'],expected_host=parked,handoff_last_used_at=close_time):raise ReviewChanged('Browser context changed')
            elif target.context_id is not None:raise ReviewChanged('Browser context disappeared')
            _live(browser,action,ref,cancel)
            updated=store.read(m['id'],owner,action.room_id)
            if updated['revision']!=m['revision']+(1 if pair or parked else 0):raise ReviewChanged('Profile revision changed')
            for field in set(m)-{'revision','last_used_at'}:
                if updated.get(field)!=m.get(field):raise ReviewChanged('Profile changed')
            with browser.sessions() as db:row=row_for(db,owner,target.item_id)
            secret=browser.owner_epochs.vault_metadata_status(owner,target.item_id,row['website'],row['username'])
            if (row['revision'],row['website'],row['username'],secret.get('status'),secret.get('revision'))!=(target.item_revision,target.origin,target.username,'present',target.secret_revision):raise ReviewChanged('Vault login changed')
            _live(browser,action,ref,cancel)
            fd=store.claim(updated,launch=False,handoff_recovery=True)
            try:
                if (pair or parked) and (_metadata_hash(store,m['id'])!=_planned_metadata_hash(next_metadata) or updated!=next_metadata):raise ReviewChanged('Close transition changed')
                if pair or parked:
                    metadata_fd=os.open(store.directory(m['id'])/'metadata.json',os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
                    try:os.fsync(metadata_fd)
                    finally:os.close(metadata_fd)
                r['profile_revision']=updated['revision'];r['prior_outcome_unknown']=False;rh=write_record(store,r)
                if pair or parked:_transition(store,m['id'],None)
                metadata_hash=_metadata_hash(store,m['id']);authority=HandoffAuthority(store,updated,epoch,action.room_id,r['handoff_id']);proxy=await HandoffProxy(authority).start()
                _live(browser,action,ref,cancel)
                response=await asyncio.to_thread(browser.owner_epochs.vault_profile_prepare,owner,r,metadata_hash,rh,proxy.port,proxy.secret)
                if not isinstance(response,dict) or response.get('status')!='prepared' or response.get('handoff_id')!=r['handoff_id']:raise ReviewChanged('Core handoff unavailable')
                _live(browser,action,ref,cancel)
                if not hasattr(store,'handoff_proxies'):store.handoff_proxies={}
                store.handoff_proxies[r['handoff_id']]=proxy
                async def watch(owned_proxy=proxy):
                    try:
                        while authority.active():await asyncio.sleep(.25)
                    finally:
                        await owned_proxy.close();store.handoff_proxies.pop(r['handoff_id'],None)
                asyncio.create_task(watch());proxy=None
            finally:store.release(updated['id'],fd)
        finally:
            if proxy is not None:await proxy.close()
    return ActionResult('completed','Awaiting Owner control of the browser.',safe_text='Awaiting Owner control of the browser.')

def unavailable(handoff_id):return {'version':1,'status':'unavailable','handoff_id':handoff_id,'profile_id':None,'profile_revision':None,'fill_status':'unknown'}
async def core_status(store,owner,r,operation='status'):
    try:
        result=await asyncio.to_thread(getattr(store.browser.owner_epochs,'vault_profile_'+operation),owner,r['handoff_id'])
        if (not isinstance(result,dict) or result.get('handoff_id')!=r['handoff_id'] or result.get('status') not in ('prepared','active','closed','unknown','unavailable')):return unavailable(r['handoff_id'])
        if result['status']!='unavailable' and (result.get('profile_id'),result.get('profile_revision'))!=(r['profile_id'],r['profile_revision']):return unavailable(r['handoff_id'])
        return result
    except Exception:return unavailable(r['handoff_id'])

async def custody_status(store,owner,room_id,profile_id):
    m=store.read(profile_id,owner,room_id);r=record(store,profile_id)
    if r is None:fail('not_found',404)
    unavailable={'version':1,'status':'unavailable','handoff_id':r['handoff_id'],'profile_id':None,'profile_revision':None,'custody_id':None}
    slot=getattr(store,'custody_slots',{}).get(r['handoff_id'])
    epoch=store.browser.owner_epochs.current(owner) if store.browser.owner_epochs else None
    if (not slot or slot.get('state') not in ('sealed','borrowed','unknown','borrowing','ending','parking')
            or not slot.get('custody_id') or not epoch or epoch!=r['owner_epoch']
            or r['state']!='prepared' or r['owner_id']!=owner or m['revision']!=r['profile_revision']
            or slot['profile_id']!=profile_id or slot['proxy'] is not getattr(store,'handoff_proxies',{}).get(r['handoff_id'])):return unavailable
    try:result=await asyncio.to_thread(store.browser.owner_epochs.vault_custody,owner,'status',{'handoff_id':r['handoff_id']})
    except Exception:result={}
    if (store.browser.owner_epochs.current(owner)!=epoch or store.read(profile_id,owner,room_id)!=m
            or record(store,profile_id)!=r or getattr(store,'custody_slots',{}).get(r['handoff_id']) is not slot):return unavailable
    unknown={'version':1,'status':'unknown','handoff_id':r['handoff_id'],'profile_id':profile_id,
             'profile_revision':m['revision'],'custody_id':slot['custody_id']}
    if (result.get('status') not in ('parked','borrowed','unknown') or result.get('custody_id')!=slot['custody_id']
            or result.get('profile_id')!=profile_id or result.get('profile_revision')!=m['revision']):return unknown
    if slot.get('state') in ('unknown','borrowing','ending','parking'):
        return unknown
    return {k:v for k,v in result.items() if k!='review_context_id'}

async def custody_seal(store,owner,room_id,profile_id,handoff_id,revision):
    async with store.operations:
        m=store.read(profile_id,owner,room_id);r=record(store,profile_id)
        if not r:fail('not_found',404)
        epoch=store.browser.owner_epochs.current(owner) if store.browser.owner_epochs else None
        if (r['owner_id']!=owner or r['state']!='prepared' or r['handoff_id']!=handoff_id
                or revision!=m['revision'] or revision!=r['profile_revision'] or not epoch or epoch!=r['owner_epoch']
                or r['prior_outcome_unknown'] or datetime.now(timezone.utc)>=datetime.fromisoformat(r['expires_at'])
                or _pending_transition(store,profile_id)):fail('changed')
        proxy=getattr(store,'handoff_proxies',{}).get(handoff_id)
        if proxy is None:fail('unavailable',503)
        if not hasattr(store,'custody_slots'):store.custody_slots={}
        if handoff_id in store.custody_slots:fail('changed')
        slot={'state':'unknown','profile_id':profile_id,'profile_revision':revision,'proxy':proxy,'custody_id':None,'owner':owner,
              'epoch':epoch,'room_id':r['room_id'],'agent_id':r['agent_id'],'cancelled':False,'active':None,'workflow_grants':set(),'workflow_proof':None}
        store.custody_slots[handoff_id]=slot
        await proxy.abort_connections(keep_paused=True)
        if (store.browser.owner_epochs.current(owner)!=epoch or store.read(profile_id,owner,room_id)!=m
                or record(store,profile_id)!=r):fail('changed')
        payload={'handoff_id':handoff_id,'profile_id':profile_id,'profile_revision':revision,
                 'profile_metadata_sha256':_metadata_hash(store,profile_id),'record_sha256':hashlib.sha256(canonical(r)).hexdigest()}
        slot.update(metadata_sha256=payload['profile_metadata_sha256'],record_sha256=payload['record_sha256'])
        response=await asyncio.to_thread(store.browser.owner_epochs.vault_custody,owner,'seal',payload)
        if (store.browser.owner_epochs.current(owner)!=epoch or store.read(profile_id,owner,room_id)!=m
                or record(store,profile_id)!=r):fail('changed')
        if response.get('status')=='sealed':
            slot.update(state='sealed',custody_id=response['custody_id'])
            if store.browser._watcher is None:store.browser._watcher=asyncio.create_task(store.browser._watch())
        return response

def custody_observed(store,metadata,action):
    r=record(store,metadata['id'])
    if not r:return None
    slot=getattr(store,'custody_slots',{}).get(r['handoff_id'])
    owner=metadata['owner_id'];provider=store.browser.owner_epochs
    if (not slot or slot.get('state')!='sealed' or slot.get('profile_id')!=metadata['id']
            or r['state']!='prepared' or r['prior_outcome_unknown'] or r['profile_revision']!=metadata['revision']
            or datetime.now(timezone.utc)>=datetime.fromisoformat(r['expires_at'])
            or provider is None or provider.current(owner)!=r['owner_epoch']
            or slot['proxy'] is not getattr(store,'handoff_proxies',{}).get(r['handoff_id'])
            or not slot['proxy']._quiescing or slot['proxy']._closing
            or slot['metadata_sha256']!=_metadata_hash(store,metadata['id'])
            or slot['record_sha256']!=hashlib.sha256(canonical(r)).hexdigest()):return None
    status=provider.vault_custody(owner,'status',{'handoff_id':r['handoff_id']})
    if (status.get('status') not in ('parked','borrowed') or status.get('custody_id')!=slot['custody_id']
            or status.get('profile_id')!=metadata['id'] or status.get('profile_revision')!=metadata['revision']
            or provider.current(owner)!=r['owner_epoch'] or store.read(metadata['id'])!=metadata
            or record(store,metadata['id'])!=r or getattr(store,'custody_slots',{}).get(r['handoff_id']) is not slot
            or slot['state']!='sealed' or not slot['proxy']._quiescing or slot['proxy']._closing):return None
    context_id=status.get('review_context_id')
    if not isinstance(context_id,str) or not _HEX32.fullmatch(context_id):return None
    if status['status']=='borrowed':
        from app.services.resource_authority import run_generation
        from app.services.resource_binding import freeze_binding
        proof=freeze_binding(getattr(action,'execution_binding',None));value=proof.as_dict() if proof else None
        expected=(action.room_id,action.agent_id,action.trigger_event_id,run_generation(store.browser.sessions,action.room_id,action.agent_id),action.workflow_id,hashlib.sha256(canonical(value)).hexdigest())
        if slot.get('workflow')!=expected or slot.get('context_id')!=context_id:return None
    elif slot.get('context_id') is not None:return None
    return {'record':r,'slot':slot,'custody_id':slot['custody_id'],'context_id':context_id}

class CustodyAuthority:
    def __init__(self,store,metadata,record_value,slot,base,allowed,expiry,task_identity=None):
        self.store=store;self.metadata=metadata;self.record_value=record_value;self.slot=slot;self.base=base;self.allowed=allowed;self.expiry=expiry;self.task_identity=task_identity
    def active(self):
        try:
            if self.task_identity is not None:
                from app.services.teammate_custody_authority import require_current
                with self.store.browser.sessions() as db:require_current(db,self.task_identity)
            return bool(self.slot.get('state')=='borrowed' and not self.slot.get('cancelled') and datetime.now(timezone.utc)<self.expiry and self.base.active()
                and self.store.browser.owner_epochs.current(self.metadata['owner_id'])==self.record_value['owner_epoch']
                and self.store.read(self.metadata['id'])==self.metadata and record(self.store,self.metadata['id'])==self.record_value
                and getattr(self.store,'custody_slots',{}).get(self.record_value['handoff_id']) is self.slot
                and self.slot['metadata_sha256']==_metadata_hash(self.store,self.metadata['id'])
                and self.slot['record_sha256']==hashlib.sha256(canonical(self.record_value)).hexdigest())
        except Exception:return False
    def broad_active(self):return False
    def allows(self,target):return self.active() and target==self.allowed and list(target) in self.metadata['origins'] and self.base.allows(target)

async def stop_custodies(browser,predicate):
    failed=False
    for handoff_id,slot in list(getattr(browser.profiles,'custody_slots',{}).items()):
        if not predicate(slot):continue
        slot['cancelled']=True;slot['state']='unknown'
        try:await slot['proxy'].abort_connections(keep_paused=True)
        except Exception:failed=True
        try:
            response=await _custody_revoke(browser.owner_epochs,slot,handoff_id)
            if (response.get('status')!='closed' or response.get('handoff_id')!=handoff_id
                    or response.get('profile_id')!=slot['profile_id'] or response.get('profile_revision')!=slot['profile_revision']):failed=True
        except Exception:failed=True
    if failed:raise RuntimeError('Browser custody stop unconfirmed')

async def _custody_revoke(provider,slot,handoff_id):
    task=slot.get('revoke_task')
    if task is None:
        task=asyncio.create_task(asyncio.to_thread(provider.vault_profile_revoke,slot['owner'],handoff_id))
        slot['revoke_task']=task
    return await asyncio.shield(task)

async def park_custody(browser,handoff_id,slot,expected,*,settled_failure=None,settled_actions=None):
    # Old workflow callbacks cannot park a replacement context.
    if (getattr(browser.profiles,'custody_slots',{}).get(handoff_id) is not slot
            or slot.get('state')!='sealed' or slot.get('cancelled') or slot.get('workflow')!=expected
            or not slot.get('context_id')):return False
    slot['state']='parking';context_id=slot['context_id']
    try:
        await slot['proxy'].abort_connections(keep_paused=True)
        room_id,agent_id,root_id,generation,workflow_id,_=expected
        response=await asyncio.to_thread(browser.owner_epochs.vault_custody,slot['owner'],'park',{
            'handoff_id':handoff_id,'custody_id':slot['custody_id'],'context_id':context_id,
            'room_id':room_id,'agent_id':agent_id,'root_request_event_id':root_id,
            'run_generation':generation,'workflow_id':workflow_id})
    except BaseException:
        slot['state']='unknown'
        raise
    def accept():
        if (getattr(browser.profiles,'custody_slots',{}).get(handoff_id) is not slot
                or slot.get('state')!='parking' or slot.get('cancelled') or slot.get('workflow')!=expected
                or slot.get('context_id')!=context_id or response.get('status')!='parked'
                or browser.owner_epochs.current(slot['owner'])!=slot['epoch']
                or response.get('custody_id')!=slot['custody_id'] or response.get('profile_id')!=slot['profile_id']
                or response.get('profile_revision')!=slot['profile_revision']
                or not isinstance(response.get('review_context_id'),str) or not _HEX32.fullmatch(response['review_context_id'])
                or response['review_context_id']==context_id):return False
        slot['context_id']=None;slot['workflow']=None;slot['workflow_grants']=set();slot['workflow_proof']=None
        slot['workflow_identity']=None;slot['workflow_actions']=set();slot['state']='sealed'
        return True
    if settled_failure is not None:
        # Never keep a SQL transaction over the Core call. Once Core replies,
        # serialize the final acceptance with a concurrent DB cancellation.
        from sqlalchemy import text
        from app.services.custody_settled_park import require_failed_readonly
        try:
            with browser.sessions() as db:
                db.execute(text('BEGIN IMMEDIATE'))
                require_failed_readonly(db,slot.get('workflow_identity'),failure_event_id=settled_failure,
                    action_ids=settled_actions if settled_actions is not None else frozenset(slot.get('workflow_actions',set())),
                    workflow_id=expected[4],owner_epochs=browser.owner_epochs)
                if accept():return True
        except Exception:pass
        slot['state']='unknown';slot['cancelled']=True
        try:await _custody_revoke(browser.owner_epochs,slot,handoff_id)
        except Exception:pass
        return False
    if accept():return True
    slot['state']='unknown';return False


def _unscoped_teammate_identity(browser,action,ref,owner):
    """Recover the exact scheduler-dispatched teammate without minting a scope."""
    from sqlalchemy import and_,or_
    from app.models.collaboration import RoomAgentRun
    from app.models.teammate import TeammateHandoff
    from app.services.teammate_custody_authority import bind
    with browser.sessions() as db:
        run=db.get(RoomAgentRun,(action.room_id,action.agent_id),populate_existing=True)
        if (not run or run.state!='running' or run.paused or run.generation!=ref.run_generation
                or run.trigger_event_id!=action.trigger_event_id):raise ReviewChanged('Browser task changed')
        pending=run.pending_task if isinstance(run.pending_task,dict) else {}
        job=pending.get('custody_teammate_job')
        if job is None:
            possible=db.query(TeammateHandoff).filter_by(room_id=action.room_id,root_request_event_id=action.trigger_event_id).filter(or_(
                and_(TeammateHandoff.recipient_agent_id==action.agent_id,TeammateHandoff.state=='running'),
                and_(TeammateHandoff.sender_agent_id==action.agent_id,TeammateHandoff.reply_state=='running'))).first()
            if possible is not None:raise ReviewChanged('Teammate identity is unavailable')
            return None
        if not isinstance(job,dict) or set(job)!={'handoff_id','phase'}:raise ReviewChanged('Teammate identity is unavailable')
        return bind(db,owner_id=owner,room_id=action.room_id,agent_id=action.agent_id,root_id=action.trigger_event_id,
                    generation=ref.run_generation,job=job)

async def park_completed_custodies(browser,room_id,agent_id,root_id,generation,proof,workflow_ids,*,settled_failure=None):
    from app.services.resource_binding import freeze_binding
    value=freeze_binding(proof);value=value.as_dict() if value else None
    digest=hashlib.sha256(canonical(value)).hexdigest()
    for handoff_id,slot in list(getattr(browser.profiles,'custody_slots',{}).items()):
        workflow=slot.get('workflow')
        if (workflow and workflow[:4]==(room_id,agent_id,root_id,generation)
                and workflow[4] in workflow_ids and workflow[5]==digest):
            actions=None
            if settled_failure is not None:
                from app.models.collaboration import ToolAction
                with browser.sessions() as db:
                    actions=frozenset(a.action_id for a in db.query(ToolAction).filter_by(
                        room_id=room_id,agent_id=agent_id,trigger_event_id=root_id,workflow_id=workflow[4]).all())
                if not slot.get('workflow_actions') or not slot['workflow_actions'].issubset(actions):
                    raise RuntimeError('Browser action evidence is incomplete')
            if not await park_custody(browser,handoff_id,slot,workflow,settled_failure=settled_failure,
                                      settled_actions=actions):raise RuntimeError('Browser custody park unconfirmed')

async def execute_custody(browser,action,ref,cancel,metadata,allowed,proof):
    from datetime import timedelta
    from app.services.grants import GrantRef
    from app.services.review_snapshot import _binding
    from app.services.review_execution import execution_supported,canonical_review_id
    from app.services.resource_authority import resource_live,require_scope_start
    from app.services.resource_binding import freeze_binding
    from app.services.tool_broker import ActionResult,prepare
    from app.services.tools.browser import WorkflowAuthority
    store=browser.profiles;owner=metadata['owner_id'];provider=browser.owner_epochs
    if action.family not in ('browser_navigate','browser_inspect','browser_interact','browser_screenshot') or (action.family=='browser_interact' and action.arguments.get('operation')!='click'):
        return ActionResult('failed','This browser action is not supported in the held profile.')
    if (not isinstance(ref,GrantRef)
            or ref.scope!='once' or ref.review is None or not canonical_review_id(ref.review_id)
            or action.review_id!=ref.review_id or _binding(action)!=ref.review.action or cancel.is_set()):
        raise ReviewChanged('Browser review changed')
    execution_supported(ref.review)
    if require_scope_start(browser.sessions,action,ref)!=proof:raise ReviewChanged('Browser scope changed')
    prepared=prepare(action.family,action.arguments,action.workflow_id)
    if any(getattr(prepared,k)!=getattr(ref.review.action,k) for k in ('family','target','scope_target','expected_effect','payload_digest')):raise ReviewChanged('Browser proposal changed')
    current=custody_observed(store,metadata,action)
    target=ref.review.targets[0] if len(ref.review.targets)==1 else None
    if (not current or not target or target.status!='verified' or target.context_id!=current['context_id']
            or (target.profile_id,target.profile_revision)!=(metadata['id'],metadata['revision'])
            or (target.room_id,target.agent_id,target.session_id,target.workflow_id)!=(action.room_id,action.agent_id,action.session_id,action.workflow_id)
            or ref.review.authority.owner_id!=owner or ref.review.authority.proposer.owner_epoch!=current['record']['owner_epoch']
            or not resource_live(browser.sessions,ref,ref.run_generation,owner_epochs=provider,allow_completed=False)):
        raise ReviewChanged('Browser custody changed')
    r=current['record'];slot=current['slot'];proxy=slot['proxy'];url=action.arguments['url']
    if allowed[0]!='https' or list(allowed) not in metadata['origins'] or not isinstance(url,str):raise ReviewChanged('Browser origin changed')
    expiry=min(ref.approved_at.replace(tzinfo=timezone.utc)+timedelta(seconds=60),datetime.fromisoformat(r['expires_at'])) if ref.approved_at else None
    if expiry is None or expiry<=datetime.now(timezone.utc):raise ReviewChanged('Browser approval expired')
    binding=freeze_binding(proof);binding_value=binding.as_dict() if binding else None
    task_identity=_unscoped_teammate_identity(browser,action,ref,owner) if binding is None else None
    if slot.get('workflow') is not None and slot.get('workflow_identity')!=task_identity:
        raise ReviewChanged('Browser task identity changed')
    host=allowed[1];origin_value='https://'+(f'[{host}]' if ':' in host else host)+((':'+str(allowed[2])) if allowed[2]!=443 else '')
    borrow_id=uuid4().hex;operation={'browser_navigate':'navigate','browser_inspect':'inspect','browser_interact':'click','browser_screenshot':'screenshot'}[action.family]
    selector=action.arguments.get('selector') if operation=='click' else None
    if operation=='click':
        import unicodedata
        try: valid_selector=isinstance(selector,str) and 1<=len(selector.encode('utf-8'))<=4096 and not any(unicodedata.category(ch)=='Cc' for ch in selector)
        except UnicodeError: valid_selector=False
        if not valid_selector:return ActionResult('failed','The held browser requires a valid standard CSS selector.')
    payload={'handoff_id':r['handoff_id'],'profile_id':metadata['id'],'profile_revision':metadata['revision'],
        'profile_metadata_sha256':slot['metadata_sha256'],'record_sha256':slot['record_sha256'],
        'custody_id':slot['custody_id'],'borrow_id':borrow_id,'review_context_id':current['context_id'],'workflow_id':action.workflow_id,'room_id':action.room_id,'agent_id':action.agent_id,
        'action_id':action.action_id,'review_id':ref.review_id,'root_request_event_id':action.trigger_event_id,
        'run_generation':ref.run_generation,'execution_binding_sha256':hashlib.sha256(canonical(binding_value)).hexdigest(),
        'execution_binding':binding_value,
        'context_mode':binding_value['context_mode'] if binding_value else 'user_room','origin':origin_value,'scope':'once',
        'operation':operation,'url':url,'selector':selector,'url_sha256':hashlib.sha256(url.encode('utf-8')).hexdigest(),
        'payload_digest':ref.review.action.payload_digest,'call_id':action.call_id,'expires_at':expiry.isoformat()}
    slot['workflow_identity']=task_identity;slot.setdefault('workflow_actions',set()).add(action.action_id);slot['state']='borrowing';slot['cancelled']=False
    slot['active']={'room_id':action.room_id,'agent_id':action.agent_id,'grant_id':ref.grant_id,'execution_binding':proof}
    borrow_task=asyncio.create_task(asyncio.to_thread(provider.vault_custody,owner,'borrow',payload))
    try:response=await asyncio.shield(borrow_task)
    except asyncio.CancelledError:
        slot['cancelled']=True;slot['state']='unknown'
        await proxy.abort_connections(keep_paused=True)
        try:await asyncio.shield(borrow_task)
        except Exception:pass
        await _custody_revoke(provider,slot,r['handoff_id'])
        raise
    except Exception:
        slot['cancelled']=True;slot['state']='unknown'
        await _custody_revoke(provider,slot,r['handoff_id'])
        return ActionResult('uncertain','Browser transfer could not be confirmed.')
    if response.get('status')!='borrowed':
        slot['state']='unknown'
        return ActionResult('uncertain','Browser transfer could not be confirmed.')
    if slot['cancelled'] or slot['state']!='borrowing':
        slot['state']='unknown'
        await _custody_revoke(provider,slot,r['handoff_id'])
        return ActionResult('uncertain','Browser transfer could not be confirmed.')
    context_id=response.get('context_id')
    if not isinstance(context_id,str) or not _HEX32.fullmatch(context_id) or context_id!=current['context_id']:
        slot['state']='unknown'
        return ActionResult('uncertain','Browser transfer could not be confirmed.')
    slot['context_id']=context_id
    slot['workflow']=(action.room_id,action.agent_id,action.trigger_event_id,ref.run_generation,action.workflow_id,payload['execution_binding_sha256'])
    slot.setdefault('workflow_grants',set()).add(ref.grant_id);slot['workflow_proof']=proof
    base=WorkflowAuthority(browser.sessions,action.room_id,action.agent_id,owner_epochs=provider,
        workflow_id=action.workflow_id,execution_binding=proof);base.add(allowed,ref)
    authority=CustodyAuthority(store,metadata,r,slot,base,allowed,expiry,task_identity);slot['state']='borrowed'
    result=None;interrupted=False;image_data=None
    try:
        if cancel.is_set() or datetime.now(timezone.utc)>=expiry or not authority.active() or not authority.allows(allowed):raise ReviewChanged('Browser authority changed')
        proxy.reviewed_origin=allowed;before=dict(proxy.blocked_reasons);proxy.resume(authority)
        call={'handoff_id':r['handoff_id'],'custody_id':slot['custody_id'],'borrow_id':borrow_id,
              'action_id':action.action_id,'operation':operation,'url':url,'selector':selector,'url_sha256':payload['url_sha256']}
        async def exchange(kind,value):
            task=asyncio.create_task(asyncio.to_thread(provider.vault_custody,owner,kind,value))
            try:return await asyncio.shield(task)
            except asyncio.CancelledError:
                await proxy.abort_connections(keep_paused=True)
                try:await asyncio.shield(task)
                except Exception:pass
                raise
        try:
            result=await exchange('call_start',call)
            deadline=time.monotonic()+(17 if operation=='navigate' else 12)
            poll={'handoff_id':r['handoff_id'],'custody_id':slot['custody_id'],'borrow_id':borrow_id,'action_id':action.action_id}
            while result.get('status')=='pending':
                if cancel.is_set() or not authority.active() or time.monotonic()>=deadline:
                    result={'status':'unknown'};break
                try:await asyncio.wait_for(cancel.wait(),timeout=.2)
                except asyncio.TimeoutError:pass
                if cancel.is_set():result={'status':'unknown'};break
                result=await exchange('call_poll',poll)
            if operation=='screenshot' and result.get('status')=='completed':
                import base64, binascii
                def unique_pairs(pairs):
                    obj={}
                    for key,value in pairs:
                        if key in obj:raise ValueError('Invalid capture metadata')
                        obj[key]=value
                    return obj
                capture=json.loads(result['text'],object_pairs_hook=unique_pairs)
                if (type(capture) is not dict or set(capture)!={'version','capture_id','bytes','sha256','chunks'}
                        or type(capture['version']) is not int or capture['version']!=1
                        or not isinstance(capture['capture_id'],str) or not _HEX32.fullmatch(capture['capture_id'])
                        or type(capture['bytes']) is not int or not 1<=capture['bytes']<=10000000
                        or not isinstance(capture['sha256'],str) or not re.fullmatch(r'[0-9a-f]{64}',capture['sha256'])
                        or type(capture['chunks']) is not int or capture['chunks']!=(capture['bytes']+23999)//24000):
                    raise ValueError('Invalid capture metadata')
                capture_ref={'handoff_id':r['handoff_id'],'custody_id':slot['custody_id'],'borrow_id':borrow_id,
                    'action_id':action.action_id,'context_id':context_id,'capture_id':capture['capture_id']}
                data=bytearray();capture_deadline=time.monotonic()+60
                try:
                    for index in range(capture['chunks']):
                        if cancel.is_set() or time.monotonic()>=capture_deadline or not authority.active() or not authority.allows(allowed):
                            raise ValueError('Capture authority unavailable')
                        part=await exchange('capture_chunk',{**capture_ref,'index':index})
                        if (part.get('status')!='ok' or part.get('capture_id')!=capture['capture_id']
                                or part.get('index')!=index or part.get('chunks')!=capture['chunks']):
                            raise ValueError('Capture chunk unavailable')
                        try:chunk=base64.b64decode(part['data'],validate=True)
                        except (ValueError,TypeError,binascii.Error):raise ValueError('Invalid capture chunk') from None
                        expected=min(24000,capture['bytes']-index*24000)
                        if len(chunk)!=expected or base64.b64encode(chunk).decode('ascii')!=part['data']:
                            raise ValueError('Invalid capture chunk size')
                        data.extend(chunk)
                    if (len(data)!=capture['bytes'] or hashlib.sha256(data).hexdigest()!=capture['sha256']
                            or cancel.is_set() or not authority.active() or not authority.allows(allowed)):
                        raise ValueError('Capture confirmation unavailable')
                    image_data=bytes(data)
                finally:
                    data.clear()
                    released=await exchange('capture_release',capture_ref)
                    if released.get('status')!='released':raise ValueError('Capture release unavailable')
        except asyncio.CancelledError:
            interrupted=True;result={'status':'unknown'}
        except Exception:
            result={'status':'unknown'}
        if result and result.get('status')=='completed' and (cancel.is_set() or datetime.now(timezone.utc)>=expiry or not authority.active() or not authority.allows(allowed)):
            result={'status':'unknown'}
    finally:
        slot['state']='ending'
        try:await proxy.abort_connections(keep_paused=True)
        except BaseException:
            slot['cancelled']=True;slot['state']='unknown'
            try:await _custody_revoke(provider,slot,r['handoff_id'])
            except Exception:pass
            raise
        if interrupted or cancel.is_set() or slot['cancelled'] or isinstance(result,dict) and result.get('status') in ('pending','unknown'):
            slot['cancelled']=True;slot['state']='unknown'
            await _custody_revoke(provider,slot,r['handoff_id'])
        else:
            end=await asyncio.to_thread(provider.vault_custody,owner,'end',{'handoff_id':r['handoff_id'],'custody_id':slot['custody_id'],'borrow_id':borrow_id,'context_id':context_id})
            slot['state']='sealed' if (not slot['cancelled'] and end.get('status')=='borrowed' and end.get('custody_id')==slot['custody_id']
                and end.get('profile_id')==metadata['id'] and end.get('profile_revision')==metadata['revision']
                and end.get('review_context_id')==context_id) else 'unknown'
        slot['active']=None
    if interrupted:raise asyncio.CancelledError
    if action.workflow_id is None and slot['state']=='sealed':
        if not await park_custody(browser,r['handoff_id'],slot,slot['workflow']):
            return ActionResult('uncertain','Browser session park could not be confirmed.')
    if slot['state']!='sealed' or not isinstance(result,dict) or result.get('status')=='unknown':return ActionResult('uncertain','Browser result could not be confirmed.')
    if result.get('status')!='completed':return ActionResult('failed','Browser request could not be completed.')
    try:
        if (cancel.is_set() or provider.current(owner)!=r['owner_epoch'] or store.read(metadata['id'])!=metadata
                or record(store,metadata['id'])!=r or getattr(store,'custody_slots',{}).get(r['handoff_id']) is not slot
                or not resource_live(browser.sessions,ref,ref.run_generation,owner_epochs=provider,allow_completed=False)
                or require_scope_start(browser.sessions,action,ref)!=proof):
            return ActionResult('uncertain','Browser result could not be confirmed.')
    except Exception:return ActionResult('uncertain','Browser result could not be confirmed.')
    reasons=[{'reason':reason,'count':min(count-before.get(reason,0),1000000)}
             for reason,count in sorted(proxy.blocked_reasons.items()) if count>before.get(reason,0)]
    if reasons and any(item['reason']!='authority_other_origin' for item in reasons):
        if operation=='click':return ActionResult('uncertain','Browser click effect could not be confirmed.')
        return ActionResult('failed','Browser blocked an out-of-scope request.')
    if operation=='screenshot':
        if reasons:return ActionResult('failed','Browser blocked an out-of-scope request.')
        from app.services.tools.desktop import validate_screenshot_png,save_screenshot_png
        try:
            if cancel.is_set() or image_data is None or browser.app_data is None:raise ValueError('Capture unavailable')
            width,height=validate_screenshot_png(image_data)
            ref_path=save_screenshot_png(browser.app_data,action,image_data)
        except (OSError,ValueError,TypeError,SyntaxError):
            return ActionResult('failed','Browser screenshot could not be validated or saved.',error_code='screenshot_artifact_unavailable')
        return ActionResult('completed','Browser page captured.',screenshot_ref=ref_path,
                            image_bytes=image_data,image_mime='image/png',image_width=width,image_height=height)
    warning='Some requests to other sites were blocked; the page may be incomplete.\n' if reasons else ''
    diagnostic=({'browser_proxy':reasons,'main_frame_origin':'approved','main_response':'ok' if operation=='navigate' else 'unavailable'} if reasons else None)
    click_notice='Standard CSS synthetic click dispatched to one visible enabled element; this is not a trusted pointer click or confirmation of site processing.\n' if operation=='click' else ''
    return ActionResult('completed',warning+click_notice+result['text'],diagnostic=diagnostic)
async def view(store,owner,room_id,profile_id):
    m=store.read(profile_id,owner,room_id);r=record(store,profile_id)
    if r is None:return {'version':1,'profile_id':profile_id,'revision':m['revision'],'allowed_origins':m['origins'],'handoff':None,'native_status':None,'can_fill':False,'can_return':False,'can_recover':False}
    if r['owner_id']!=owner:fail('unavailable')
    epoch=store.browser.owner_epochs.current(owner) if store.browser.owner_epochs else None
    native=await core_status(store,owner,r)
    if not epoch or store.browser.owner_epochs.current(owner)!=epoch or store.read(profile_id,owner,room_id)!=m or record(store,profile_id)!=r:fail('changed')
    active=bool(not _pending_transition(store,profile_id) and r['state']=='prepared' and epoch==r['owner_epoch'] and m['revision']==r['profile_revision'])
    try:unexpired=datetime.now(timezone.utc)<datetime.fromisoformat(r['expires_at'])
    except Exception:unexpired=False
    return {'version':1,'profile_id':profile_id,'revision':m['revision'],'allowed_origins':m['origins'],'handoff':r,'native_status':native,'can_fill':bool(active and unexpired and not r['prior_outcome_unknown'] and native['status'] in ('prepared','active')),'can_return':bool(active and native['status']=='closed'),'can_recover':bool(r['state']=='prepared' or _pending_transition(store,profile_id))}
def _remove_attempt_marker(store,profile_id,handoff_id):
    if not _HEX32.fullmatch(handoff_id):fail('cleanup_pending')
    directory=store.directory(profile_id);name=f'vault-attempt-{handoff_id}.json'
    dfd=None;fd=None
    try:
        dfd=os.open(directory,os.O_RDONLY|getattr(os,'O_DIRECTORY',0)|getattr(os,'O_NOFOLLOW',0))
        try:before=os.stat(name,dir_fd=dfd,follow_symlinks=False)
        except FileNotFoundError:return
        if (not stat.S_ISREG(before.st_mode) or before.st_uid!=os.getuid() or before.st_nlink!=1
                or stat.S_IMODE(before.st_mode)!=0o600 or before.st_size!=33):fail('cleanup_pending')
        fd=os.open(name,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0),dir_fd=dfd)
        opened=os.fstat(fd);raw=os.read(fd,34)
        after=os.stat(name,dir_fd=dfd,follow_symlinks=False)
        if (raw!=b'{"version":1,"state":"attempted"}' or (opened.st_dev,opened.st_ino)!=(before.st_dev,before.st_ino)
                or (after.st_dev,after.st_ino)!=(opened.st_dev,opened.st_ino)
                or not stat.S_ISREG(after.st_mode) or after.st_uid!=os.getuid() or after.st_nlink!=1
                or stat.S_IMODE(after.st_mode)!=0o600):fail('cleanup_pending')
        os.unlink(name,dir_fd=dfd);os.fsync(dfd)
    except HTTPException:raise
    except OSError:fail('cleanup_pending')
    finally:
        if fd is not None:os.close(fd)
        if dfd is not None:os.close(dfd)

def _marker_absent(store,profile_id,handoff_id):
    path=store.directory(profile_id)/f'vault-attempt-{handoff_id}.json'
    try:path.lstat()
    except FileNotFoundError:return True
    return False

def _remove_custody_receipt(store,r,lease_fd):
    profile_id=r['profile_id'];handoff_id=r['handoff_id']
    if store.claims.get(profile_id)!=lease_fd or r['state'] not in ('returned','recovered') or _pending_transition(store,profile_id):fail('cleanup_pending')
    directory=store.directory(profile_id);name=f'custody-{handoff_id}.json';dfd=None;fd=None
    try:
        dfd=os.open(directory,os.O_RDONLY|getattr(os,'O_DIRECTORY',0)|getattr(os,'O_NOFOLLOW',0))
        try:before=os.stat(name,dir_fd=dfd,follow_symlinks=False)
        except FileNotFoundError:return
        if (not stat.S_ISREG(before.st_mode) or before.st_uid!=os.getuid() or before.st_nlink!=1
                or stat.S_IMODE(before.st_mode)!=0o600 or before.st_size>4096):fail('cleanup_pending')
        fd=os.open(name,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0),dir_fd=dfd)
        opened=os.fstat(fd);raw=os.read(fd,4097);after=os.stat(name,dir_fd=dfd,follow_symlinks=False)
        value=json.loads(raw)
        if (not isinstance(value,dict) or set(value)!={'version','status','handoff_id','profile_id','profile_revision','custody_id'}
                or type(value['version']) is not int or value['version']!=1 or value['status']!='parked'
                or type(value['profile_revision']) is not int
                or (value['handoff_id'],value['profile_id'],value['profile_revision'])!=(handoff_id,profile_id,r['profile_revision'])
                or not isinstance(value['custody_id'],str) or not _HEX32.fullmatch(value['custody_id'])
                or (opened.st_dev,opened.st_ino)!=(before.st_dev,before.st_ino)
                or (after.st_dev,after.st_ino)!=(opened.st_dev,opened.st_ino)
                or not stat.S_ISREG(after.st_mode) or after.st_uid!=os.getuid() or after.st_nlink!=1
                or stat.S_IMODE(after.st_mode)!=0o600):fail('cleanup_pending')
        os.unlink(name,dir_fd=dfd);os.fsync(dfd)
    except HTTPException:raise
    except (OSError,ValueError,TypeError):fail('cleanup_pending')
    finally:
        if fd is not None:os.close(fd)
        if dfd is not None:os.close(dfd)

def retire_profile_handoff(store,metadata,lease_fd,expected_epoch,*,retire):
    """Validate one terminal record and preserve its public audit before removal."""
    from app.models.collaboration import RoomEvent,ToolAction
    from app.models.models import Conversation
    from app.services.events import ActorRef,append_event
    profile_id=metadata['id'];directory=store.directory(profile_id)
    if (store.claims.get(profile_id)!=lease_fd or not expected_epoch or not store.browser.owner_epochs
            or store.browser.owner_epochs.current(metadata['owner_id'])!=expected_epoch):fail('unavailable')
    allowed={'data','metadata.json','lease.lock','handoff.json'}
    def check_directory():
        if {p.name for p in directory.iterdir()}-allowed:fail('unsafe_storage')
        if store.read(profile_id,metadata['owner_id'])!=metadata:fail('changed')
        lease=os.fstat(lease_fd);current=(directory/'lease.lock').lstat()
        if (not stat.S_ISREG(current.st_mode) or current.st_uid!=os.getuid() or current.st_nlink!=1
                or stat.S_IMODE(current.st_mode)!=0o600 or (lease.st_dev,lease.st_ino)!=(current.st_dev,current.st_ino)):fail('unsafe_storage')
    check_directory()
    r=record(store,profile_id)
    if r is None:return
    if (r['owner_id']!=metadata['owner_id'] or r['profile_id']!=profile_id
            or r['state'] not in ('returned','recovered') or _pending_transition(store,profile_id)
            or not _marker_absent(store,profile_id,r['handoff_id'])):fail('cleanup_pending')
    if not retire:return
    path=directory/'handoff.json';st=path.lstat();digest=_private_file_hash(path)
    if store.browser.sessions is None:fail('audit_unavailable')
    with store.browser.sessions() as db:
        action=db.get(ToolAction,r['action_id']);room=db.get(Conversation,r['room_id'])
        audit=db.query(RoomEvent).filter_by(room_id=r['room_id'],idempotency_key=f"vault-handoff:{r['handoff_id']}:{'return' if r['state']=='returned' else 'recover'}").first()
        status='return_authorized' if r['state']=='returned' else 'recovery_authorized'
        if (not room or room.user_id!=metadata['owner_id'] or not action or action.family!='request_vault_login'
                or action.room_id!=r['room_id'] or action.agent_id!=r['agent_id'] or action.review_id!=r['review_id']
                or action.approval_owner_id!=metadata['owner_id'] or action.approval_scope!='once'
                or action.state not in ('completed','failed','denied','cancelled','uncertain') or not audit
                or audit.kind!='activity' or audit.actor_type!='system' or audit.actor_id is not None
                or not isinstance(audit.payload,dict) or audit.payload.get('category')!='vault_profile_handoff'
                or audit.payload.get('action_id')!=r['action_id'] or audit.payload.get('handoff_id')!=r['handoff_id']
                or audit.payload.get('status')!=status):fail('audit_unavailable')
        append_event(db,r['room_id'],ActorRef('system',None),'activity',
            {'category':'vault_profile_handoff','status':'profile_retired','handoff':r},
            f"vault-handoff:{r['handoff_id']}:profile-retired",allow_inactive=True)
    if store.browser.owner_epochs.current(metadata['owner_id'])!=expected_epoch:fail('unavailable')
    check_directory()
    if record(store,profile_id)!=r or _private_file_hash(path)!=digest:fail('changed')
    dfd=None;handoff_fd=None
    try:
        dfd=os.open(directory,os.O_RDONLY|getattr(os,'O_DIRECTORY',0)|getattr(os,'O_NOFOLLOW',0))
        handoff_fd=os.open('handoff.json',os.O_RDONLY|getattr(os,'O_NOFOLLOW',0),dir_fd=dfd)
        opened=os.fstat(handoff_fd);current=os.stat('handoff.json',dir_fd=dfd,follow_symlinks=False)
        if (not stat.S_ISREG(current.st_mode) or current.st_uid!=os.getuid() or current.st_nlink!=1
                or stat.S_IMODE(current.st_mode)!=0o600 or (opened.st_dev,opened.st_ino)!=(st.st_dev,st.st_ino)
                or (current.st_dev,current.st_ino)!=(opened.st_dev,opened.st_ino)):fail('unsafe_storage')
        os.unlink('handoff.json',dir_fd=dfd);os.fsync(dfd)
    except HTTPException:raise
    except OSError:fail('cleanup_pending')
    finally:
        if handoff_fd is not None:os.close(handoff_fd)
        if dfd is not None:os.close(dfd)

def _no_profile_references(store,profile_id):
    if store.session(profile_id) or profile_id in store.transitions:fail('busy')
    data=store.directory(profile_id)/'data'
    if data.is_symlink() or not data.is_dir():fail('unsafe_storage')
    binary=shutil.which('lsof') or ('/usr/sbin/lsof' if Path('/usr/sbin/lsof').exists() else None)
    if not binary:fail('close_unconfirmed')
    result=subprocess.run([binary,'+D',str(data)],capture_output=True,timeout=10)
    if result.returncode!=1 or result.stdout or result.stderr:fail('close_unconfirmed')
async def finish(store,owner,room_id,profile_id,handoff_id,revision,*,recover=False):
    from app.models.collaboration import ToolAction
    from app.services.events import ActorRef,append_event
    async with store.operations:
        m=store.read(profile_id,owner,room_id);r=record(store,profile_id);epochs=store.browser.owner_epochs
        epoch=epochs.current(owner) if epochs else None
        if not epoch or not r or r['owner_id']!=owner or r['handoff_id']!=handoff_id or m['revision']!=revision:fail('changed')
        journal=_pending_transition(store,profile_id)
        if journal and not recover:fail('changed')
        if not journal and r['state'] in ('returned','recovered'):
            if r['state']==('recovered' if recover else 'returned'):
                if not _marker_absent(store,profile_id,handoff_id):fail('cleanup_pending')
                receipt_path=store.directory(profile_id)/f'custody-{handoff_id}.json'
                if receipt_path.exists() or receipt_path.is_symlink():
                    from app.models.collaboration import RoomEvent
                    audit_status='recovery_authorized' if recover else 'return_authorized'
                    with store.browser.sessions() as db:
                        action=db.get(ToolAction,r['action_id'])
                        audit=db.query(RoomEvent).filter_by(room_id=r['room_id'],idempotency_key=f"vault-handoff:{handoff_id}:{'recover' if recover else 'return'}").first()
                        if (not action or action.review_id!=r['review_id'] or action.room_id!=r['room_id'] or action.agent_id!=r['agent_id']
                                or not audit or audit.kind!='activity' or audit.actor_type!='system' or audit.actor_id is not None
                                or not isinstance(audit.payload,dict) or audit.payload.get('category')!='vault_profile_handoff'
                                or audit.payload.get('action_id')!=r['action_id'] or audit.payload.get('handoff_id')!=handoff_id
                                or audit.payload.get('status')!=audit_status):fail('audit_unavailable')
                    retry_fd=store.claim(m,launch=False,handoff_recovery=True)
                    try:
                        await asyncio.to_thread(_no_profile_references,store,profile_id)
                        if epochs.current(owner)!=epoch or store.read(profile_id,owner,room_id)!=m or record(store,profile_id)!=r:fail('changed')
                        _remove_custody_receipt(store,r,retry_fd)
                    finally:store.release(profile_id,retry_fd)
                proxies=getattr(store,'handoff_proxies',{});proxy=proxies.get(handoff_id)
                if proxy:
                    await proxy.close()
                    if proxies.get(handoff_id) is proxy:proxies.pop(handoff_id,None)
                getattr(store,'custody_slots',{}).pop(handoff_id,None)
                return await view(store,owner,room_id,profile_id)
            fail('changed')
        if not journal and (r['state']!='prepared' or m['revision']!=r['profile_revision']):fail('changed')
        if not recover and epoch!=r['owner_epoch']:fail('unavailable')
        slot=getattr(store,'custody_slots',{}).get(handoff_id)
        if slot:
            slot['cancelled']=True;slot['state']='unknown'
            try:await slot['proxy'].abort_connections(keep_paused=True)
            except Exception:fail('close_unconfirmed')
        hashes=None
        if recover:
            hashes=_recovery_hashes(store,profile_id)
            if journal:_reconcile_transition(store,m,r,journal,commit=False)
            revoke=(await _custody_revoke(epochs,slot,handoff_id)) if slot else await core_status(store,owner,r,'revoke')
            if revoke.get('status')=='closed' and (revoke.get('profile_id'),revoke.get('profile_revision'))!=(profile_id,r['profile_revision']):fail('close_unconfirmed')
            if revoke['status'] not in ('closed','unavailable'):fail('close_unconfirmed')
            if (epochs.current(owner)!=epoch or store.read(profile_id,owner,room_id)!=m
                    or record(store,profile_id)!=r or _recovery_hashes(store,profile_id)!=hashes):fail('changed')
        else:
            proof=await core_status(store,owner,r)
            if proof['status']!='closed':fail('close_unconfirmed')
        fd=store.claim(m,launch=False,handoff_recovery=True)
        try:
            await asyncio.to_thread(_no_profile_references,store,profile_id)
            if recover:
                if _recovery_hashes(store,profile_id)!=hashes:fail('changed')
                store.release(profile_id,fd);fd=None
                try:
                    proof=await asyncio.to_thread(epochs.vault_profile_recover,owner,handoff_id,profile_id,*hashes)
                except Exception:fail('close_unconfirmed')
                fd=store.claim(m,launch=False,handoff_recovery=True)
                await asyncio.to_thread(_no_profile_references,store,profile_id)
                if (_recovery_hashes(store,profile_id)!=hashes or not isinstance(proof,dict)
                        or proof.get('status')!='recovered' or proof.get('handoff_id')!=handoff_id
                        or proof.get('profile_id')!=profile_id or proof.get('profile_revision')!=r['profile_revision']
                        or proof.get('fill_status')!='unknown'):fail('close_unconfirmed')
            if epochs.current(owner)!=epoch:fail('unavailable')
            latest=store.read(profile_id,owner,room_id)
            if latest!=m or record(store,profile_id)!=r:fail('changed')
            if journal:
                r=_reconcile_transition(store,latest,r,journal)
                if r['state'] in ('returned','recovered'):
                    if not r['prior_outcome_unknown']:
                        r=dict(r,prior_outcome_unknown=True);write_record(store,r)
                    _remove_attempt_marker(store,profile_id,handoff_id)
                    _remove_custody_receipt(store,r,fd)
                    return await view(store,owner,room_id,profile_id)
                if r['state']!='prepared' or latest['revision']!=r['profile_revision']:fail('changed')
            with store.browser.sessions() as db:
                action=db.get(ToolAction,r['action_id'])
                if not action or action.room_id!=r['room_id'] or action.agent_id!=r['agent_id'] or action.review_id!=r['review_id'] or action.state not in ('completed','failed','denied','cancelled','uncertain'):fail('audit_unavailable')
                append_event(db,r['room_id'],ActorRef('system',None),'activity',{'category':'vault_profile_handoff','action_id':r['action_id'],'handoff_id':handoff_id,'status':'recovery_authorized' if recover else 'return_authorized','prior_outcome_unknown':bool(recover or r['prior_outcome_unknown'])},f"vault-handoff:{handoff_id}:{'recover' if recover else 'return'}",allow_inactive=True)
            new_metadata=dict(latest);new_metadata['revision']+=1
            new_record=dict(r);new_record['state']='recovered' if recover else 'returned';new_record['prior_outcome_unknown']=bool(recover or r['prior_outcome_unknown'] or proof.get('fill_status')=='unknown')
            _begin_transition(store,r,new_record,latest,new_metadata,'release')
            latest=new_metadata;store._save(latest)
            mp=store.directory(profile_id)/'metadata.json';mfd=os.open(mp,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
            try:os.fsync(mfd)
            finally:os.close(mfd)
            dfd=os.open(store.directory(profile_id),os.O_RDONLY)
            try:os.fsync(dfd)
            finally:os.close(dfd)
            if _metadata_hash(store,profile_id)!=_planned_metadata_hash(new_metadata):fail('changed')
            r=new_record;write_record(store,r);_transition(store,profile_id,None)
            _remove_attempt_marker(store,profile_id,handoff_id)
            _remove_custody_receipt(store,r,fd)
            proxies=getattr(store,'handoff_proxies',{});proxy=proxies.get(handoff_id)
            if proxy:
                await proxy.close()
                if proxies.get(handoff_id) is proxy:proxies.pop(handoff_id,None)
            getattr(store,'custody_slots',{}).pop(handoff_id,None)
        finally:
            if fd is not None:store.release(profile_id,fd)
        return await view(store,owner,room_id,profile_id)
