"""Owner room goals and opt-in public watches, stored separately from the existing product DB."""
import asyncio
from contextlib import contextmanager
from datetime import datetime,timezone
import hashlib,json,os,sqlite3,stat,time
from pathlib import Path
from uuid import uuid4
from fastapi import HTTPException
from app.services.public_watch import retrieve,WatchFailure
from app.services.events import ActorRef,append_event


def fail(code,status=409):raise HTTPException(status,{'code':'room_plan_'+code})
def stamp(value):return datetime.fromtimestamp(value,timezone.utc).isoformat() if value else None


class RoomPlans:
    def __init__(self,database_path,sessions,hub):
        self.path=Path(database_path).parent/'room-plans.db';self.sessions=sessions;self.hub=hub;self.task=None
        parent=self.path.parent
        if parent.is_symlink() or parent.stat().st_uid!=os.geteuid() or parent.stat().st_mode&0o077:raise RuntimeError('private app data required')
        fd=os.open(self.path,os.O_RDWR|os.O_CREAT|getattr(os,'O_NOFOLLOW',0),0o600)
        try:
            info=os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid() or info.st_nlink!=1 or stat.S_IMODE(info.st_mode)!=0o600:raise RuntimeError('private room plan storage required')
        finally:os.close(fd)
        with self.db() as db:db.executescript('''
CREATE TABLE IF NOT EXISTS goals(owner INTEGER NOT NULL,room INTEGER NOT NULL,revision INTEGER NOT NULL,data TEXT NOT NULL,PRIMARY KEY(owner,room));
CREATE TABLE IF NOT EXISTS watches(id TEXT PRIMARY KEY,owner INTEGER NOT NULL,room INTEGER NOT NULL,revision INTEGER NOT NULL,state TEXT NOT NULL,data TEXT NOT NULL,next REAL NOT NULL,claim TEXT,lease REAL NOT NULL DEFAULT 0,failures INTEGER NOT NULL DEFAULT 0,last_hash TEXT,last_match INTEGER,last_seen REAL,last_error TEXT);
CREATE TABLE IF NOT EXISTS notifications(id TEXT PRIMARY KEY,owner INTEGER NOT NULL,room INTEGER NOT NULL,watch TEXT NOT NULL,payload TEXT NOT NULL,delivered INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS fetch_budget(id INTEGER PRIMARY KEY,at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS watch_due ON watches(state,next);
''')

    @contextmanager
    def db(self):
        db=sqlite3.connect(self.path,timeout=5);db.row_factory=sqlite3.Row
        try:
            db.execute('PRAGMA busy_timeout=5000');yield db;db.commit()
        except BaseException:db.rollback();raise
        finally:db.close()

    def goal(self,owner,room):
        with self.db() as db:r=db.execute('SELECT * FROM goals WHERE owner=? AND room=?',(owner,room)).fetchone()
        return {'revision':r['revision'],**json.loads(r['data'])} if r else {'revision':0,'title':'','criteria':[],'complete':False}

    def save_goal(self,owner,room,value):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE');old=db.execute('SELECT revision FROM goals WHERE owner=? AND room=?',(owner,room)).fetchone()
            revision=old['revision'] if old else 0
            if value.revision!=revision:fail('changed')
            data=value.model_dump(exclude={'revision'});ids=[c['id'] for c in data['criteria']]
            if len(ids)!=len(set(ids)) or not data['title'].strip():fail('invalid_goal',422)
            # Always validate canonical room evidence again, including when marking the whole goal complete.
            from app.models.collaboration import RoomEvent
            with self.sessions() as main:
                for c in data['criteria']:
                    if not c['text'].strip():fail('invalid_criterion',422)
                    evidence=main.query(RoomEvent).filter_by(room_id=room,seq=c['evidence_seq']).first() if c['evidence_seq'] else None
                    if c['done'] and (not evidence or evidence.kind not in ('message','tool_result','summary','activity')):fail('evidence_required',422)
            if data['complete'] and (not data['criteria'] or not all(c['done'] and c['evidence_seq'] for c in data['criteria'])):fail('criteria_incomplete',422)
            db.execute('INSERT INTO goals VALUES(?,?,?,?) ON CONFLICT(owner,room) DO UPDATE SET revision=excluded.revision,data=excluded.data',(owner,room,revision+1,json.dumps(data)))
        return self.goal(owner,room)

    def view(self,r):
        d=json.loads(r['data'])
        return {'id':r['id'],'revision':r['revision'],'state':r['state'],**d,'next_at':stamp(r['next']) if r['state']=='active' else None,'last_at':stamp(r['last_seen']),'last_error':r['last_error'],'failures':r['failures'],'observing':bool(r['claim'] and r['lease']>time.time())}

    def watches(self,owner,room):
        with self.db() as db:return [self.view(r) for r in db.execute('SELECT * FROM watches WHERE owner=? AND room=? ORDER BY rowid',(owner,room))]

    def write_watch(self,owner,room,value,watch_id=None):
        from app.services.public_watch import public_url
        from zoneinfo import ZoneInfo,ZoneInfoNotFoundError
        try:url=public_url(value.url);ZoneInfo(value.timezone)
        except (WatchFailure,ZoneInfoNotFoundError,ValueError):fail('invalid_public_url_or_timezone',422)
        if value.mode!='text_change' and not value.keyword.strip():fail('keyword_required',422)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT * FROM watches WHERE id=? AND owner=? AND room=?',(watch_id,owner,room)).fetchone() if watch_id else None
            if watch_id and not old:fail('not_found',404)
            if old and value.revision!=old['revision']:fail('changed')
            # Re-enabling ended records also counts against the active configuration cap.
            if old and old['state']=='ended' and value.enabled and db.execute("SELECT count(*) FROM watches WHERE owner=? AND room=? AND state!='ended'",(owner,room)).fetchone()[0]>=3:fail('watch_limit',422)
            data=value.model_dump(exclude={'revision','enabled','command_id'});data['url']=url
            if not old:
                prior=db.execute('SELECT * FROM watches WHERE id=?',(value.command_id,)).fetchone()
                if prior:
                    if prior['owner']!=owner or prior['room']!=room or json.loads(prior['data'])!=data:fail('command_changed')
                    return self.view(prior)
            if not old and db.execute("SELECT count(*) FROM watches WHERE owner=? AND room=? AND state!='ended'",(owner,room)).fetchone()[0]>=3:fail('watch_limit',422)
            state='active' if value.enabled else 'paused';next_at=time.time()+value.interval_minutes*60
            if old:
                db.execute('UPDATE watches SET revision=revision+1,state=?,data=?,next=?,claim=NULL,lease=0,failures=0,last_hash=NULL,last_match=NULL,last_seen=NULL,last_error=NULL WHERE id=?',(state,json.dumps(data),next_at,watch_id))
            else:
                watch_id=value.command_id
                db.execute('INSERT INTO watches(id,owner,room,revision,state,data,next) VALUES(?,?,?,1,?,?,?)',(watch_id,owner,room,state,json.dumps(data),next_at))
        return next(r for r in self.watches(owner,room) if r['id']==watch_id)

    def control(self,owner,room,watch_id,value):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE');r=db.execute('SELECT * FROM watches WHERE id=? AND owner=? AND room=?',(watch_id,owner,room)).fetchone()
            if not r:fail('not_found',404)
            if r['revision']!=value.revision:fail('changed')
            if value.action=='resume' and r['state']=='ended':fail('ended')
            state={'pause':'paused','resume':'active','end':'ended'}[value.action];data=json.loads(r['data'])
            db.execute('UPDATE watches SET state=?,revision=revision+1,next=?,claim=NULL,lease=0,failures=0,last_error=NULL WHERE id=?',(state,time.time()+data['interval_minutes']*60,watch_id))
        return next(x for x in self.watches(owner,room) if x['id']==watch_id)

    def live(self,owner,room):
        from app.models.models import Conversation,Project,User
        with self.sessions() as db:
            r=db.get(Conversation,room);u=db.get(User,owner);p=db.get(Project,r.project_id) if r else None
            return bool(u and u.is_active and r and r.user_id==owner and r.is_active and p and p.user_id==owner and p.is_active)

    def claim(self):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE');now=time.time()
            r=db.execute("SELECT * FROM watches WHERE state='active' AND next<=? AND lease<=? ORDER BY next LIMIT 1",(now,now)).fetchone()
            if not r:return None
            db.execute('DELETE FROM fetch_budget WHERE at<?',(now-3600,))
            if db.execute('SELECT count(*) FROM fetch_budget').fetchone()[0]>=120:return None
            db.execute('INSERT INTO fetch_budget(at) VALUES(?)',(now,))
            token=uuid4().hex;interval=json.loads(r['data'])['interval_minutes']*60
            # Consume this time slot before I/O. A crash skips its unknown result instead of replaying the same GET.
            db.execute('UPDATE watches SET claim=?,lease=?,next=? WHERE id=?',(token,now+120,now+interval,r['id']))
            return dict(r)|{'claim':token}

    async def observe(self,r):
        data=json.loads(r['data']);error=None;text=None;retry=None
        if not self.live(r['owner'],r['room']):error='room_unavailable'
        else:
            try:text=await asyncio.to_thread(retrieve,data['url'])
            except WatchFailure as e:error=e.code;retry=e.retry_after
            except Exception:error='request_failed'
        # A pause/edit/end during the request invalidates its result, never re-enabling it.
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE');fresh=db.execute('SELECT * FROM watches WHERE id=? AND claim=? AND revision=? AND state=\'active\'',(r['id'],r['claim'],r['revision'])).fetchone()
            if not fresh:return
            if not self.live(r['owner'],r['room']):error='room_unavailable'
            now=time.time();interval=data['interval_minutes']*60;state='active';failures=0;digest=r['last_hash'];match=r['last_match']
            if error:
                failures=r['failures']+1;state='paused' if failures>=5 or error=='room_unavailable' else 'active';delay=max(interval,300*2**min(failures-1,8),retry or 0)
                if state=='paused':
                    payload={'content':f"공개 페이지 감시 ‘{data['name']}’가 일시정지됐습니다. 이유: {error}. 설정을 확인한 뒤 직접 재개하세요.",'category':'public_watch','watch_id':r['id'],'status':'paused'}
                    db.execute('INSERT OR IGNORE INTO notifications VALUES(?,?,?,?,?,0)',(r['claim'],r['owner'],r['room'],r['id'],json.dumps(payload,ensure_ascii=False)))
            else:
                digest=hashlib.sha256(text.encode()).hexdigest();match=int(data['keyword'].casefold() in text.casefold()) if data['mode']!='text_change' else 0
                changed=r['last_hash'] is not None and (digest!=r['last_hash'] if data['mode']=='text_change' else match!=r['last_match'] and match==(1 if data['mode']=='keyword_appears' else 0))
                if changed:
                    payload={'content':f"공개 페이지 변화: {data['name']}\n기준: {data['mode']}\n{data['url']}",'category':'public_watch','watch_id':r['id'],'status':'changed','observed_at':stamp(now)}
                    db.execute('INSERT OR IGNORE INTO notifications VALUES(?,?,?,?,?,0)',(r['claim'],r['owner'],r['room'],r['id'],json.dumps(payload,ensure_ascii=False)))
                    if data['stop_after_change']:state='ended'
                delay=interval
            db.execute('UPDATE watches SET state=?,next=?,claim=NULL,lease=0,failures=?,last_hash=?,last_match=?,last_seen=?,last_error=? WHERE id=?',(state,now+delay,failures,digest,match,now,error,r['id']))

    def publish(self):
        # Durable outbox + canonical idempotency key survive a crash between the two commits.
        with self.db() as side:rows=side.execute('SELECT * FROM notifications WHERE delivered=0 ORDER BY rowid LIMIT 20').fetchall()
        for r in rows:
            try:
                if not self.live(r['owner'],r['room']):continue
                with self.sessions() as db:append_event(db,r['room'],ActorRef('system',None),'activity',json.loads(r['payload']),'watch:'+r['id'])
                self.hub.publish(r['room'])
                with self.db() as side:side.execute('UPDATE notifications SET delivered=1 WHERE id=?',(r['id'],))
            except Exception:continue

    async def loop(self):
        while True:
            try:
                await asyncio.to_thread(self.publish)
                row=self.claim()
                if row:await self.observe(row)
            except Exception:pass
            await asyncio.sleep(5)

    async def start(self):self.task=asyncio.create_task(self.loop())
    async def close(self):
        if self.task:
            self.task.cancel()
            try:await self.task
            except asyncio.CancelledError:pass
