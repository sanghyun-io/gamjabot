"""Independent public template registry. No desktop database or credentials."""
import base64
import hashlib
import hmac
import html
import ipaddress
import json
import os
import re
import sqlite3
import stat
import uuid
from contextlib import asynccontextmanager, closing
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import ValidationError
from bot_template_portable import portable_bytes

MAX_REQUEST=512*1024
IDENT=re.compile(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}\Z')
HASH=re.compile(r'[0-9a-f]{64}\Z')
STYLE="""*{box-sizing:border-box}body{margin:0;background:#f5f6f8;color:#20242b;font:16px/1.55 system-ui,sans-serif}main{max-width:780px;margin:auto;padding:32px 20px 64px}h1,h2,h3{line-height:1.2}h1{font-size:2rem}h2{font-size:1.3rem;margin-top:2rem}p,li,pre{overflow-wrap:anywhere}pre{white-space:pre-wrap;font:inherit;margin:.5rem 0}ul{padding:0;list-style:none}li,.notice,details{background:#fff;border:1px solid #d9dde3;border-radius:12px;padding:16px;margin:12px 0}li h3{margin:0 0 8px}.notice{border-left:4px solid #315fbe}.button{display:inline-block;background:#244ea5;color:#fff;padding:12px 18px;border-radius:9px;text-decoration:none;font-weight:650}.muted{color:#515966}@media(prefers-color-scheme:dark){body{background:#15171b;color:#f2f3f5}li,.notice,details{background:#22262c;border-color:#454d59}.muted{color:#c1c7d0}.button{background:#a9c4ff;color:#10213e}}"""
STYLE_HASH=base64.b64encode(hashlib.sha256(STYLE.encode('utf-8')).digest()).decode('ascii')
HEADERS={'Cache-Control':'no-store, max-age=0','X-Content-Type-Options':'nosniff',
         'Content-Security-Policy':f"default-src 'none'; style-src 'sha256-{STYLE_HASH}'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
         'Referrer-Policy':'no-referrer','X-Robots-Tag':'noindex, nofollow'}

class Refusal(Exception):
    def __init__(self,status,code):self.status=status;self.code=code

def fail(status,code):raise Refusal(status,code)
def pairs(items):
    result={}
    for k,v in items:
        if k in result:raise ValueError('duplicate')
        result[k]=v
    return result

def decode(raw):
    return json.loads(raw.decode('utf-8'),object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('invalid')))

def publisher_config(path):
    if not path or not os.path.isabs(path):raise ValueError('publisher configuration required')
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    try:
        st=os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid!=os.geteuid() or stat.S_IMODE(st.st_mode)!=0o600 or st.st_size>262144:raise ValueError('publisher configuration invalid')
        with os.fdopen(fd,'rb',closefd=False) as stream:raw=stream.read(262145)
        data=decode(raw)
        if not isinstance(data,dict) or not data or len(data)>1000:raise ValueError('publisher configuration invalid')
        if any(not isinstance(k,str) or not HASH.fullmatch(k) or not isinstance(v,str) or not IDENT.fullmatch(v) for k,v in data.items()):raise ValueError('publisher configuration invalid')
        return tuple(data.items())
    finally:os.close(fd)

