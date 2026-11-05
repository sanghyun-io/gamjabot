"""Disposable SQLite/API contracts; no installed data, credentials or model traffic."""
from concurrent.futures import ThreadPoolExecutor
import json
from uuid import uuid4
import pytest
from fastapi import HTTPException
from app.models.models import Agent, Conversation
from app.models.collaboration import RoomAgentRun,RoomEvent,RoomTrigger,ToolAction
from app.models.room_memory import RoomMemoryEntry,RoomMemoryState
from app.schemas.room_memory import MemoryWrite,MemoryEdit
from app.services import room_memory as svc
from app.services.memory_context import snapshot,digest
from app.services.events import ActorRef,append_event
from test_room_state import room_client,create_room,headers
from test_scheduler import ready_agents

@pytest.fixture
def memory(room_client):
    c,(owner,other,project,_,foreign)=room_client
    room=create_room(c,owner,project,'Memory A');second=create_room(c,owner,project,'Memory B')
    a,b=ready_agents(c,owner,room,('Alpha','Beta'))
    for agent in (a,b):assert c.post(f'/api/v1/conversations/{second}/members/{agent}',headers=headers(c,owner)).status_code==201
    return c,dict(owner=owner,other=other,room=room,second=second,a=a,b=b,foreign=foreign)

def url(ids,room=None):return f'/api/v1/conversations/{room or ids["room"]}/memories'
def h(c,i,key='key',owner=None):return headers(c,owner or i['owner'])|{'Idempotency-Key':key}
def body(rev=0,**kw):return dict(expected_room_revision=rev,category='fact',content='Remember me',scope='room') | kw
def add(c,i,key='add',**kw):return c.post(url(i),json=body(**kw),headers=h(c,i,key))

def test_manual_canonical_scope_metadata_and_original_preservation(memory):
    c,i=memory
    with c.app.state.session_factory() as db:
        event=append_event(db,i['room'],ActorRef('user',i['owner']),'message',{'content':'ORIGINAL'})
        before=(event.event_id,event.payload,db.get(Agent,i['a']).system_prompt)
    r=add(c,i);assert r.status_code==200,r.text
    listing=c.get(url(i),headers=h(c,i)).json();e=listing['items'][0]
    assert e['source_kind']=='manual' and e['source_event_id'] is None and e['source_snapshot']=={}
    assert e['created_by']==i['owner'] and e['updated_at'].endswith('+00:00') and listing['revision']==1
    assert 'no-store' in r.headers['cache-control']
    with c.app.state.session_factory() as db:
        assert db.get(RoomEvent,before[0]).payload==before[1];assert db.get(Agent,i['a']).system_prompt==before[2]
        for agent in (i['a'],i['b']):assert snapshot(db,i['room'],agent)['memory']['entries'][0]['content']=='Remember me'
        assert snapshot(db,i['second'],i['a'])['memory']=={}
        activity=db.query(RoomEvent).filter_by(room_id=i['room'],kind='activity').one();assert 'Remember me' not in json.dumps(activity.payload)

def test_replay_conflict_crossroom_owner_and_archived(memory):
    c,i=memory;r=add(c,i);assert r.status_code==200
    repeat=add(c,i);assert repeat.json()==r.json()|{'replayed':True}
    assert add(c,i,content='different').status_code==409
    mid=r.json()['memory_id'];assert c.get(url(i,i['second'])+'/'+mid,headers=h(c,i)).status_code==404
    assert c.get(url(i),headers=h(c,i,owner=i['other'])).status_code==404
    with c.app.state.session_factory() as db:db.get(Conversation,i['room']).is_active=False;db.commit()
    assert add(c,i).json()['replayed'] is True
    assert add(c,i,key='new',rev=1).status_code==409

@pytest.mark.parametrize('bad',[{'expected_room_revision':True},{'target_agent_id':True,'scope':'agent'}, {'content':' '},{'category':'unknown'},{'author':'invented'},{'source':{'kind':'manual','url':'https://a.invalid'}},{'source':{'kind':'link','url':'file:///private'}},{'scope':'agent'}])
def test_strict_no_claimed_provenance_and_invalid_input(memory,bad):
    c,i=memory;v=body();v.update(bad);r=c.post(url(i),json=v,headers=h(c,i));assert r.status_code==422,r.text
    assert c.get(url(i),headers=h(c,i)).json()['revision']==0

