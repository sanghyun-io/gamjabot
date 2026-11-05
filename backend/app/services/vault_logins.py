"""Durable Owner metadata; authoritative secret status comes only from Core."""
import hashlib
import json
import re
from datetime import datetime, timezone
from uuid import uuid4
from fastapi import HTTPException
from sqlalchemy import text
from app.models.models import User

ID = re.compile(r'^[0-9a-f]{32}$')
STATES = {'present', 'missing', 'deleted', 'metadata_mismatch', 'unavailable'}


def fail(code, status=422):
    raise HTTPException(status, {'code': 'vault_' + code})


def key_valid(key):
    if not isinstance(key, str) or not 1 <= len(key) <= 128 or not re.fullmatch(r'[A-Za-z0-9_-]+', key):
        fail('invalid_command')
    return key


def item_id(value):
    if not isinstance(value, str) or ID.fullmatch(value) is None:
        fail('not_found', 404)
    return value


def row_for(db, owner, item, active=True):
    row = db.execute(text('SELECT * FROM vault_logins WHERE owner_id=:o AND item_id=:i' + (' AND is_active=1' if active else '')), {'o': owner, 'i': item_id(item)}).mappings().first()
    if row is None:
        fail('not_found', 404)
    return dict(row)


def status(request, owner, row):
    unavailable = {'version': 1, 'status': 'unavailable', 'revision': None, 'command_id': None}
    provider = getattr(request.app.state, 'owner_epoch_provider', None)
    method = getattr(provider, 'vault_metadata_status', None)
    if method is None:
        return unavailable
    try:
        epoch = provider.current(owner)
        if epoch is None:
            return unavailable
        result = method(owner, row['item_id'], row['website'], row['username'])
        if provider.current(owner) != epoch:
            return unavailable
        if not isinstance(result, dict) or set(result) != {'version', 'status', 'revision', 'command_id'} or result['version'] != 1 or result['status'] not in STATES:
            return unavailable
        revision = result['revision']; command = result['command_id']
        if revision is not None and (type(revision) is not int or revision < 0):
            return unavailable
        if command is not None and (not isinstance(command, str) or len(command) > 128 or not re.fullmatch(r'[A-Za-z0-9_-]+', command)):
            return unavailable
        return result
    except Exception:
        return unavailable


def public(request, owner, row):
    return {'version': 1, 'item_id': row['item_id'], 'website': row['website'], 'username': row['username'], 'nickname': row['nickname'], 'hint': row['hint'], 'revision': row['revision'], 'is_active': bool(row['is_active']), 'created_at': row['created_at'], 'updated_at': row['updated_at'], 'secret_status': status(request, owner, row)}


def read(request, db, owner, item):
    return public(request, owner, row_for(db, owner, item))


def list_items(request, db, owner, after):
    if after is not None:
        item_id(after)
    rows = db.execute(text('SELECT * FROM vault_logins WHERE owner_id=:o AND is_active=1 AND item_id>:after ORDER BY item_id LIMIT 21'), {'o': owner, 'after': after or ''}).mappings().all()
    return {'version': 1, 'items': [public(request, owner, dict(r)) for r in rows[:20]], 'next_cursor': rows[19]['item_id'] if len(rows)>20 else None}


def receipt(db, owner, key):
    key_valid(key)
    row = db.execute(text('SELECT response FROM vault_login_commands WHERE owner_id=:o AND command_key=:k'), {'o': owner, 'k': key}).scalar_one_or_none()
    if row is None:
        fail('command_not_found', 404)
    return json.loads(row)