def database_path(path):
    if not path or not os.path.isabs(path):raise ValueError('database configuration required')
    p=Path(path)
    if not p.parent.is_dir() or p.is_symlink():raise ValueError('database configuration invalid')
    fd=os.open(p,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        st=os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid!=os.geteuid() or stat.S_IMODE(st.st_mode)!=0o600:raise ValueError('database permissions invalid')
    finally:os.close(fd)
    return str(p)

def connect(app):
    db=sqlite3.connect(app.state.db_path,timeout=5,isolation_level=None)
    db.execute('PRAGMA foreign_keys=ON');db.execute('PRAGMA busy_timeout=5000')
    return db

def public_origin(value):
    if value is None or value == '':return None
    try:
        parts=urlsplit(value);host=parts.hostname
        if (parts.scheme!='https' or not host or value!=f'https://{host}' or parts.username is not None
                or parts.password is not None or parts.port is not None or parts.path or parts.query or parts.fragment):raise ValueError()
        labels=host.split('.')
        if (len(host)>253 or len(labels)<2 or not re.search(r'[a-z]',labels[-1]) or labels[-1] in {'localhost','local','internal','home','test','invalid','example'}
                or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?',label) for label in labels)):raise ValueError()
        try:ipaddress.ip_address(host)
        except ValueError:return value
        raise ValueError()
    except (ValueError,UnicodeError):raise ValueError('public origin configuration invalid') from None

@asynccontextmanager
async def lifespan(app):
    app.state.public_origin=public_origin(os.environ.get('REGISTRY_PUBLIC_ORIGIN'))
    app.state.publishers=publisher_config(os.environ.get('REGISTRY_PUBLISHERS_FILE'))
    app.state.db_path=database_path(os.environ.get('REGISTRY_DB_PATH'))
    with closing(connect(app)) as db:
        db.executescript('''CREATE TABLE IF NOT EXISTS templates (id TEXT PRIMARY KEY, publisher TEXT NOT NULL, revision INTEGER NOT NULL, revoked INTEGER NOT NULL CHECK(revoked IN (0,1)));
        CREATE TABLE IF NOT EXISTS versions (template_id TEXT NOT NULL REFERENCES templates(id), revision INTEGER NOT NULL, snapshot BLOB NOT NULL, sha256 TEXT NOT NULL, PRIMARY KEY(template_id, revision));
        CREATE TABLE IF NOT EXISTS commands (publisher TEXT NOT NULL, key_hash TEXT NOT NULL, request_hash TEXT NOT NULL, response TEXT NOT NULL, PRIMARY KEY(publisher,key_hash));''')
    yield

app=FastAPI(title='Portable template registry',docs_url=None,redoc_url=None,openapi_url=None,lifespan=lifespan)

@app.middleware('http')
async def headers(request,call_next):
    response=await call_next(request)
    for k,v in HEADERS.items():response.headers[k]=v
    return response

@app.exception_handler(Refusal)
async def refusal(_,exc):return JSONResponse({'code':exc.code},status_code=exc.status,headers=HEADERS)

async def body(request):
    size=0;parts=[]
    async for part in request.stream():
        size+=len(part)
        if size>MAX_REQUEST:fail(413,'request_too_large')
        parts.append(part)
    try:return decode(b''.join(parts))
    except (ValueError,UnicodeError,RecursionError):fail(400,'invalid_json')

def identity(request):
    value=request.headers.get('authorization','')
    if not value.startswith('Bearer ') or len(value)>1024:fail(401,'unauthorized')
    token=value[7:]
    if len(token)<32 or not token.isascii() or not token.isprintable() or ' ' in token:fail(401,'unauthorized')
    digest=hashlib.sha256(token.encode('ascii')).hexdigest();found=None
    for key,publisher in request.app.state.publishers:
        if hmac.compare_digest(key,digest):found=publisher
    if found is None:fail(401,'unauthorized')
    return found

def command_key(request):
    value=request.headers.get('idempotency-key','')
    if not 1<=len(value)<=200 or not value.isascii() or not value.isprintable():fail(400,'invalid_command_key')
    return hashlib.sha256(value.encode('ascii')).hexdigest()

def template_id(value):
    try:parsed=uuid.UUID(value)
    except (ValueError,AttributeError):fail(404,'not_found')
    if str(parsed)!=value or parsed.version!=4:fail(404,'not_found')
    return value

def json_digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode('utf-8')).hexdigest()

def validate_snapshot(value):
    try:return portable_bytes(value)
    except (ValidationError,ValueError,TypeError,RecursionError):fail(400,'invalid_template')

def expected(value):
    if type(value) is not int or value<1 or value>2**63-2:fail(400,'invalid_revision')
    return value

def response(data,status=200):return JSONResponse(data,status_code=status,headers=HEADERS)

def mutation(request,publisher,key,operation,tid,revision,snapshot=None):
    # Payloads are canonicalized and checked before any transaction or storage.
    normalized,raw,sha=validate_snapshot(snapshot) if operation!='revoke' else (None,None,None)
    signature=json_digest({'operation':operation,'template_id':tid,'expected_revision':revision,'snapshot_sha256':sha})
    with closing(connect(request.app)) as db:
        try:
            db.execute('BEGIN IMMEDIATE')
            prior=db.execute('SELECT request_hash,response FROM commands WHERE publisher=? AND key_hash=?',(publisher,key)).fetchone()
            if prior:
                if not hmac.compare_digest(prior[0],signature):fail(409,'command_conflict')
                db.rollback();return {**json.loads(prior[1]),'replayed':True}
            if operation=='create':
                tid=str(uuid.uuid4());newrev=1
                db.execute('INSERT INTO templates VALUES(?,?,?,0)',(tid,publisher,newrev))
            else:
                row=db.execute('SELECT publisher,revision,revoked FROM templates WHERE id=?',(tid,)).fetchone()
                if row is None or row[0]!=publisher or row[2]:fail(404,'not_found')
                if row[1]!=revision:fail(409,'revision_conflict')
                newrev=revision+1
                if operation=='revoke':db.execute('UPDATE templates SET revision=?,revoked=1 WHERE id=?',(newrev,tid))
                else:db.execute('UPDATE templates SET revision=? WHERE id=?',(newrev,tid))
            if operation!='revoke':db.execute('INSERT INTO versions VALUES(?,?,?,?)',(tid,newrev,raw,sha))
            result={'version':1,'template_id':tid,'revision':newrev,'revoked':operation=='revoke','snapshot_sha256':sha,'replayed':False}
            db.execute('INSERT INTO commands VALUES(?,?,?,?)',(publisher,key,signature,json.dumps(result,separators=(',',':'))));db.commit();return result
        except BaseException:
            db.rollback();raise

@app.get('/publisher/me')
async def publisher_me(request:Request):
    return response({'version':1,'publisher_id':identity(request)})

@app.get('/publisher/templates/{tid}')
async def publisher_template(tid:str,request:Request):
    publisher=identity(request);tid=template_id(tid)
    with closing(connect(request.app)) as db:
        row=db.execute('SELECT t.revision,t.revoked,v.sha256 FROM templates t LEFT JOIN versions v ON v.template_id=t.id AND v.revision=t.revision WHERE t.id=? AND t.publisher=?',(tid,publisher)).fetchone()
    if row is None:fail(404,'not_found')
    return response({'version':1,'template_id':tid,'revision':row[0],'revoked':bool(row[1]),'snapshot_sha256':row[2]})

@app.post('/publisher/templates')
async def create(request:Request):
    publisher=identity(request);key=command_key(request);data=await body(request)
    if type(data) is not dict or set(data)!={'snapshot'}:fail(400,'invalid_request')
    return response(mutation(request,publisher,key,'create',None,None,data['snapshot']),201)

@app.put('/publisher/templates/{tid}')
async def update(tid:str,request:Request):
    publisher=identity(request);key=command_key(request);tid=template_id(tid);data=await body(request)
    if type(data) is not dict or set(data)!={'expected_revision','snapshot'}:fail(400,'invalid_request')
    return response(mutation(request,publisher,key,'update',tid,expected(data['expected_revision']),data['snapshot']))

@app.delete('/publisher/templates/{tid}')
async def revoke(tid:str,request:Request):
    publisher=identity(request);key=command_key(request);tid=template_id(tid);data=await body(request)
    if type(data) is not dict or set(data)!={'expected_revision'}:fail(400,'invalid_request')
    return response(mutation(request,publisher,key,'revoke',tid,expected(data['expected_revision'])))

@app.get('/publisher/commands')
async def read_command(request:Request):
    publisher=identity(request);key=command_key(request)
    with closing(connect(request.app)) as db:row=db.execute('SELECT response FROM commands WHERE publisher=? AND key_hash=?',(publisher,key)).fetchone()
    if row is None:fail(404,'command_not_found')
    return response({**json.loads(row[0]),'replayed':True})

def public_current(request,tid):
    tid=template_id(tid)
    with closing(connect(request.app)) as db:
        row=db.execute('SELECT v.snapshot,v.sha256 FROM templates t JOIN versions v ON v.template_id=t.id AND v.revision=t.revision WHERE t.id=? AND t.revoked=0',(tid,)).fetchone()
    if row is None:fail(404,'not_found')
    return row

@app.get('/t/{tid}/snapshot')
async def snapshot(tid:str,request:Request):
    raw,_=public_current(request,tid)
    return Response(content=raw,media_type='application/json',headers={**HEADERS,'Content-Disposition':f'attachment; filename="{tid}.json"'})

@app.get('/t/{tid}')
async def preview(tid:str,request:Request):
    raw,_=public_current(request,tid);data=decode(raw)
    def esc(value):return html.escape(str(value),quote=True)
    def structured(value):return esc(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False))
    def schedule_text(value):
        kind=value['kind']
        if kind=='weekly':
            weekdays=('Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday')
            days=', '.join(weekdays[day] for day in value['weekdays'])
            return f"Weekly: {days}, {value['hour']:02d}:{value['minute']:02d} ({value['timezone']})"
        if kind=='interval':return f"Every {value['interval_minutes']} minutes ({value['timezone']})"
        return 'Webhook: set up a new webhook after import'
    name=esc(data['name']);description=esc(data['description']);settings=data['settings']
    temperature='Provider default' if settings['temperature']=='auto' else settings['temperature']
    output='Provider default' if settings['max_tokens']==0 else str(settings['max_tokens'])
    skills=''.join('<li><h3>'+esc(skill['name'])+'</h3><p>'+esc(skill['description'])+'</p><pre>'+esc(skill['content'])+'</pre></li>' for skill in data['skills']) or '<li>None included</li>'
    routines=''.join('<li><h3>'+esc(routine['name'])+'</h3><p>'+('Enabled' if routine['enabled'] else 'Paused')+' · '+esc(schedule_text(routine['schedule']))+'</p><pre>'+esc(routine['instruction'])+'</pre></li>' for routine in data['routines']) or '<li>None included</li>'
    # Link origin comes only from explicit operator configuration, never Host headers.
    origin=request.app.state.public_origin
    app_link=('gamjabot://template?url='+quote(f'{origin}/t/{tid}',safe='')) if origin else None
    app_action=(f'<p><a class="button" href="{html.escape(app_link,quote=True)}">Add to Gamja Bot</a></p>'
                if app_link else '<p class="muted">Open in Gamja Bot is not configured for this service.</p>')
    # Static style is CSP-hashed; all template text is escaped and inert.
    content=(f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{name}</title><style>{STYLE}</style></head><body><main>'
             f'<h1>{name}</h1><p>{description}</p>'
             '<p class="notice">The author provides these instructions without verification. Review them before importing. Enabled routines may run in the future after import; choose your own connection when importing.</p>'
             f'{app_action}<p><a class="button" href="/t/{tid}/snapshot">Download template</a></p><p class="muted">A downloaded copy cannot be recalled after removal.</p>'
             f'<h2>Role instructions</h2><pre>{esc(data["system_prompt"])}</pre>'
             f'<h2>Settings</h2><p>Temperature: {esc(temperature)}<br>Maximum output: {esc(output)}</p>'
             f'<h2>Avatar</h2><p>Included with template</p><details><summary>Avatar details</summary><pre>{structured(data["avatar"])}</pre></details>'
             f'<h2>Skills</h2><ul>{skills}</ul><h2>Routines</h2><ul>{routines}</ul>'
             f'<p class="muted">Template format version: {esc(data["version"])}</p></main></body></html>')
    return HTMLResponse(content,headers=HEADERS)