def test_event_source_and_link_target_guards(memory):
    c,i=memory
    with c.app.state.session_factory() as db:
        e=append_event(db,i['room'],ActorRef('user',i['owner']),'message',{'content':'source'});eid,seq=e.event_id,e.seq
    r=add(c,i,source={'kind':'event','event_id':eid,'seq':seq});assert r.status_code==200,r.text
    e=c.get(url(i)+'/'+r.json()['memory_id'],headers=h(c,i)).json();assert e['source_snapshot']['actor_id']==i['owner']
    assert add(c,i,key='wrong',rev=1,source={'kind':'event','event_id':eid,'seq':seq+1}).status_code==404
    assert add(c,i,key='foreign',rev=1,scope='agent',target_agent_id=i['foreign']).status_code==404
    assert add(c,i,key='link',rev=1,source={'kind':'link','url':'https://example.invalid/unchecked'}).status_code==200

@pytest.mark.parametrize('kind',['pending_trigger','running','waiting','active_tool','work_wave','uncertain','approved','unknown'])
def test_busy_failclosed_without_mutation(memory,kind):
    c,i=memory
    with c.app.state.session_factory() as db:
        run=db.get(RoomAgentRun,(i['room'],i['a']))
        if kind=='pending_trigger':
            e=append_event(db,i['room'],ActorRef('user',i['owner']),'message',{'content':'request'})
            db.add(RoomTrigger(event_id=e.event_id,room_id=i['room'],state='pending'))
        elif kind in {'running','waiting','unknown'}:run.state=kind
        elif kind=='active_tool':run.active_tool={'id':'opaque'}
        elif kind=='work_wave':run.pending_task={'work_wave':{'pending':True}}
        else:db.add(ToolAction(action_id=str(uuid4()),room_id=i['room'],agent_id=i['a'],family='execute',arguments={},state=kind))
        db.commit()
    r=add(c,i);assert r.status_code==409 and r.json()['detail']['code']=='memory_room_busy'
    assert c.get(url(i),headers=h(c,i)).json()['items']==[]


def test_agent_scope_edit_scope_union_floor_and_soft_delete(memory):
    c,i=memory;r=add(c,i,scope='agent',target_agent_id=i['a']);assert r.status_code==200
    mid=r.json()['memory_id']
    with c.app.state.session_factory() as db:
        assert snapshot(db,i['room'],i['b'])['memory']=={}
    v=body(1,scope='agent',target_agent_id=i['b']);v.update(content='Corrected',expected_revision=1)
    r=c.patch(url(i)+'/'+mid,json=v,headers=h(c,i,'edit'));assert r.status_code==200,r.text
    floor=r.json()['activity_seq']
    with c.app.state.session_factory() as db:
        for a in (i['a'],i['b']):assert snapshot(db,i['room'],a)['binding']['context_after_seq']==floor
        assert snapshot(db,i['room'],i['a'])['memory']=={};assert snapshot(db,i['room'],i['b'])['memory']['entries'][0]['content']=='Corrected'
    assert c.patch(url(i)+'/'+mid,json=v,headers=h(c,i,'stale')).status_code==409
    d={'expected_room_revision':2,'expected_revision':2};r=c.request('DELETE',url(i)+'/'+mid,json=d,headers=h(c,i,'delete'));assert r.status_code==200
    with c.app.state.session_factory() as db:
        assert db.get(RoomMemoryEntry,mid).deleted_at is not None
        assert snapshot(db,i['room'],i['b'])['memory']=={}
        assert db.get(RoomMemoryState,i['room']).common_context_after_seq==0


def test_legacy_disable_replace_and_original_json_preserved(memory):
    c,i=memory
    with c.app.state.session_factory() as db:db.get(RoomAgentRun,(i['room'],i['a'])).memory={'old':'legacy'};db.commit()
    legacy=c.get(url(i),headers=h(c,i)).json()['agents'][0]
    v={'expected_room_revision':0,'expected_agent_revision':0,'expected_legacy_digest':legacy['legacy_digest'],'operation':'replace',
       'replacement':body(scope='agent',target_agent_id=i['a'])}
    r=c.post(url(i)+f'/legacy/{i["a"]}',json=v,headers=h(c,i,'legacy'));assert r.status_code==200,r.text
    with c.app.state.session_factory() as db:
        assert db.get(RoomAgentRun,(i['room'],i['a'])).memory=={'old':'legacy'}
        snap=snapshot(db,i['room'],i['a']);assert snap['binding']['legacy_disabled'] and snap['legacy']=={}
        assert snap['memory']['entries'][0]['source_kind']=='manual'
        assert snapshot(db,i['second'],i['a'])['memory']=={}


