"""Owner-selected room browser data. Persistence never grants execution permission."""
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import time
from uuid import uuid4, UUID

from fastapi import HTTPException


def fail(code, status=409):
    raise HTTPException(status, {'code': 'browser_profile_' + code})


def utc():return datetime.now(timezone.utc).isoformat()


def assigned_agents(metadata, room_id):
    if metadata.get('room_id') == room_id:
        return metadata.get('agent_ids', [])
    shared = metadata.get('shared_rooms', {})
    return shared.get(str(room_id), []) if isinstance(shared, dict) else []


def has_room(metadata, room_id):
    shared = metadata.get('shared_rooms', {})
    return metadata.get('room_id') == room_id or isinstance(shared, dict) and str(room_id) in shared


class ProfileAuthority:
    def __init__(self, base, metadata, store=None, room_id=None, agent_id=None):
        self.base=base;self.metadata=metadata;self.store=store;self.room_id=room_id;self.agent_id=agent_id
    def current(self):
        if self.store is None:return True
        try:
            m=self.store.read(self.metadata['id'],self.metadata['owner_id'],self.room_id)
            from app.services.vault_profile_handoff import hold
            return not hold(self.store,m['id']) and m['revision']==self.metadata['revision'] and self.agent_id in assigned_agents(m,self.room_id)
        except Exception:return False
    def add(self,*args):return self.base.add(*args)
    def revoke(self,*args):return self.base.revoke(*args)
    def active(self):return self.current() and self.base.active()
    def broad_active(self):return False
    def allows(self,target):return self.current() and list(target) in self.metadata['origins'] and self.base.allows(target)
    def _live(self,grant):return self.current() and self.base._live(grant)
    @property
    def entries(self):return self.base.entries


class UserAuthority:
    def __init__(self, store, metadata, expected_epoch=None, room_id=None):
        self.store=store;self.metadata=metadata;self.room_id=metadata['room_id'] if room_id is None else room_id;self.deadline=time.monotonic()+1800
        if store.browser.owner_epochs is None:fail('authority_unavailable')
        self.epoch=expected_epoch
        if not self.epoch or store.browser.owner_epochs.current(metadata['owner_id'])!=self.epoch:fail('authority_unavailable')
    def active(self):
        from app.models.models import Conversation,Project,User
        b=self.store.browser;m=self.metadata
        try:
            if time.monotonic()>=self.deadline or b.owner_epochs.current(m['owner_id'])!=self.epoch:return False
            current=self.store.read(m['id'],m['owner_id'],self.room_id)
            if current['revision']!=m['revision']:return False
            with b.sessions() as db:
                room=db.get(Conversation,self.room_id);user=db.get(User,m['owner_id'])
                project=db.get(Project,room.project_id) if room else None
                return bool(user and user.is_active and room and room.user_id==user.id and room.is_active
                            and project and project.user_id==user.id and project.is_active)
        except Exception:return False
    def broad_active(self):return False
    def allows(self,target):return self.active() and list(target) in self.metadata['origins']
    def revoke(self,*args):pass
    def _live(self,*args):return False


