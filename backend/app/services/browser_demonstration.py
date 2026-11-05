"""Owner-controlled, volatile browser demonstrations; hints never authorize effects."""
import asyncio
import hashlib
import json
import re
import time
from uuid import uuid4
from fastapi import HTTPException
from app.services.tools.browser import origin
from app.services.tools.browser_profiles import assigned_agents
from app.services import owner_skills

MAX_STEPS = 100
MAX_SECONDS = 600
from app.models.browser_demonstration import BrowserDemonstration, BrowserDemonstrationReviewCommand
from sqlalchemy import text

def safe_label(value):
    # This is a conservative filter, not a guarantee that arbitrary page text is non-sensitive.
    if not isinstance(value, str) or not value or len(value) > 128: return None
    value = ' '.join(value.split())
    if (not value or re.search(r'https?://|www\.|@|[?&=/#\\]|\d{4,}|password|passwd|secret|token|credential|비밀번호|인증|계좌', value, re.I)
            or any(not (c.isalnum() or c.isspace() or c in '-_.,:()') for c in value)):
        return None
    return value

def fail(code, status=409):
    raise HTTPException(status, {'code': 'browser_demonstration_' + code})

class Demonstrations:
    def __init__(self, profiles):
        self.profiles = profiles
        self.items = {}
        self.commands = {}
        self.instance_id = uuid4().hex
        self.lock = asyncio.Lock()

    def evict(self):
        # Durable records are retained until the Owner explicitly deletes them.
        for demo_id, item in list(self.items.items()):
            if item['state'] != 'recording': self.items.pop(demo_id, None)

    def from_row(self, row):
        return {'id':row.demo_id, 'owner_id':row.owner_id, 'room_id':row.room_id, 'agent_id':row.agent_id,
                'profile_id':row.profile_id, 'profile_revision':row.profile_revision, 'state':row.state,
                'goal':row.goal, 'steps':list(row.steps), 'include_target_labels':row.include_target_labels,
                'review':dict(row.review), 'review_revision':row.review_revision,
                'command_key':row.command_key, 'deadline':0, 'session':None}

    def command(self, owner_id, room_id, key):
        owner_skills.validate_key(key)
        with self.profiles.browser.sessions() as db:
            row=db.query(BrowserDemonstration).filter_by(owner_id=owner_id, room_id=room_id, command_key=key).first()
            if not row: fail('command_not_found',404)
            demo_id=row.demo_id
        return self.view(self.owned(demo_id,owner_id,room_id))

    def owned(self, demo_id, owner_id, room_id):
        item = self.items.get(demo_id)
        if item and (item['owner_id'],item['room_id'])==(owner_id,room_id): return item
        with self.profiles.browser.sessions() as db:
            row=db.get(BrowserDemonstration,demo_id)
            if not row or (row.owner_id,row.room_id)!=(owner_id,room_id): fail('not_found',404)
            # Backend startup lock prevents another app instance owning this DB.
            # Preserve unknown profile leases rather than interrupting another controller.
            if row.state=='recording' and row.instance_id!=self.instance_id:
                if self.profiles.session(row.profile_id) is not None or row.profile_id in self.profiles.claims:
                    fail('recording_owner_unconfirmed')
                profile_id, old_instance, profile_revision = row.profile_id, row.instance_id, row.profile_revision
                try:
                    metadata=self.profiles.read(profile_id,owner_id,room_id)
                    lease=self.profiles.claim(metadata,launch=False)
                except Exception:
                    fail('recording_owner_unconfirmed')
                try:
                    # The original snapshot may predate the lease; lock and re-read
                    # so an already-finished record and its review are never replaced.
                    db.rollback();db.execute(text('BEGIN IMMEDIATE'))
                    row=db.get(BrowserDemonstration,demo_id,populate_existing=True)
                    if not row or (row.owner_id,row.room_id)!=(owner_id,room_id): fail('not_found',404)
                    if row.state=='recording':
                        if (row.instance_id,row.profile_id,row.profile_revision)!=(old_instance,profile_id,profile_revision):
                            fail('recording_owner_unconfirmed')
                        row.state='interrupted'
                        row.review={'goal':row.goal,'steps':list(row.steps),'rules':'','fields':None}
                    db.commit()
                except BaseException:
                    db.rollback();raise
                finally:
                    self.profiles.release(profile_id,lease)
            return self.from_row(row)

    def persist_recording(self, item, terminal=False):
        with self.profiles.browser.sessions() as db:
            row=db.get(BrowserDemonstration,item['id'])
            if not row or row.instance_id!=self.instance_id or row.state!='recording':
                fail('recording_changed')
            row.steps=list(item['steps'])
            if terminal:
                row.state=item['state']
                row.review={'goal':row.goal,'steps':list(item['steps']),'rules':'','fields':None}
                item['review']=dict(row.review);item['review_revision']=row.review_revision
            db.commit()

    def list(self, owner_id, room_id, after_demo_id=None):
        if after_demo_id is not None and not re.fullmatch(r'[a-f0-9]{32}',after_demo_id):
            fail('invalid_cursor',422)
        with self.profiles.browser.sessions() as db:
            query=db.query(BrowserDemonstration.demo_id).filter_by(owner_id=owner_id,room_id=room_id)
            if after_demo_id is not None: query=query.filter(BrowserDemonstration.demo_id>after_demo_id)
            ids=[x.demo_id for x in query.order_by(BrowserDemonstration.demo_id).limit(201)]
        page=ids[:200];items=[];unavailable=0
        for demo_id in page:
            try: items.append(self.view(self.owned(demo_id,owner_id,room_id)))
            except HTTPException as error:
                if error.status_code==409 and error.detail.get('code')=='browser_demonstration_recording_owner_unconfirmed':
                    unavailable+=1
                else: raise
        return {'items':items,'next_after_demo_id':page[-1] if len(ids)>200 else None,'unavailable_count':unavailable}

    def update_review(self, db, item, value, key):
        owner_skills.validate_key(key)
        material=value.model_dump();digest=hashlib.sha256(json.dumps({'demo_id':item['id'],'value':material},sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
        db.rollback();db.execute(text('BEGIN IMMEDIATE'))
        try:
            command=db.get(BrowserDemonstrationReviewCommand,(item['owner_id'],key))
            if command:
                if command.demo_id!=item['id'] or command.digest!=digest: fail('idempotency_conflict')
                response=command.response;db.commit();return response
            row=db.get(BrowserDemonstration,item['id'])
            if not row or (row.owner_id,row.room_id)!=(item['owner_id'],item['room_id']): fail('not_found',404)
            if row.state not in ('stopped','interrupted','saved'): fail('not_stopped')
            if row.review_revision!=value.expected_revision: fail('review_revision_conflict')
            row.review={'goal':value.goal,'steps':value.steps,'rules':value.rules,'fields':value.fields.model_dump() if value.fields else None}
            row.review_revision+=1;db.flush()
            result=self.view(self.from_row(row))
            db.add(BrowserDemonstrationReviewCommand(owner_id=item['owner_id'],command_key=key,demo_id=item['id'],digest=digest,response=result))
            db.commit()
            if item['id'] in self.items:
                item['review']=dict(row.review);item['review_revision']=row.review_revision
            return result
        except BaseException: db.rollback();raise

    def live(self, item):
        s = item['session']
        try:
            m = self.profiles.read(item['profile_id'], item['owner_id'], item['room_id'])
            pair = self.profiles.session(item['profile_id'])
            from app.models.collaboration import RoomMembership
            from app.models.models import Agent
            with self.profiles.browser.sessions() as db:
                member = db.get(RoomMembership, (item['room_id'], item['agent_id']))
                agent = db.get(Agent, item['agent_id'])
                agent_live = bool(member and member.is_active and agent and agent.is_active and agent.user_id == item['owner_id'])
            return (agent_live and self.profiles.claims.get(item['profile_id']) == item['lease'] and item['state'] == 'recording' and time.monotonic() < item['deadline']
                    and m['revision'] == item['profile_revision'] and item['agent_id'] in assigned_agents(m,item['room_id'])
                    and pair is not None and pair[1] is s and s.profile_lease == item['lease']
                    and s.room_id == item['room_id'] and s.controller == 'user' and not s.closing and not s.page.is_closed()
                    and s.authority.active() and s.authority.allows(origin(s.page.url)))
        except Exception:
            return False

    def interrupt(self, item):
        if item['state'] == 'recording':
            item['state'] = 'interrupted'
            item['finished_at'] = time.monotonic()
            self.persist_recording(item, terminal=True)

    def view(self, item):
        if item['state'] == 'recording' and not self.live(item): self.interrupt(item)
        steps = list(item['steps'])
        draft = None
        if item['state'] in ('stopped', 'saved', 'interrupted'):
            content = ('Goal: ' + item['goal'] + '\n\nObserved hints (verify and edit):\n' +
                       '\n'.join(f'{i}. {step}' for i, step in enumerate(steps, 1)) +
                       '\n\nReview decision rules, failure handling and approval boundaries before use. '
                       'This skill does not grant permission to use tools or execute actions.')
            draft = {'name': item['goal'][:120], 'description': 'Draft from a browser demonstration', 'content': content}
        return {'version':1, 'demo_id':item['id'], 'state':item['state'],
                'expires_in_seconds':max(0,int(item['deadline']-time.monotonic())) if item['state']=='recording' else 0,
                'goal':item['goal'], 'steps':steps, 'draft':draft, 'source_trust':'untrusted_hints',
                'agent_id':item['agent_id'], 'profile_id':item['profile_id'], 'profile_revision':item['profile_revision'],
                'include_target_labels':item.get('include_target_labels',False),
                'review':item.get('review',{'goal':item['goal'],'steps':[], 'rules':'', 'fields':None}),
                'review_revision':item.get('review_revision',1)}

    def add(self, item, kind, label=None):
        if not self.live(item): self.interrupt(item); return
        if len(item['steps']) >= MAX_STEPS: self.interrupt(item); return
        if kind in ('click', 'click_button', 'click_link', 'input', 'navigation'):
            text = {'click':'Click a page control', 'click_button':'Click a button',
                    'click_link':'Click a link', 'input':'Enter a value (not recorded)',
                    'navigation':'Navigate within the approved origin'}[kind]
            clean = safe_label(label) if item.get('include_target_labels') and kind in ('click_button','click_link','input') else None
            item['steps'].append(text + (': ' + clean if clean else ''))
            self.persist_recording(item)

    async def start(self, db, owner_id, room_id, value, key):
        owner_skills.validate_key(key)
        digest = hashlib.sha256(json.dumps({'room_id':room_id, 'value':value.model_dump()}, sort_keys=True, ensure_ascii=False, separators=(',',':')).encode()).hexdigest()
        from app.services.room_service import require_room
        require_room(db, room_id, owner_id)
        owner_skills.agent_owned(db, owner_id, value.agent_id)
        from app.models.collaboration import RoomMembership
        membership = db.get(RoomMembership, (room_id, value.agent_id))
        if not membership or not membership.is_active: fail('agent_not_in_room', 404)
        async with self.lock, self.profiles.operations:
            self.evict()
            with self.profiles.browser.sessions() as saved:
                command=saved.query(BrowserDemonstration).filter_by(owner_id=owner_id,command_key=key).first()
                if command:
                    if command.command_digest!=digest or command.room_id!=room_id: fail('idempotency_conflict')
                    return self.view(self.owned(command.demo_id,owner_id,room_id))
            if len(self.items) >= 128: fail('limit')
            m = self.profiles.read(value.profile_id, owner_id, room_id)
            if m['revision'] != value.profile_revision or value.agent_id not in assigned_agents(m,room_id): fail('profile_changed')
            pair = self.profiles.session(value.profile_id)
            if pair is None: fail('owner_control_required')
            s = pair[1]
            if any(x['state']=='recording' and x['session'] is s for x in self.items.values()): fail('already_recording')
            try: current = origin(s.page.url)
            except Exception: fail('owner_control_required')
            item = {'id':uuid4().hex, 'owner_id':owner_id, 'room_id':room_id, 'agent_id':value.agent_id,
                    'profile_id':value.profile_id, 'profile_revision':value.profile_revision, 'lease':s.profile_lease,
                    'session':s, 'origin':current, 'goal':value.goal.strip(), 'deadline':time.monotonic()+MAX_SECONDS,
                    'state':'recording', 'steps':[], 'command_key':key, 'include_target_labels':value.include_target_labels}
            if not self.live(item): fail('owner_control_required')
            item['review']={'goal':item['goal'],'steps':[],'rules':'','fields':None};item['review_revision']=1
            with self.profiles.browser.sessions() as saved:
                saved.add(BrowserDemonstration(demo_id=item['id'],owner_id=owner_id,room_id=room_id,agent_id=value.agent_id,
                    profile_id=value.profile_id,profile_revision=value.profile_revision,state='recording',goal=item['goal'],steps=[],
                    include_target_labels=value.include_target_labels,review=item['review'],review_revision=1,
                    command_key=key,command_digest=digest,instance_id=self.instance_id))
                saved.commit()
            self.items[item['id']] = item
            name = '__gamja_demonstration_hint'
            async def hint(source, payload):
                current_item = getattr(s, '_demonstration_item', None)
                if not current_item or source.get('frame') is not s.page.main_frame: return
                if isinstance(payload, str): self.add(current_item, payload)
                elif isinstance(payload, dict) and set(payload) <= {'kind','label'}:
                    self.add(current_item, payload.get('kind'), payload.get('label'))
            # Page bindings are untrusted: retain only fixed, valueless hint types.
            script = """(() => { const send = k => { try { window[NAME](k).catch(()=>{}); } catch (_) {} };
              const click = e => { if (!e.isTrusted) return; const t=e.target;
                if (!(t instanceof Element)) return; const a=t.closest('a,button,[role=button]');
                const kind=!a ? 'click' : a.tagName === 'A' ? 'click_link' : 'click_button';
                let label=null;
                if (LABELS && a && !t.closest('input,textarea,select,[contenteditable]') &&
                    !a.closest('input,textarea,select,[contenteditable]') &&
                    !a.querySelector('input,textarea,select,[contenteditable]') && a.getClientRects().length) {
                  label=(a.getAttribute('aria-label') || a.innerText || '').trim();
                  if (label.length > 128) label=null;
                }
                send(label ? {kind,label} : kind); };
              const change = e => { const t=e.target; if(!e.isTrusted || !t ||
                !(t instanceof HTMLInputElement || t instanceof HTMLTextAreaElement || t instanceof HTMLSelectElement) ||
                t.type === 'password' || /password|cc-|one-time-code/i.test(t.autocomplete || '')) return;
                let label=null;
                if (LABELS) {
                  label = t.getAttribute('aria-label') || t.getAttribute('placeholder') || null;
                  if (!label && t.labels) {
                    const candidate=Array.from(t.labels).find(x => !x.querySelector('input,textarea,select,[contenteditable]'));
                    if (candidate) label=candidate.textContent;
                  }
                  if (label) { label=label.trim(); if (label.length > 128) label=null; }
                }
                send(label ? {kind:'input',label} : 'input'); };
              document.addEventListener('click', click, true); document.addEventListener('change', change, true);
              window[NAME + '_cleanup'] = () => { document.removeEventListener('click',click,true); document.removeEventListener('change',change,true); };
            })();""".replace('NAME', repr(name)).replace('LABELS', 'true' if value.include_target_labels else 'false')
            async def attach():
                if self.live(item):
                    try: await s.page.evaluate(script)
                    except Exception: self.interrupt(item)
            def navigation(frame):
                if frame is s.page.main_frame:
                    self.add(item, 'navigation')
                    if item['state'] == 'recording': asyncio.create_task(attach())
            item['navigation'] = navigation
            item['binding_name'] = name
            try:
                async with s.lock:
                    if not self.live(item): fail('owner_control_required')
                    if not getattr(s, '_demonstration_binding_installed', False):
                        await s.page.expose_binding(name, hint)
                        s._demonstration_binding_installed = True
                    s._demonstration_item = item
                    s.page.on('framenavigated', navigation)
                    await s.page.evaluate(script)
                    if not self.live(item): fail('owner_control_required')
            except BaseException:
                self.interrupt(item)
                await self.cleanup(item)
                raise
            async def watch():
                while item['state'] == 'recording':
                    await asyncio.sleep(1)
                    if not self.live(item): self.interrupt(item)
                await self.cleanup(item)
            item['watcher'] = asyncio.create_task(watch())
            return self.view(item)

    async def cleanup(self, item):
        session = item['session']
        page = session.page
        owns_collector = getattr(session, '_demonstration_item', None) is item
        if owns_collector: session._demonstration_item = None
        try:
            if item.get('navigation'): page.remove_listener('framenavigated', item['navigation'])
            if owns_collector and item.get('binding_name') and not page.is_closed():
                await page.evaluate("name => { const f=window[name + '_cleanup']; if(typeof f==='function') f(); delete window[name + '_cleanup']; }", item['binding_name'])
        except Exception:
            pass

    async def stop(self, item):
        async with self.lock:
            if item['state'] != 'recording' or not self.live(item):
                self.interrupt(item)
                await self.cleanup(item)
                fail('not_recording')
            item['state'] = 'stopped'
            self.persist_recording(item, terminal=True)
            item['finished_at'] = time.monotonic()
            item['watcher'].cancel()
            await self.cleanup(item)
            return self.view(item)

    async def save(self, db, item, value, key):
        async with self.lock:
            if item['state'] not in ('stopped','saved','interrupted'): fail('not_stopped')
            source = {'kind':'browser_demonstration', 'demo_id':item['id'], 'room_id':item['room_id'],
                      'agent_id':item['agent_id'], 'profile_id':item['profile_id'],
                      'profile_revision':item['profile_revision'], 'trust':'untrusted_hints'}
            result = owner_skills.create_demonstration(db, item['owner_id'], value, key, source)
            item['state'] = 'saved'
            with self.profiles.browser.sessions() as saved:
                row=saved.get(BrowserDemonstration,item['id']);row.state='saved';saved.commit()
            return result
