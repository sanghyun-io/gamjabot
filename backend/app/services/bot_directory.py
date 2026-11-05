"""Owner-only directory preferences with optimistic concurrency and exact receipts."""
import json
from hashlib import sha256
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import text
from app.models.models import Agent, User
from app.schemas.bot_directory import DirectorySnapshot
from app.services.peer_reply import canonical_id


def fail(code, status=422):
    raise HTTPException(status, {'code': 'bot_directory_' + code})


def _key(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 200 or not value.isprintable():
        fail('invalid')
    return value


def _decode(raw):
    try:
        value = DirectorySnapshot.model_validate(json.loads(raw), strict=True).model_dump(mode='json')
        _shape(value)
        return value
    except (ValueError, TypeError, ValidationError):
        fail('unavailable', 503)


def _shape(value):
    sections=value['sections'];items=value['items']
    ids=[s['id'] for s in sections];names=[s['name'] for s in sections]
    agents=[i['agent_id'] for i in items]
    if (any(not canonical_id(x) for x in ids) or len(set(ids)) != len(ids)
        or any(not n.strip() or n != n.strip() or not n.isprintable() for n in names)
        or len(set(n.casefold() for n in names)) != len(names)
        or len(set(agents)) != len(agents)
        or any(i['section_id'] is not None and i['section_id'] not in ids for i in items)):
        raise ValueError('Invalid directory shape')


def read(db, owner):
    row=db.execute(text('SELECT revision,snapshot FROM bot_directory_layouts WHERE owner_id=:owner'), {'owner':owner}).first()
    return {'version':1,'revision':row.revision,**_decode(row.snapshot)} if row else {'version':1,'revision':0,'sections':[],'items':[]}


def write(db, owner, value, key):
    key=_key(key);snapshot={'sections':[s.model_dump(mode='json') for s in value.sections],
                            'items':[i.model_dump(mode='json') for i in value.items]}
    try: _shape(snapshot)
    except ValueError: fail('invalid')
    request=json.dumps([value.expected_revision,snapshot],sort_keys=True,separators=(',',':'),ensure_ascii=False)
    digest=sha256(request.encode('utf-8')).hexdigest()
    db.rollback()
    try:
        db.execute(text('BEGIN IMMEDIATE'))
        user=db.get(User,owner)
        if user is None or not user.is_active: fail('unavailable',404)
        prior=db.execute(text('SELECT request_digest,response FROM bot_directory_commands WHERE owner_id=:owner AND idempotency_key=:key'),{'owner':owner,'key':key}).first()
        if prior:
            if prior.request_digest != digest: fail('idempotency_conflict',409)
            response=json.loads(prior.response);db.rollback();return {**response,'replayed':True}
        agent_ids=[i['agent_id'] for i in snapshot['items']]
        for start in range(0,len(agent_ids),300):
            batch=agent_ids[start:start+300]
            owned={r[0] for r in db.query(Agent.id).filter(Agent.user_id==owner,Agent.id.in_(batch)).all()}
            if len(owned)!=len(batch): fail('invalid')
        row=db.execute(text('SELECT revision,snapshot FROM bot_directory_layouts WHERE owner_id=:owner'),{'owner':owner}).first()
        current=row.revision if row else 0
        if current!=value.expected_revision: fail('changed',409)
        if row:
            previous=_decode(row.snapshot)
            deleted={section['id'] for section in previous['sections']} - {section['id'] for section in snapshot['sections']}
            new_items={item['agent_id']:item for item in snapshot['items']}
            for old in previous['items']:
                if old['section_id'] in deleted:
                    new=new_items.get(old['agent_id'])
                    if not new or new['section_id'] is not None or new['pinned']!=old['pinned'] or new['hidden']!=old['hidden']:
                        fail('invalid')
        revision=current+1
        encoded=json.dumps(snapshot,ensure_ascii=False,separators=(',',':'))
        if row:
            changed=db.execute(text('UPDATE bot_directory_layouts SET revision=:revision,snapshot=:snapshot WHERE owner_id=:owner AND revision=:current'),
                               {'revision':revision,'snapshot':encoded,'owner':owner,'current':current}).rowcount
            if changed!=1: fail('changed',409)
        else:
            db.execute(text('INSERT INTO bot_directory_layouts(owner_id,revision,snapshot) VALUES(:owner,:revision,:snapshot)'),
                       {'owner':owner,'revision':revision,'snapshot':encoded})
        response={'version':1,'command_key':key,'expected_revision':current,'revision':revision,**snapshot,'replayed':False}
        db.execute(text('INSERT INTO bot_directory_commands(owner_id,idempotency_key,request_digest,response) VALUES(:owner,:key,:digest,:response)'),
                   {'owner':owner,'key':key,'digest':digest,'response':json.dumps(response,ensure_ascii=False,separators=(',',':'))})
        db.commit();return response
    except BaseException:
        db.rollback();raise


def receipt(db, owner, key):
    key=_key(key)
    row=db.execute(text('SELECT response FROM bot_directory_commands WHERE owner_id=:owner AND idempotency_key=:key'),{'owner':owner,'key':key}).first()
    if not row: fail('command_not_found',404)
    return {**json.loads(row.response),'replayed':True}