def test_budget_rollback_and_json_overhead(memory):
    c,i=memory
    for n in range(3):assert add(c,i,key=str(n),rev=n,content='x'*2000).status_code==200
    before=c.get(url(i),headers=h(c,i)).json();r=add(c,i,key='overflow',rev=3,content='x'*2000);assert r.status_code==422
    assert c.get(url(i),headers=h(c,i)).json()==before


def test_concurrent_cas_and_atomic_audit_rollback(memory,monkeypatch):
    c,i=memory;first=add(c,i).json();mid=first['memory_id'];v=MemoryEdit(**(body(1)|{'expected_revision':1}))
    def edit(key):
        with c.app.state.session_factory() as db:
            try:return svc.mutate(db,i['owner'],i['room'],'edit',v,key,mid)['revision']
            except HTTPException as e:return e.status_code
    with ThreadPoolExecutor(2) as pool:out=list(pool.map(edit,['one','two']))
    assert sorted(out)==[2,409]
    before=c.get(url(i),headers=h(c,i)).json()
    def broken(*a,**k):raise RuntimeError('synthetic audit persistence failure')
    monkeypatch.setattr(svc,'stage_event',broken)
    with c.app.state.session_factory() as db:
        with pytest.raises(RuntimeError):svc.mutate(db,i['owner'],i['room'],'add',MemoryWrite(**body(2)),'rollback')
    assert c.get(url(i),headers=h(c,i)).json()==before


def test_public_owner_cli_uses_same_api_contract(memory,capsys):
    from app.owner_cli import command
    c,i=memory
    class Client:
        def call(self,method,path,payload=None,authenticated=False,**kw):
            hds=h(c,i,kw.get('idempotency_key','cli'));r=c.request(method,'/api/v1'+path,json=payload,headers=hds);r.raise_for_status();return r.json()
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmp:
        q=Path(tmp)/'command.json';q.write_text(json.dumps(body()))
        command(Client(),['room','memory','add',str(i['room']),'--json',str(q),'--idempotency-key','cli'])
    result=json.loads(capsys.readouterr().out);assert result['revision']==1
    assert c.get(url(i),headers=h(c,i)).json()['items'][0]['content']=='Remember me'


def test_common_to_agent_floor_applies_to_future_participants(memory):
    c,i=memory;r=add(c,i);mid=r.json()['memory_id'];v=body(1,scope='agent',target_agent_id=i['a'])|{'expected_revision':1}
    changed=c.patch(url(i)+'/'+mid,json=v,headers=h(c,i,'narrow'));assert changed.status_code==200
    floor=changed.json()['activity_seq']
    (future,)=ready_agents(c,i['owner'],i['room'],('Future',))
    with c.app.state.session_factory() as db:
        assert snapshot(db,i['room'],future)['binding']['context_after_seq']==floor
        assert snapshot(db,i['room'],future)['memory']=={}
        assert snapshot(db,i['second'],i['a'])['binding']['context_after_seq']==0


def test_removed_agent_rejoin_keeps_tombstone_and_disabled_legacy(memory):
    from app.models.collaboration import RoomMembership
    c,i=memory;r=add(c,i,scope='agent',target_agent_id=i['a']);mid=r.json()['memory_id']
    r=c.request('DELETE',url(i)+'/'+mid,json={'expected_room_revision':1,'expected_revision':1},headers=h(c,i,'delete'));assert r.status_code==200
    floor=r.json()['activity_seq']
    with c.app.state.session_factory() as db:
        m=db.get(RoomMembership,(i['room'],i['a']));m.is_active=False;db.commit()
        assert snapshot(db,i['room'],i['a'])['memory']=={}
        m.is_active=True;db.commit();s=snapshot(db,i['room'],i['a']);assert s['memory']=={} and s['binding']['context_after_seq']==floor


def test_fresh_inactive_owner_cannot_replay_receipt(memory):
    from app.models.models import User
    c,i=memory;r=add(c,i);assert r.status_code==200
    with c.app.state.session_factory() as db:
        db.get(User,i['owner']).is_active=False;db.commit()
    # Service fresh lookup must protect replay even independently of HTTP auth.
    with c.app.state.session_factory() as db:
        with pytest.raises(HTTPException) as e:svc.mutate(db,i['owner'],i['room'],'add',MemoryWrite(**body()),'add')
    assert e.value.status_code==404
