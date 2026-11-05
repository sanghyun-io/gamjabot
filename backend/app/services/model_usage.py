"""Local usage ledger and versioned, public reference prices. Never an invoice."""
import asyncio
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
from threading import RLock
from contextlib import contextmanager
from uuid import uuid4
from urllib.parse import urlsplit
from ipaddress import ip_address
import httpx

FEED = 'https://models.dev/api.json'
_store = None
SUBSCRIPTIONS = {'chatgpt', 'opencode-go', 'alibaba-coding-plan', 'github-copilot'}
PROVIDER_MAP = {'gemini': 'google', 'google-vertex': 'google-vertex'}


def now(): return datetime.now(timezone.utc).isoformat()

def counter(value): return value if type(value) is int and 0 <= value <= 10**12 else None

def number(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 10**6: return None
    return Decimal(str(value))


def extract(data, protocol):
    """Provider-reported counts only, normalizing cache/reasoning inclusion."""
    read = write = reasoning = None
    if protocol == 'native':
        inp, out = counter(data.get('input_tokens')), counter(data.get('output_tokens'))
    elif protocol == 'anthropic_messages':
        u = data.get('usage', {}); u = u if isinstance(u, dict) else {}
        inp, out = counter(u.get('input_tokens')), counter(u.get('output_tokens'))
        read, write = counter(u.get('cache_read_input_tokens', 0)), counter(u.get('cache_creation_input_tokens', 0))
        if inp is not None and read is not None and write is not None: inp += read + write
    elif protocol == 'gemini_generate_content':
        u = data.get('usageMetadata', {}); u = u if isinstance(u, dict) else {}
        inp, out = counter(u.get('promptTokenCount')), counter(u.get('candidatesTokenCount'))
        reasoning = counter(u.get('thoughtsTokenCount', 0)); read = counter(u.get('cachedContentTokenCount', 0))
        if out is not None and reasoning is not None: out += reasoning
    else:
        u = data.get('usage', {}); u = u if isinstance(u, dict) else {}
        inp, out = counter(u.get('prompt_tokens', u.get('input_tokens'))), counter(u.get('completion_tokens', u.get('output_tokens')))
        details = u.get('prompt_tokens_details', u.get('input_tokens_details', {}))
        if isinstance(details, dict): read = counter(details.get('cached_tokens'))
        details = u.get('completion_tokens_details', u.get('output_tokens_details', {}))
        if isinstance(details, dict): reasoning = counter(details.get('reasoning_tokens'))
    if inp is not None and read is not None and read > inp: inp = None
    if inp is not None and write is not None and (read or 0) + write > inp: inp = None
    if out is not None and reasoning is not None and reasoning > out: out = None
    return inp, out, read, write, reasoning


def estimate(tokens, price):
    inp, out, read, write, reasoning = tokens
    if inp is None or out is None or not isinstance(price, dict): return None
    rates = dict(price)
    tiers = rates.get('tiers', [])
    if not isinstance(tiers, list): return None
    applicable = []
    for tier in tiers:
        if not isinstance(tier, dict) or not isinstance(tier.get('tier'), dict): return None
        condition = tier['tier']
        if condition.get('type') != 'context' or counter(condition.get('size')) is None: return None
        if inp > condition['size']: applicable.append(tier)
    for tier in sorted(applicable, key=lambda row: row['tier']['size']): rates.update(tier)
    input_rate, output_rate = number(rates.get('input')), number(rates.get('output'))
    if input_rate is None or output_rate is None: return None
    cost = Decimal(inp - (read or 0) - (write or 0)) * input_rate
    for key, count in [('cache_read', read), ('cache_write', write)]:
        if count:
            rate = number(rates.get(key))
            if rate is None: return None
            cost += Decimal(count) * rate
    if 'reasoning' in rates:
        rate = number(rates['reasoning'])
        if rate is None or reasoning is None: return None
        cost += Decimal(out - reasoning) * output_rate + Decimal(reasoning) * rate
    else: cost += Decimal(out) * output_rate
    return str(cost / Decimal(1_000_000))


def billing_mode(config):
    provider = config.provider_id or config.provider_kind
    host = urlsplit(config.base_url or '').hostname or ''
    # Legacy compatible connections can still point at a fixed subscription
    # endpoint. Exact hosts only; never infer a plan from a display name.
    plan_hosts = {'token-plan.ap-southeast-1.maas.aliyuncs.com',
                  'coding-intl.dashscope.aliyuncs.com', 'coding.dashscope.aliyuncs.com'}
    if provider in SUBSCRIPTIONS or config.auth_method_id in {'oauth_browser','oauth_device'} or host in plan_hosts:
        return 'subscription'
    try: local = ip_address(host).is_loopback
    except ValueError: local = host == 'localhost'
    return 'local' if local else 'reference'


class UsageStore:
    def __init__(self, path, session_factory):
        self.path, self.sessions, self.lock = Path(path), session_factory, RLock()
        self.dropped = 0; self.task = None; self.refresh_lock = asyncio.Lock()
        if self.path.resolve() != self.path: raise ValueError('usage_path')
        if not self.path.exists():
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600); os.close(fd)
        info = self.path.lstat()
        if self.path.is_symlink() or info.st_uid != os.geteuid() or info.st_mode & 0o077: raise ValueError('usage_permissions')
        with self.db() as db:
            db.executescript('''
              PRAGMA journal_mode=WAL;
              CREATE TABLE IF NOT EXISTS usage_requests (
                id TEXT PRIMARY KEY, at TEXT NOT NULL, owner INTEGER NOT NULL, connection_id INTEGER,
                agent_id INTEGER, room_id INTEGER, provider TEXT NOT NULL, model TEXT NOT NULL,
                auth TEXT, billing TEXT NOT NULL, state TEXT NOT NULL,
                input_tokens INTEGER, output_tokens INTEGER, cache_read INTEGER, cache_write INTEGER,
                reasoning INTEGER, price_json TEXT, estimate_usd TEXT, price_at TEXT, assumed_cache INTEGER,
                accounting_scope TEXT NOT NULL
              );
              CREATE INDEX IF NOT EXISTS usage_owner_time ON usage_requests(owner,at);
              CREATE TABLE IF NOT EXISTS usage_prices (
                provider TEXT, model TEXT, fingerprint TEXT, observed_at TEXT, cost TEXT,
                PRIMARY KEY(provider,model,fingerprint)
              );
              CREATE TABLE IF NOT EXISTS usage_meta (key TEXT PRIMARY KEY,value TEXT);
            ''')
            # An interrupted process cannot claim the preceding in-flight calls were free.
            db.execute("UPDATE usage_requests SET state='interrupted_unknown' WHERE state='started'")
    @contextmanager
    def db(self):
        connection = sqlite3.connect(self.path, timeout=.2)
        try:
            with connection: yield connection
        finally: connection.close()
    def meta(self, db, key, fallback=None):
        row = db.execute('SELECT value FROM usage_meta WHERE key=?', (key,)).fetchone()
        return row[0] if row else fallback
    def set_meta(self, db, key, value): db.execute('INSERT OR REPLACE INTO usage_meta VALUES (?,?)', (key, value))
    def price(self, db, provider, model):
        provider = PROVIDER_MAP.get(provider, provider)
        active = self.meta(db, 'active:' + json.dumps([provider,model]))
        row = db.execute('SELECT cost,observed_at FROM usage_prices WHERE provider=? AND model=? AND fingerprint=?', (provider, model, active)).fetchone()
        return row if row else (None, None)
    def start(self, request, scope):
        config = request.connection
        if type(config.owner_id) is not int or config.owner_id < 1: return None
        provider = config.provider_id or config.provider_kind
        billing = billing_mode(config)
        key = str(uuid4())
        try:
            with self.lock, self.db() as db:
                price, observed = self.price(db, provider, request.model_id)
                db.execute('INSERT INTO usage_requests(id,at,owner,connection_id,agent_id,room_id,provider,model,auth,billing,state,price_json,price_at,accounting_scope) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                  (key,now(),config.owner_id,config.connection_id,request.agent_id,request.room_id,provider,request.model_id,config.auth_method_id,billing,'started',price,observed,scope))
            return key
        except (OSError, sqlite3.Error): self.dropped += 1; return None
    def finish(self, key, data=None, protocol='', state='response_received'):
        if not key: return
        try:
            values = extract(data if isinstance(data, dict) else {}, protocol)
            with self.lock, self.db() as db:
                row = db.execute('SELECT price_json,billing FROM usage_requests WHERE id=?', (key,)).fetchone()
                price = json.loads(row[0]) if row and row[0] else None
                cost = estimate(values, price) if row and row[1] == 'reference' else None
                db.execute('UPDATE usage_requests SET state=?,input_tokens=?,output_tokens=?,cache_read=?,cache_write=?,reasoning=?,estimate_usd=?,assumed_cache=? WHERE id=?',
                  (state,*values,cost,int(values[2] is None),key))
        except (OSError, sqlite3.Error, ValueError, TypeError, InvalidOperation): self.dropped += 1
    def targets(self):
        from app.models.collaboration import ProviderConnection
        from app.models.models import Agent
        with self.sessions() as db:
            connections = db.query(ProviderConnection).all()
            agents = db.query(Agent).all()
            by_id = {c.id:c for c in connections}
            pairs = {(c.provider_id or c.kind,c.default_model_id) for c in connections}
            pairs |= {(by_id[a.connection_id].provider_id or by_id[a.connection_id].kind,a.model_id or a.model) for a in agents if a.connection_id in by_id}
        with self.db() as db: pairs |= set(db.execute('SELECT DISTINCT provider,model FROM usage_requests'))
        return {(PROVIDER_MAP.get(p,p),m) for p,m in pairs if p and m}
    async def refresh(self, force=False):
        async with self.refresh_lock:
            with self.db() as db:
                last = self.meta(db,'last_attempt')
                delay = 60 if force else (3600 if self.meta(db,'last_error') else 86400)
                if last and (datetime.now(timezone.utc)-datetime.fromisoformat(last)).total_seconds() < delay: return
                self.set_meta(db,'last_attempt',now())
            try:
                payload = bytearray()
                async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=30) as client:
                    async with client.stream('GET',FEED,headers={'Accept':'application/json'}) as response:
                        response.raise_for_status()
                        async for chunk in response.aiter_bytes():
                            payload.extend(chunk)
                            if len(payload) > 16*1024*1024: raise ValueError('catalog_size')
                catalog = json.loads(payload)
                if not isinstance(catalog,dict) or not catalog: raise ValueError('catalog_shape')
                rows=[]; stamp=now()
                targets = self.targets()
                for provider, model in targets:
                    entry = catalog.get(provider,{}).get('models',{}).get(model,{})
                    cost = entry.get('cost') if isinstance(entry,dict) else None
                    if not isinstance(cost,dict) or number(cost.get('input')) is None or number(cost.get('output')) is None: continue
                    raw=json.dumps(cost,sort_keys=True,allow_nan=False)
                    if len(raw)>16000: continue
                    rows.append((provider,model,hashlib.sha256(raw.encode()).hexdigest(),stamp,raw))
                with self.lock, self.db() as db:
                    db.executemany('INSERT OR IGNORE INTO usage_prices VALUES (?,?,?,?,?)',rows)
                    for provider,model in targets:
                        self.set_meta(db,'active:' + json.dumps([provider,model]),'')
                    for provider,model,fingerprint,_,_ in rows:
                        self.set_meta(db,'active:' + json.dumps([provider,model]),fingerprint)
                    self.set_meta(db,'last_success',stamp); self.set_meta(db,'last_error',''); self.set_meta(db,'feed_rows',str(len(rows)))
            except asyncio.CancelledError: raise
            except Exception as error:
                with self.db() as db: self.set_meta(db,'last_error','price_refresh_failed:' + type(error).__name__)
    async def run(self):
        while True:
            try: await self.refresh()
            except asyncio.CancelledError: raise
            except Exception: pass
            await asyncio.sleep(3600)
    def report(self, owner, days):
        since=(datetime.now(timezone.utc)-timedelta(days=days)).isoformat()
        with self.lock, self.db() as db:
            db.row_factory=sqlite3.Row
            groups=[dict(r) for r in db.execute('''SELECT connection_id,provider,model,auth,billing,
                COUNT(*) attempts, COUNT(input_tokens) input_reported,COUNT(output_tokens) output_reported,
                SUM(input_tokens) input_tokens,SUM(output_tokens) output_tokens,
                SUM(cache_read) cache_read,SUM(cache_write) cache_write,SUM(reasoning) reasoning,
                COUNT(estimate_usd) priced, SUM(CAST(estimate_usd AS REAL)) estimate_usd,
                SUM(COALESCE(assumed_cache,0)) assumed_cache,MAX(price_at) price_at
                FROM usage_requests WHERE owner=? AND at>=? GROUP BY connection_id,provider,model,auth,billing
                ORDER BY attempts DESC''',(owner,since))]
            prices=[]
            for row in groups:
                if any(p['provider']==row['provider'] and p['model']==row['model'] for p in prices): continue
                cost,at=self.price(db,row['provider'],row['model'])
                prices.append({'provider':row['provider'],'model':row['model'],'cost':json.loads(cost) if cost else None,'observed_at':at})
            # Also show configured prices before the first call, only for this Owner.
            from app.models.collaboration import ProviderConnection
            from app.models.models import Agent
            with self.sessions() as session:
                conns=session.query(ProviderConnection).filter_by(user_id=owner).all()
                by_id={c.id:c for c in conns}; pairs={(c.provider_id or c.kind,c.default_model_id) for c in conns}
                pairs|={(by_id[a.connection_id].provider_id or by_id[a.connection_id].kind,a.model_id or a.model) for a in session.query(Agent).filter_by(user_id=owner).all() if a.connection_id in by_id}
            seen={(p['provider'],p['model']) for p in prices}
            for provider,model in sorted((p,m) for p,m in pairs-seen if p and m):
                cost,at=self.price(db,provider,model);prices.append({'provider':provider,'model':model,'cost':json.loads(cost) if cost else None,'observed_at':at})
            return {'days':days,'since':since,'groups':groups,'prices':prices,'last_attempt':self.meta(db,'last_attempt'),'last_success':self.meta(db,'last_success'),'last_error':self.meta(db,'last_error'),'feed_source':FEED,'interval_hours':24,'dropped_records':self.dropped,'capture_started':self.meta(db,'capture_started')}


def begin(request, scope='http_attempt'):
    if _store is None: return None
    try: return _store.start(request,scope)
    except Exception: _store.dropped += 1; return None

def finish(key, data=None, protocol='', state='response_received'):
    if _store is not None: _store.finish(key,data,protocol,state)


def install(path, session_factory):
    global _store
    _store=UsageStore(path,session_factory)
    with _store.db() as db:
        if not _store.meta(db,'capture_started'): _store.set_meta(db,'capture_started',now())
    _store.task=asyncio.create_task(_store.run())
    return _store


async def close():
    global _store
    store,_store=_store,None
    if store and store.task:
        store.task.cancel()
        await asyncio.gather(store.task,return_exceptions=True)