class BrowserProfiles:
    def __init__(self,browser,app_data=None):
        self.browser=browser;self.root=Path(app_data)/'browser-profiles' if app_data and os.name=='posix' else None
        self.claims={};self.operations=asyncio.Lock();self.transitions=set()

    def _root(self,create=False):
        if self.root is None:fail('unavailable')
        parent=self.root.parent
        for p in [parent,self.root]:
            if not p.exists():
                if p==self.root and create:p.mkdir(mode=0o700)
                else:fail('unavailable')
            st=p.lstat()
            if p.is_symlink() or not stat.S_ISDIR(st.st_mode) or st.st_uid!=os.getuid() or stat.S_IMODE(st.st_mode)&0o077:fail('unsafe_storage')
        return self.root

    def directory(self,profile_id):
        try:
            if UUID(profile_id).hex!=profile_id:raise ValueError
        except (ValueError,TypeError,AttributeError):fail('not_found',404)
        root=self._root();p=root/profile_id
        if p.is_symlink() or not p.is_dir() or p.stat().st_uid!=os.getuid() or p.stat().st_mode&0o077:fail('not_found',404)
        return p

    def read(self,profile_id,owner_id=None,room_id=None):
        p=self.directory(profile_id)/'metadata.json';st=p.lstat()
        if p.is_symlink() or not stat.S_ISREG(st.st_mode) or st.st_uid!=os.getuid() or st.st_nlink!=1 or st.st_size>16384 or stat.S_IMODE(st.st_mode)!=0o600:fail('unsafe_storage')
        m=json.loads(p.read_text())
        if m.get('id')!=profile_id or owner_id is not None and m.get('owner_id')!=owner_id or room_id is not None and not has_room(m,room_id):fail('not_found',404)
        return m

    def _save(self,m):
        p=self.directory(m['id']);q=p/('.metadata-'+uuid4().hex)
        fd=os.open(q,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        try:
            with os.fdopen(fd,'w') as out:json.dump(m,out,ensure_ascii=False)
            os.replace(q,p/'metadata.json')
        finally:q.unlink(missing_ok=True)

    def list(self,owner_id,room_id):
        if self.root is None or not self.root.exists():return []
        root=self._root();items=[]
        for p in root.iterdir():
            if len(p.name)!=32:continue
            if not (p/'metadata.json').exists():continue
            m=self.read(p.name)
            if m['owner_id']==owner_id and has_room(m,room_id):items.append(m)
        return sorted(items,key=lambda m:m['created_at'])

    def binding(self,room_id,agent_id):
        if self.browser.sessions is None:return None
        from app.models.models import Conversation
        with self.browser.sessions() as db:
            room=db.get(Conversation,room_id)
            if not room:return None
            owner_id=room.user_id
        return next((m for m in self.list(owner_id,room_id) if agent_id in assigned_agents(m,room_id)),None)

    def selected(self,action,execution_binding=None):
        from app.services.resource_binding import freeze_binding
        from app.services.action_review import ReviewChanged
        try:
            if any(key in (action.arguments or {}) for key in ('profile','profile_id','profile_revision')):
                raise ReviewChanged('Explicit browser profile selection is not permitted')
            proof=freeze_binding(execution_binding if execution_binding is not None else getattr(action,'execution_binding',None))
            if proof is not None and (proof.execution_room_id,proof.agent_id)!=(action.room_id,action.agent_id):
                raise ReviewChanged('Browser execution recipient changed')
            m=self.binding(action.room_id,action.agent_id)
            if m:
                from app.services.vault_profile_handoff import hold,record
                if hold(self,m['id']):
                    r=record(self,m['id'])
                    slot=getattr(self,'custody_slots',{}).get(r['handoff_id']) if r else None
                    family=getattr(action,'family',None)
                    allowed=(family in ('browser_navigate','browser_inspect','browser_screenshot') or
                        family=='browser_interact' and (action.arguments or {}).get('operation')=='click')
                    if not allowed or not slot or slot.get('state')!='sealed':
                        raise ReviewChanged('Profile is held for Owner handoff')
            if proof is not None and m and proof.as_dict()['owner_id']!=m['owner_id']:
                raise ReviewChanged('Browser profile Owner changed')
            if proof is not None and proof.as_dict()['context_mode']=='delegated':
                return m if m and m.get('allow_delegated') is True else None
            return m
        except ReviewChanged:raise
        except (ValueError,TypeError,AttributeError) as error:
            raise ReviewChanged('Browser profile selection unavailable') from error

    def claim(self,m,launch=True,*,handoff_recovery=False):
        from app.services.vault_profile_handoff import hold
        if not handoff_recovery and hold(self,m['id']):fail('busy')
        if m['id'] in self.claims or (launch and len(self.claims)>=2):fail('busy')
        if launch and shutil.disk_usage(self.directory(m['id'])).free < int((15+1.8*1.2+.5)*1024**3):fail('disk_space')
        data=self.directory(m['id'])/'data'
        if data.is_symlink() or not data.is_dir():fail('unsafe_storage')
        if launch and sum(q.lstat().st_size for q in data.rglob('*') if stat.S_ISREG(q.lstat().st_mode))>512*1024**2:fail('profile_size')
        p=self.directory(m['id'])/'lease.lock';fd=os.open(p,os.O_CREAT|os.O_RDWR|getattr(os,'O_NOFOLLOW',0),0o600)
        try:
            st=os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_uid!=os.getuid() or st.st_nlink!=1:fail('unsafe_storage')
            if st.st_size==0:os.write(fd,b'0')
            os.lseek(fd,0,0)
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(fd,msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            if not handoff_recovery and hold(self,m['id']):fail('busy')
            self.claims[m['id']]=fd;return fd
        except OSError:os.close(fd);fail('busy')
        except BaseException:os.close(fd);raise

    def release(self,profile_id,fd):
        if self.claims.get(profile_id)==fd:self.claims.pop(profile_id,None);os.close(fd)

    def session(self,profile_id):
        return next(((key,s) for key,s in self.browser.workflows.items() if s.profile_id==profile_id),None)

    def status(self,m,room_id=None):
        room_id=m['room_id'] if room_id is None else room_id
        from app.services.vault_profile_handoff import hold
        held=hold(self,m['id'])
        pair=self.session(m['id']);s=pair[1] if pair else None
        parked=self.browser.parked_valid(m)
        return {k:m[k] for k in ('id','name','account_label','origin_urls','revision','created_at','last_used_at')}|{
            'agent_ids':list(assigned_agents(m,room_id)), 'home_room_id':m['room_id'], 'allow_delegated':m.get('allow_delegated',False) is True,
            'shared_room_ids':sorted(int(x) for x in m.get('shared_rooms',{})),
            'controller':'settling' if held else (('settling' if s.closing else s.controller) if s else ('parked' if parked else ('settling' if m['id'] in self.claims else 'disconnected'))),
            'agent_id':s.agent_id if s and s.controller=='agent' else None,
            'connected':bool(s and not s.closing and not s.page.is_closed()),'expires_in_seconds':max(0,int(s.deadline-time.monotonic())) if s else 0}

    def validate(self,db,owner_id,room_id,agent_ids,origin_urls):
        from app.services.room_service import require_room
        from app.models.collaboration import RoomMembership
        from app.models.models import Agent
        from app.services.tools.browser import origin
        require_room(db,room_id,owner_id)
        for aid in agent_ids:
            member=db.get(RoomMembership,(room_id,aid));agent=db.get(Agent,aid)
            if not member or not member.is_active or not agent or not agent.is_active or agent.user_id!=owner_id:fail('invalid_agent',422)
        origins=[];urls=[]
        import ipaddress
        for url in origin_urls:
            try:
                value=origin(url)
                # This feature is for public sites. Do not use profiles to reach local services.
                if value[1]=='localhost' or value[1].endswith('.local'):raise ValueError
                try:
                    if not ipaddress.ip_address(value[1]).is_global:raise ValueError
                except ValueError:
                    if ':' in value[1] or value[1].replace('.','').isdigit():raise
                normalized=f'{value[0]}://{value[1] if ":" not in value[1] else "["+value[1]+"]"}'+('' if (value[0],value[2]) in [('https',443),('http',80)] else ':'+str(value[2]))
            except (ValueError,TypeError):fail('invalid_origin',422)
            if list(value) not in origins:origins.append(list(value));urls.append(normalized)
        if not origins:fail('invalid_origin',422)
        return origins,urls

    def put(self,db,owner_id,room_id,value,profile_id=None):
        if not value.name.strip():fail('invalid_name',422)
        origins,urls=self.validate(db,owner_id,room_id,value.agent_ids,value.origins)
        others=self.list(owner_id,room_id)
        if any(m['name'].casefold()==value.name.strip().casefold() for m in others if m['id']!=profile_id):fail('name_exists')
        if any(set(assigned_agents(m,room_id))&set(value.agent_ids) for m in others if m['id']!=profile_id):fail('agent_already_bound')
        if profile_id:
            m=self.read(profile_id,owner_id,room_id)
            if m['room_id']!=room_id:fail('home_room_required',403)
            if value.revision!=m['revision']:fail('changed')
            fd=self.claim(m,launch=False)
            try:
                m=self.read(profile_id,owner_id,room_id)
                if m['room_id']!=room_id or value.revision!=m['revision']:fail('changed')
            except BaseException:
                self.release(profile_id,fd);raise
        else:
            if len(others)>=8:fail('limit')
            root=self._root(create=True);profile_id=uuid4().hex;p=root/profile_id;p.mkdir(mode=0o700);(p/'data').mkdir(mode=0o700)
            m={'id':profile_id,'owner_id':owner_id,'room_id':room_id,'created_at':utc(),'last_used_at':None,'revision':0};fd=None
        try:
            if profile_id and m.get('shared_rooms'):
                for shared_id in m['shared_rooms']:
                    for other in self.list(owner_id,int(shared_id)):
                        if other['id']!=profile_id and other['name'].casefold()==value.name.strip().casefold():fail('name_exists')
            m.update(name=value.name.strip(),account_label=value.account_label.strip(),agent_ids=sorted(set(value.agent_ids)),origins=origins,origin_urls=urls,revision=m['revision']+1)
            self._save(m);return self.status(m)
        finally:
            if fd is not None:self.release(profile_id,fd)

    def share(self,db,owner_id,home_room_id,profile_id,revision,bindings,allow_delegated=False):
        if type(allow_delegated) is not bool:fail('invalid_share',422)
        if not isinstance(bindings,list) or len(bindings)>8:fail('share_limit',422)
        from app.services.room_service import require_room
        require_room(db,home_room_id,owner_id)
        m=self.read(profile_id,owner_id,home_room_id)
        if m['room_id']!=home_room_id:fail('home_room_required',403)
        if m['revision']!=revision:fail('changed')
        fd=self.claim(m,launch=False)
        try:
            m=self.read(profile_id,owner_id,home_room_id)
            if m['room_id']!=home_room_id or m['revision']!=revision:fail('changed')
            desired={}
            for binding in bindings:
                if not isinstance(binding,dict) or set(binding)!={'room_id','agent_ids'}:fail('invalid_share',422)
                room=binding['room_id'];agents=binding['agent_ids']
                if type(room) is not int or room<=0 or room==home_room_id or str(room) in desired:fail('invalid_share',422)
                if not isinstance(agents,list) or len(agents)>16 or any(type(a) is not int or a<=0 for a in agents) or len(set(agents))!=len(agents):fail('invalid_share',422)
                self.validate(db,owner_id,room,agents,m['origin_urls'])
                others=[x for x in self.list(owner_id,room) if x['id']!=profile_id]
                if len(others)>=8:fail('limit')
                if any(x['name'].casefold()==m['name'].casefold() for x in others):fail('name_exists')
                if any(set(assigned_agents(x,room)) & set(agents) for x in others):fail('agent_already_bound')
                desired[str(room)]=sorted(agents)
            m['shared_rooms']=desired;m['allow_delegated']=allow_delegated;m['revision']+=1;self._save(m)
            return self.status(m,home_room_id)
        finally:self.release(profile_id,fd)

    async def close_profile(self,m):
        from app.services.vault_profile_handoff import hold
        if hold(self,m['id']):fail('busy')
        pair=self.session(m['id'])
        if pair:
            key,s=pair
            async with s.lock:await self.browser._close(key,s)
        else:
            host=self.browser.parked.get(m['id'])
            if host:
                if host['revision']!=m['revision']:fail('changed')
                if not await self.browser.close_parked(m['id'],expected_host=host):fail('changed')

    async def return_profile(self,m):
        from app.services.vault_profile_handoff import hold
        if hold(self,m['id']):fail('busy')
        pair=self.session(m['id'])
        if pair:
            key,s=pair
            await self.browser._park(key,s)
        elif not self.browser.parked_valid(m):
            fail('disconnected')

    async def owner_open(self,m,expected_epoch=None,room_id=None):
        from app.services.vault_profile_handoff import hold
        if hold(self,m['id']):fail('busy')
        room_id=m['room_id'] if room_id is None else room_id
        self.transitions.add(m['id'])
        try:
            if not expected_epoch or self.browser.owner_epochs is None or self.browser.owner_epochs.current(m['owner_id'])!=expected_epoch:fail('authority_unavailable')
            from types import SimpleNamespace
            pair=self.session(m['id'])
            if pair and pair[1].room_id==room_id and pair[1].controller=='user' and pair[1].authority.active() and not pair[1].page.is_closed():return self.status(m,room_id)
            if pair:await self.return_profile(m)
            m=self.read(m['id'],m['owner_id'],room_id)
            # Owner control is independent of Agent grants and always closes their prior context.
            action=SimpleNamespace(room_id=room_id,agent_id=0,workflow_id='owner-'+m['id'],action_id='owner-'+m['id'])
            key=(action.room_id,0,action.workflow_id)
            s=await self.browser._create(action,None,tuple(m['origins'][0]),manual=m,manual_epoch=expected_epoch)
            self.browser.workflows[key]=s
            if self.browser._watcher is None:self.browser._watcher=asyncio.create_task(self.browser._watch())
            try:await s.page.goto(m['origin_urls'][0],wait_until='domcontentloaded')
            except BaseException:
                await self.browser._close(key,s);raise
            return self.status(m,room_id)
        finally:self.transitions.discard(m['id'])

    async def delete(self,m,reset=False,expected_epoch=None,room_id=None):
        from app.services.vault_profile_handoff import hold
        if hold(self,m['id']):fail('busy')
        if room_id is not None and room_id!=m['room_id']:fail('home_room_required',403)
        had_session=self.session(m['id']) is not None or m['id'] in self.browser.parked
        await self.close_profile(m)
        if not expected_epoch or not self.browser.owner_epochs or self.browser.owner_epochs.current(m['owner_id'])!=expected_epoch:fail('authority_unavailable')
        fresh=self.read(m['id'],m['owner_id'],room_id)
        if (fresh['revision']!=m['revision']+(1 if had_session else 0)
                or any(fresh.get(k)!=m.get(k) for k in set(m)|set(fresh) if k not in ('revision','last_used_at'))):fail('changed')
        m=fresh;fd=self.claim(m,launch=False)
        try:
            p=self.directory(m['id']);data=p/'data'
            # Unknown external browser ownership is preserved, including an orphan after a crash.
            binary=shutil.which('lsof') or ('/usr/sbin/lsof' if Path('/usr/sbin/lsof').exists() else None)
            if not binary:fail('close_unconfirmed')
            refs=subprocess.run([binary,'+D',str(data)],capture_output=True)
            if refs.returncode!=1 or refs.stdout or refs.stderr:fail('close_unconfirmed')
            if any(q.lstat().st_uid!=os.getuid() for q in [data,*data.rglob('*')]):fail('unsafe_storage')
            from app.services.vault_profile_handoff import retire_profile_handoff
            retire_profile_handoff(self,m,fd,expected_epoch,retire=not reset)
            if self.browser.owner_epochs.current(m['owner_id'])!=expected_epoch:fail('authority_unavailable')
            shutil.rmtree(data)
            if reset:
                data.mkdir(mode=0o700)
                fresh=self.read(m['id']);fresh['revision']+=1;self._save(fresh)
            else:(p/'metadata.json').unlink()
        finally:self.release(m['id'],fd)
        if not reset:
            (p/'lease.lock').unlink(missing_ok=True);p.rmdir()
        return {'reset':reset,'server_logout_verified':False}

    def user_in_control(self):
        if any(s.profile_id and s.controller=='user' for s in self.browser.workflows.values()):return True
        if self.root is None or not self.root.exists():return False
        try:
            from app.services.vault_profile_handoff import hold
            return any(hold(self,p.name) for p in self.root.iterdir() if p.is_dir() and not p.is_symlink() and len(p.name)==32)
        except Exception:return True

    async def wait_for_agent(self,action,cancel):
        from app.services.action_review import ReviewChanged
        metadata=self.selected(action)
        identity=(metadata['id'],metadata['revision']) if metadata else None
        while not cancel.is_set():
            current=self.selected(action)
            if ((current['id'],current['revision']) if current else None)!=identity:
                raise ReviewChanged('Selected profile changed while waiting; review again')
            pair=self.session(metadata['id']) if metadata else None
            key=(action.room_id,action.agent_id,action.workflow_id or action.action_id)
            if metadata is None:return
            if metadata['id'] not in self.transitions and (metadata['id'] not in self.claims or self.browser.parked_valid(metadata) or pair and pair[0]==key and not pair[1].closing):return
            await asyncio.sleep(.2)