def write(request, db, owner, operation, item, value, key):
    key_valid(key)
    if item is not None:
        item_id(item)
    provider = getattr(request.app.state, 'owner_epoch_provider', None)
    start_epoch = provider.current(owner) if provider else None
    if start_epoch is None:
        fail('unavailable', 503)
    payload = value.model_dump(mode='json')
    digest = hashlib.sha256(json.dumps([operation, item, payload], ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    prior = db.execute(text('SELECT request_digest,response FROM vault_login_commands WHERE owner_id=:o AND command_key=:k'), {'o': owner, 'k': key}).first()
    if prior:
        if prior.request_digest != digest:
            fail('idempotency_conflict', 409)
        return json.loads(prior.response)
    # Check a deletion against Core outside the SQLite write lock; the revision
    # and the current Owner are rechecked inside the transaction below.
    if operation == 'delete':
        row = row_for(db, owner, item)
        if row['revision'] != value.expected_revision:
            fail('changed', 409)
        if status(request, owner, row)['status'] != 'deleted':
            fail('secret_not_deleted', 409)
    db.rollback()
    try:
        db.execute(text('BEGIN IMMEDIATE'))
        user = db.get(User, owner)
        provider = getattr(request.app.state, 'owner_epoch_provider', None)
        if user is None or not user.is_active or provider is None or provider.current(owner) != start_epoch:
            fail('unavailable', 503)
        prior = db.execute(text('SELECT request_digest,response FROM vault_login_commands WHERE owner_id=:o AND command_key=:k'), {'o': owner, 'k': key}).first()
        if prior:
            if prior.request_digest != digest:
                fail('idempotency_conflict', 409)
            db.rollback()
            return json.loads(prior.response)
        now = datetime.now(timezone.utc).isoformat()
        if operation == 'create':
            item = uuid4().hex
            db.execute(text('INSERT INTO vault_logins(owner_id,item_id,website,username,nickname,hint,revision,is_active,created_at,updated_at) VALUES(:o,:i,:website,:username,:nickname,:hint,1,1,:now,:now)'), {'o': owner, 'i': item, 'now': now, **payload})
            revision = 1
        else:
            row = row_for(db, owner, item)
            if row['revision'] != value.expected_revision:
                fail('changed', 409)
            revision = row['revision'] + 1
            if operation == 'update':
                params = {k: payload[k] for k in ('website', 'username', 'nickname', 'hint')}
                changed = db.execute(text('UPDATE vault_logins SET website=:website,username=:username,nickname=:nickname,hint=:hint,revision=:rev,updated_at=:now WHERE owner_id=:o AND item_id=:i AND revision=:old AND is_active=1'), {**params, 'rev':revision, 'now':now, 'o':owner, 'i':item, 'old':row['revision']}).rowcount
            else:
                changed = db.execute(text('UPDATE vault_logins SET is_active=0,revision=:rev,updated_at=:now WHERE owner_id=:o AND item_id=:i AND revision=:old AND is_active=1'), {'rev':revision,'now':now,'o':owner,'i':item,'old':row['revision']}).rowcount
            if changed != 1:
                fail('changed', 409)
        result = {'version':1, 'operation':operation, 'command_key':key, 'item_id':item, 'revision':revision}
        db.execute(text('INSERT INTO vault_login_commands(owner_id,command_key,request_digest,response) VALUES(:o,:k,:d,:r)'), {'o':owner,'k':key,'d':digest,'r':json.dumps(result,separators=(',',':'))})
        if provider.current(owner) != start_epoch:
            fail('unavailable', 503)
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


def reserve_import(request, db, owner, value, key):
    """Reserve one reviewed metadata row; duplicate check is inside the write transaction."""
    key_valid(key)
    if ID.fullmatch(key) is None:
        fail('invalid_command')
    provider = getattr(request.app.state, 'owner_epoch_provider', None)
    start_epoch = provider.current(owner) if provider else None
    if start_epoch is None:
        fail('unavailable', 503)
    payload = value.model_dump(mode='json')
    metadata = {k:payload[k] for k in ('website','username','nickname','hint')}
    digest = hashlib.sha256(json.dumps(['import_reserve', payload], ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    db.rollback()
    try:
        db.execute(text('BEGIN IMMEDIATE'))
        user = db.get(User, owner)
        if user is None or not user.is_active or provider.current(owner) != start_epoch:
            fail('unavailable', 503)
        prior = db.execute(text('SELECT request_digest,response FROM vault_login_commands WHERE owner_id=:o AND command_key=:k'), {'o':owner,'k':key}).first()
        if prior:
            if prior.request_digest != digest:
                fail('idempotency_conflict', 409)
            db.rollback()
            return json.loads(prior.response)
        existing = db.execute(text('SELECT item_id,revision FROM vault_logins WHERE owner_id=:o AND is_active=1 AND website=:website AND username=:username ORDER BY item_id LIMIT 1'), {'o':owner,'website':metadata['website'],'username':metadata['username']}).first()
        if existing:
            state, item, revision = 'duplicate', existing.item_id, existing.revision
        else:
            state, item, revision = 'created', uuid4().hex, 1
            now = datetime.now(timezone.utc).isoformat()
            db.execute(text('INSERT INTO vault_logins(owner_id,item_id,website,username,nickname,hint,revision,is_active,created_at,updated_at) VALUES(:o,:i,:website,:username,:nickname,:hint,1,1,:now,:now)'), {'o':owner,'i':item,'now':now,**metadata})
        result = {'version':1,'operation':'import_reserve','command_key':key,'state':state,'item_id':item,'revision':revision,'draft_id':payload['draft_id'],'row':payload['row']}
        db.execute(text('INSERT INTO vault_login_commands(owner_id,command_key,request_digest,response) VALUES(:o,:k,:d,:r)'), {'o':owner,'k':key,'d':digest,'r':json.dumps(result,separators=(',',':'))})
        if provider.current(owner) != start_epoch:
            fail('unavailable', 503)
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


def bind_import(request, db, owner, key):
    """Revalidate the immutable reservation and ask Core to bind its draft row."""
    if not isinstance(key, str) or ID.fullmatch(key) is None:
        fail('invalid_command')
    provider = getattr(request.app.state, 'owner_epoch_provider', None)
    method = getattr(provider, 'vault_csv_bind', None)
    epoch = provider.current(owner) if provider else None
    unavailable = {'version':1,'operation':'import_bind','command_key':key,'native_ready':False}
    if method is None or epoch is None:
        return unavailable
    saved = receipt(db, owner, key)
    if (not isinstance(saved, dict) or set(saved) != {'version','operation','command_key','state','item_id','revision','draft_id','row'}
            or saved.get('version') != 1 or saved.get('operation') != 'import_reserve'
            or saved.get('command_key') != key or saved.get('state') != 'created'
            or saved.get('revision') != 1 or not isinstance(saved.get('item_id'), str) or ID.fullmatch(saved['item_id']) is None
            or not isinstance(saved.get('draft_id'), str) or ID.fullmatch(saved['draft_id']) is None
            or type(saved.get('row')) is not int or not 2 <= saved['row'] <= 5001):
        fail('invalid_command', 409)
    row = row_for(db, owner, saved['item_id'])
    if row['revision'] != saved['revision']:
        fail('changed', 409)
    try:
        # Do not hold a SQLite read transaction across the Core exchange.
        db.rollback()
        result = method(owner, saved['draft_id'], saved['row'], saved['item_id'], row['website'], row['username'], key)
        if provider.current(owner) != epoch:
            return unavailable
        latest = row_for(db, owner, saved['item_id'])
        if (latest['revision'] != row['revision'] or latest['website'] != row['website']
                or latest['username'] != row['username']):
            return unavailable
        # This flag reflects only this Core response; it is never stored in the receipt.
        if result == {'version':1, 'state':'bound'}:
            return {'version':1,'operation':'import_bind','command_key':key,'native_ready':True}
    except Exception:
        pass
    return unavailable
