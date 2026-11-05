"""Owner-reviewed public template commands and crash-safe remote reconciliation."""
import hashlib
import hmac
import http.client
import json
import re
import secrets
import socket
import ssl
import threading
import time
from urllib.parse import urlsplit
from uuid import UUID
from fastapi import HTTPException
from sqlalchemy import text
from app.services import bot_template_links, bot_templates
from app.services.secrets import SecretStoreUnavailable

_LOCK=threading.RLock()
OPEN=('prepared','unknown','not_found')

def fail(code,status=409): raise HTTPException(status,{'code':'template_publication_'+code}) from None

def origin_checked(origin):
    try:
        suffix='/t/00000000-0000-4000-8000-000000000000'
        _,source,_=bot_template_links._url(origin+suffix)
        normalized=source[:-len(suffix)]
        if origin!=normalized: raise ValueError
        return origin
    except (HTTPException,ValueError,TypeError): fail('origin_invalid',422)

def key_checked(key):
    if not isinstance(key,str) or not 1<=len(key)<=200 or not key.isascii() or not key.isprintable():fail('key_invalid',422)
    return key

def token_checked(token):
    if not isinstance(token,str) or not 32<=len(token)<=1017 or not token.isascii() or not token.isprintable() or ' ' in token:fail('credential_invalid',422)
    return token

def _digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf-8')).hexdigest()

def _network(origin,token,method,path,payload=None,key=None):
    """No proxy, redirects or sensitive diagnostics; resolve all IPs before pinning."""
    host=urlsplit(origin).hostname;deadline=time.monotonic()+15
    try: addresses=bot_template_links._resolve(host,deadline)
    except HTTPException: fail('remote_unavailable',503)
    family,kind,proto,address=addresses[0]
    class Pinned(http.client.HTTPSConnection):
        def connect(self):
            raw=socket.socket(family,kind,proto);self.sock=raw;self.transport=raw
            raw.settimeout(max(.01,deadline-time.monotonic()));raw.connect(address)
            if deadline<=time.monotonic():fail('remote_unavailable',503)
            wrapped=ssl.create_default_context().wrap_socket(raw,server_hostname=host,do_handshake_on_connect=False)
            self.sock=wrapped;self.transport=wrapped
            remaining=deadline-time.monotonic()
            if remaining<=0:fail('remote_unavailable',503)
            wrapped.settimeout(remaining);wrapped.do_handshake()
    connection=Pinned(host,443,timeout=15)
    def expire():
        sock=getattr(connection,'transport',None)
        if sock:
            try:sock.shutdown(socket.SHUT_RDWR)
            except OSError:pass
            try:sock.close()
            except OSError:pass
    timer=threading.Timer(max(.01,deadline-time.monotonic()),expire);timer.daemon=True;timer.start()
    try:
        headers={'Authorization':'Bearer '+token,'Accept':'application/json','Accept-Encoding':'identity','Connection':'close'}
        if key:headers['Idempotency-Key']=key
        data=None if payload is None else json.dumps(payload,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf-8')
        if data is not None:headers['Content-Type']='application/json'
        connection.request(method,path,body=data,headers=headers);res=connection.getresponse()
        if time.monotonic()>=deadline:fail('remote_unavailable',503)
        if res.status not in (200,201,404,409):fail('remote_unavailable',503)
        if res.getheader('Content-Encoding','identity').strip().lower() not in ('','identity'):fail('remote_unavailable',503)
        if res.getheader('Content-Type','').split(';',1)[0].strip().lower()!='application/json':fail('remote_unavailable',503)
        length=res.getheader('Content-Length')
        if length is not None and (not length.isascii() or not length.isdecimal() or int(length)>524288):fail('remote_unavailable',503)
        parts=[];size=0
        while True:
            if time.monotonic()>=deadline:fail('remote_unavailable',503)
            part=res.read1(min(32768,524289-size))
            if not part:break
            size+=len(part)
            if size>524288:fail('remote_unavailable',503)
            parts.append(part)
        if length is not None and size!=int(length):fail('remote_unavailable',503)
        obj=json.loads(b''.join(parts).decode('utf-8'),object_pairs_hook=bot_template_links._pairs,parse_constant=bot_template_links._bad_constant)
        if type(obj) is not dict:fail('remote_unavailable',503)
        return res.status,obj
    except HTTPException:raise
    except Exception:fail('remote_unavailable',503)
    finally:timer.cancel();connection.close()

def _config(db,owner):
    return db.execute(text('SELECT registry_id,origin,publisher_id,revision,secret_ref FROM template_publisher_configs WHERE owner_id=:o'),{'o':owner}).mappings().first()

def read_config(db,owner):
    row=_config(db,owner)
    return {'version':1,'configured':bool(row),'revision':row['revision'] if row else 0,
            'origin':row['origin'] if row else None,'publisher_id':row['publisher_id'] if row else None}

def _store(store):
    if store is None:fail('credential_unavailable',503)
    return store

def set_config(db,owner,value,store):
    origin=origin_checked(value.origin);token=token_checked(value.token);store=_store(store)
    with _LOCK:
        _cleanup(db,owner,store)
        old=_config(db,owner);revision=old['revision'] if old else 0
        if revision!=value.expected_revision:fail('config_changed')
        status,identity=_network(origin,token,'GET','/publisher/me')
        publisher=identity.get('publisher_id')
        if status!=200 or identity.get('version')!=1 or not isinstance(publisher,str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}',publisher):fail('credential_invalid',422)
        prior=db.execute(text('''SELECT registry_id FROM template_publication_commands WHERE owner_id=:o AND origin=:u AND publisher_id=:p
            UNION SELECT registry_id FROM template_publications WHERE owner_id=:o AND origin=:u AND publisher_id=:p LIMIT 1'''),
            {'o':owner,'u':origin,'p':publisher}).first()
        registry=(old['registry_id'] if old and old['origin']==origin and old['publisher_id']==publisher
            else prior.registry_id if prior else secrets.randbits(62)+1)
        ref=f'template-publisher:{owner}:{registry}:{secrets.token_hex(16)}'
        db.rollback();db.execute(text('INSERT INTO template_publisher_secret_cleanup(owner_id,secret_ref) VALUES(:o,:s)'),{'o':owner,'s':ref});db.commit()
        try:store.put(ref,token)
        except SecretStoreUnavailable:
            _cleanup(db,owner,store)
            fail('credential_unavailable',503)
        committed=False;commit_attempted=False
        try:
            db.rollback();db.execute(text('BEGIN IMMEDIATE'))
            current=_config(db,owner)
            if (current['revision'] if current else 0)!=revision:fail('config_changed')
            db.execute(text('INSERT INTO template_publisher_configs(owner_id,registry_id,origin,publisher_id,revision,secret_ref) VALUES(:o,:r,:u,:p,:v,:s) ON CONFLICT(owner_id) DO UPDATE SET registry_id=:r,origin=:u,publisher_id=:p,revision=:v,secret_ref=:s'),
                {'o':owner,'r':registry,'u':origin,'p':publisher,'v':revision+1,'s':ref})
            db.execute(text('DELETE FROM template_publisher_secret_cleanup WHERE owner_id=:o AND secret_ref=:s'),{'o':owner,'s':ref})
            if current:db.execute(text('INSERT OR IGNORE INTO template_publisher_secret_cleanup(owner_id,secret_ref) VALUES(:o,:s)'),{'o':owner,'s':current['secret_ref']})
            commit_attempted=True;db.commit();committed=True
        finally:
            if not committed:
                db.rollback()
                if not commit_attempted:_cleanup(db,owner,store)
        _cleanup(db,owner,store)
        return read_config(db,owner)

def _cleanup(db,owner,store):
    rows=db.execute(text('SELECT secret_ref FROM template_publisher_secret_cleanup WHERE owner_id=:o'),{'o':owner}).all()
    for (ref,) in rows:
        active=_config(db,owner)
        if active and active['secret_ref']==ref:continue
        try:store.delete(ref)
        except SecretStoreUnavailable:continue
        db.execute(text('DELETE FROM template_publisher_secret_cleanup WHERE owner_id=:o AND secret_ref=:s'),{'o':owner,'s':ref});db.commit()

def _token(db,owner,registry,publisher,origin,store):
    row=_config(db,owner)
    if not row or row['registry_id']!=registry or row['publisher_id']!=publisher or row['origin']!=origin:fail('identity_changed')
    try: token=_store(store).get(row['secret_ref'])
    except SecretStoreUnavailable:fail('credential_unavailable',503)
    token_checked(token)
    status,info=_network(origin,token,'GET','/publisher/me')
    if status!=200 or info.get('version')!=1 or info.get('publisher_id')!=publisher:fail('identity_changed')
    return token

def _publication(db,owner,bot):
    return db.execute(text('SELECT registry_id,publisher_id,origin,template_id,revision,revoked,snapshot_sha256 FROM template_publications WHERE owner_id=:o AND bot_id=:b'),{'o':owner,'b':bot}).mappings().first()

def _pending(db,owner,bot):
    return db.execute(text("SELECT command_key,state FROM template_publication_commands WHERE owner_id=:o AND bot_id=:b AND state IN ('prepared','unknown','not_found')"),{'o':owner,'b':bot}).mappings().first()

def read_publication(db,owner,bot):
    bot_templates.bot_profile.owned_agent(db,owner,bot)
    row=_publication(db,owner,bot);pending=_pending(db,owner,bot)
    return {'version':1,'bot_id':bot,'publication':({'origin':row['origin'],'publisher_id':row['publisher_id'],
        'template_id':row['template_id'],'revision':row['revision'],'revoked':bool(row['revoked']),
        'snapshot_sha256':row['snapshot_sha256'],'url':row['origin']+'/t/'+row['template_id']} if row else None),
        'pending':dict(pending) if pending else None}

def _plan(db,owner,bot,value):
    config=_config(db,owner)
    if not config:fail('config_required')
    source=bot_templates.export(db,owner,bot)
    pub=_publication(db,owner,bot)
    op=value.operation
    if op=='publish':
        if pub and not pub['revoked']:fail('already_published')
    elif not pub or pub['revoked']:fail('not_published')
    elif (pub['registry_id'],pub['publisher_id'],pub['origin'])!=(config['registry_id'],config['publisher_id'],config['origin']):fail('identity_changed')
    if (op=='revoke')!=(value.template is None):fail('invalid',422)
    draft=bot_templates.checked(value.template.model_dump(mode='json')) if value.template else None
    sha=bot_templates.digest(draft) if draft is not None else None
    scope={'owner_id':owner,'bot_id':bot,'registry_id':config['registry_id'],'origin':config['origin'],
        'publisher_id':config['publisher_id'],'source_sha256':source['template_sha256'],
        'operation':op,'snapshot_sha256':sha,'template_id':pub['template_id'] if op!='publish' else None,
        'expected_revision':pub['revision'] if op!='publish' else None}
    return draft,scope,_digest(scope)

def preview(db,owner,bot,value):
    if _pending(db,owner,bot):fail('pending')
    draft,scope,plan_hash=_plan(db,owner,bot,value)
    return {'version':1,'bot_id':bot,'operation':value.operation,'template':draft,
        'snapshot_sha256':scope['snapshot_sha256'],'source_sha256':scope['source_sha256'],
        'origin':scope['origin'],'publisher_id':scope['publisher_id'],'template_id':scope['template_id'],
        'expected_revision':scope['expected_revision'],'plan_hash':plan_hash}

def _row(db,owner,bot,key):
    return db.execute(text('SELECT * FROM template_publication_commands WHERE owner_id=:o AND bot_id=:b AND command_key=:k'),{'o':owner,'b':bot,'k':key}).mappings().first()

def _status(row):
    frozen=json.loads(row['payload'])
    public_scope={**frozen['scope'],'registry_id':str(frozen['scope']['registry_id'])}
    return {'version':1,'bot_id':row['bot_id'],'command_key':row['command_key'],'state':row['state'],
        'operation':frozen['scope']['operation'],'template':frozen['template'],'plan_hash':frozen['plan_hash'],
        'scope':public_scope,'response':json.loads(row['response']) if row['response'] else None}

def _validate_remote(obj,scope):
    try:
        tid=obj['template_id'];parsed=UUID(tid)
        expected=1 if scope['operation']=='publish' else scope['expected_revision']+1
        return (obj['version']==1 and str(parsed)==tid and parsed.version==4 and type(obj['revision']) is int
            and obj['revision']==expected and type(obj['revoked']) is bool
            and obj['revoked']==(scope['operation']=='revoke') and obj['snapshot_sha256']==scope['snapshot_sha256']
            and (scope['template_id'] is None or scope['template_id']==tid))
    except (KeyError,ValueError,TypeError,AttributeError):return False

def _finalize(db,owner,bot,key,obj,scope):
    if not _validate_remote(obj,scope):fail('remote_unavailable',503)
    response={'template_id':obj['template_id'],'revision':obj['revision'],'revoked':obj['revoked'],
        'snapshot_sha256':obj['snapshot_sha256'],'url':scope['origin']+'/t/'+obj['template_id']}
    db.rollback();db.execute(text('BEGIN IMMEDIATE'))
    try:
        row=_row(db,owner,bot,key)
        if not row:fail('command_not_found',404)
        if row['state']=='completed':db.rollback();return _status(row)
        db.execute(text('''INSERT INTO template_publications(owner_id,bot_id,registry_id,publisher_id,origin,template_id,revision,revoked,snapshot_sha256)
            VALUES(:o,:b,:r,:p,:u,:t,:v,:x,:s) ON CONFLICT(owner_id,bot_id) DO UPDATE SET registry_id=:r,publisher_id=:p,origin=:u,template_id=:t,revision=:v,revoked=:x,snapshot_sha256=:s'''),
            {'o':owner,'b':bot,'r':scope['registry_id'],'p':scope['publisher_id'],'u':scope['origin'],'t':response['template_id'],
             'v':response['revision'],'x':int(response['revoked']),'s':response['snapshot_sha256']})
        db.execute(text("UPDATE template_publication_commands SET state='completed',response=:response WHERE owner_id=:o AND command_key=:k"),
            {'response':json.dumps(response,separators=(',',':')),'o':owner,'k':key})
        db.commit();return _status(_row(db,owner,bot,key))
    except BaseException:db.rollback();raise

def _send(db,owner,bot,key,row,store):
    data=json.loads(row['payload']);scope=data['scope'];token=_token(db,owner,row['registry_id'],row['publisher_id'],row['origin'],store)
    op=scope['operation'];tid=scope['template_id']
    method='POST' if op=='publish' else 'PUT' if op=='update' else 'DELETE'
    path='/publisher/templates'+('' if op=='publish' else '/'+tid)
    payload={'snapshot':data['template']} if op=='publish' else ({'snapshot':data['template'],'expected_revision':scope['expected_revision']} if op=='update' else {'expected_revision':scope['expected_revision']})
    try:
        status,obj=_network(row['origin'],token,method,path,payload,key)
        if ((status==409 and obj.get('code')=='revision_conflict')
                or (status==404 and obj.get('code')=='not_found' and op in ('update','revoke'))):
            db.rollback();db.execute(text("UPDATE template_publication_commands SET state='conflict' WHERE owner_id=:o AND command_key=:k AND state!='completed'"),{'o':owner,'k':key});db.commit()
            return _status(_row(db,owner,bot,key))
        if status not in (200,201):fail('remote_unavailable',503)
        return _finalize(db,owner,bot,key,obj,scope)
    except BaseException:
        db.rollback();db.execute(text("UPDATE template_publication_commands SET state='unknown' WHERE owner_id=:o AND command_key=:k AND state!='completed'"),{'o':owner,'k':key});db.commit();raise

def command(db,owner,bot,value,key,store):
    key_checked(key)
    with _LOCK:
        existing=_row(db,owner,bot,key)
        other=db.execute(text('SELECT bot_id FROM template_publication_commands WHERE owner_id=:o AND command_key=:k'),{'o':owner,'k':key}).first()
        if other and other.bot_id!=bot:fail('idempotency_conflict')
        signature=_digest({'bot_id':bot,'operation':value.operation,'template':value.template.model_dump(mode='json') if value.template else None,'plan_hash':value.plan_hash})
        if existing:
            if not hmac.compare_digest(existing['request_digest'],signature):fail('idempotency_conflict')
            if existing['state'] in ('completed','conflict'):return _status(existing)
            if existing['state']!='not_found':fail('pending')
            # Explicit same-key retry only after owner requested remote readback and received 404.
            db.execute(text("UPDATE template_publication_commands SET state='prepared' WHERE owner_id=:o AND command_key=:k AND state='not_found'"),{'o':owner,'k':key});db.commit()
            return _send(db,owner,bot,key,_row(db,owner,bot,key),store)
        db.rollback();db.execute(text('BEGIN IMMEDIATE'))
        try:
            if _pending(db,owner,bot):fail('pending')
            draft,scope,plan_hash=_plan(db,owner,bot,value)
            if not hmac.compare_digest(plan_hash,value.plan_hash):fail('changed')
            db.execute(text('''INSERT INTO template_publication_commands(owner_id,bot_id,command_key,request_digest,payload,registry_id,publisher_id,origin,state,response)
                VALUES(:o,:b,:k,:d,:payload,:r,:p,:u,'prepared',NULL)'''),
                {'o':owner,'b':bot,'k':key,'d':signature,'payload':json.dumps({'scope':scope,'template':draft,'plan_hash':plan_hash},ensure_ascii=False,separators=(',',':')),
                 'r':scope['registry_id'],'p':scope['publisher_id'],'u':scope['origin']})
            db.commit()
        except BaseException:db.rollback();raise
        return _send(db,owner,bot,key,_row(db,owner,bot,key),store)

def receipt(db,owner,bot,key,store):
    key_checked(key)
    with _LOCK:
        row=_row(db,owner,bot,key)
        if not row:fail('command_not_found',404)
        if row['state'] in ('completed','conflict'):return _status(row)
        token=_token(db,owner,row['registry_id'],row['publisher_id'],row['origin'],store)
        status,obj=_network(row['origin'],token,'GET','/publisher/commands',key=key)
        if status==404:
            if obj.get('code')!='command_not_found':fail('remote_unavailable',503)
            db.execute(text("UPDATE template_publication_commands SET state='not_found' WHERE owner_id=:o AND command_key=:k AND state!='completed'"),{'o':owner,'k':key});db.commit()
            return _status(_row(db,owner,bot,key))
        if status!=200:fail('remote_unavailable',503)
        return _finalize(db,owner,bot,key,obj,json.loads(row['payload'])['scope'])

def remote_publication(db,owner,bot,store):
    with _LOCK:
        bot_templates.bot_profile.owned_agent(db,owner,bot)
        pub=_publication(db,owner,bot)
        if not pub:fail('not_published')
        token=_token(db,owner,pub['registry_id'],pub['publisher_id'],pub['origin'],store)
        status,obj=_network(pub['origin'],token,'GET','/publisher/templates/'+pub['template_id'])
        if status!=200 or obj.get('version')!=1 or obj.get('template_id')!=pub['template_id'] or type(obj.get('revision')) is not int or obj['revision']<1 or type(obj.get('revoked')) is not bool:fail('remote_unavailable',503)
        sha=obj.get('snapshot_sha256')
        if ((obj['revoked'] and sha is not None) or (not obj['revoked'] and (not isinstance(sha,str) or not re.fullmatch('[0-9a-f]{64}',sha)))):fail('remote_unavailable',503)
        return {'version':1,'bot_id':bot,'template_id':pub['template_id'],'revision':obj['revision'],
            'revoked':obj['revoked'],'snapshot_sha256':sha,
            'matches_local':obj['revision']==pub['revision'] and obj['revoked']==bool(pub['revoked']) and sha==pub['snapshot_sha256']}


def adopt_remote(db,owner,bot,value,store):
    """Adopt only the exact metadata the Owner just observed; never rewrite Bot or draft."""
    with _LOCK:
        bot_templates.bot_profile.owned_agent(db,owner,bot)
        pub=_publication(db,owner,bot)
        if not pub:fail('not_published')
        if _pending(db,owner,bot):fail('pending')
        if pub['revision']!=value.expected_local_revision or pub['template_id']!=value.template_id:fail('changed')
        before=dict(pub)
        observed=remote_publication(db,owner,bot,store)
        if (observed['template_id']!=value.template_id or observed['revision']!=value.observed_revision
                or observed['revoked']!=value.observed_revoked
                or observed['snapshot_sha256']!=value.observed_snapshot_sha256):fail('changed')
        db.rollback();db.execute(text('BEGIN IMMEDIATE'))
        try:
            current=_publication(db,owner,bot);config=_config(db,owner)
            if not current or dict(current)!=before or _pending(db,owner,bot):fail('changed')
            if not config or (current['registry_id'],current['publisher_id'],current['origin'])!=(config['registry_id'],config['publisher_id'],config['origin']):fail('identity_changed')
            db.execute(text('UPDATE template_publications SET revision=:v,revoked=:x,snapshot_sha256=:s WHERE owner_id=:o AND bot_id=:b'),
                {'v':value.observed_revision,'x':int(value.observed_revoked),'s':value.observed_snapshot_sha256,'o':owner,'b':bot})
            db.commit();return read_publication(db,owner,bot)
        except BaseException:db.rollback();raise
