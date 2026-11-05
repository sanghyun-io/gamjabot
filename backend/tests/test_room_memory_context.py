"""Context/queue/await/floor boundaries using owned SQLite and synthetic providers."""
import asyncio,json,importlib,sqlite3
from pathlib import Path
import pytest
from app.models.collaboration import RoomAgentRun,RoomEvent,RoomMembership,RoomTrigger
from app.models.room_memory import RoomMemoryState
from app.services.events import append_event,ActorRef
from app.services.context import append_room_summary,build_turn_context
from app.services.context_projection import project_turn_context
from app.services.memory_context import snapshot,require_current
from app.services.providers import ProviderResult
from app.services.llm_service import LLMFailure
from test_room_memory import memory,room_client,url,h,body,add


def test_edit_delete_excludes_old_summary_and_legacy_context(memory):
    c,i=memory
    with c.app.state.session_factory() as db:
        a=append_event(db,i['room'],ActorRef('user',i['owner']),'message',{'content':'OLD_FACT'})
        append_room_summary(db,i['room'],1,1,'OLD_SUMMARY')
    r=add(c,i);mid=r.json()['memory_id'];v=body(1)|{'content':'NEW_FACT','expected_revision':1}
    assert c.patch(url(i)+'/'+mid,json=v,headers=h(c,i,'edit')).status_code==200
    # Even a late summary claiming pre-floor source range cannot bring history back.
    with c.app.state.session_factory() as db:append_room_summary(db,i['room'],1,1,'LATE_OLD_SUMMARY')
    preview=c.post(f'/api/v1/conversations/{i["room"]}/context-preview',json={'content':'NEXT'},headers=h(c,i)).json()
    text=json.dumps(preview);assert 'OLD_FACT' not in text and 'OLD_SUMMARY' not in text and 'NEW_FACT' in text
    captured=[]
    async def provider(request):captured.append(request);return ProviderResult(text='new answer')
    c.app.state.scheduler.provider=provider
    r=c.post(f'/api/v1/conversations/{i["room"]}/messages',json={'content':'NEXT'},headers=h(c,i));assert r.status_code==201,r.text
    assert captured and all('OLD_FACT' not in json.dumps(x.events) and 'NEW_FACT' in x.memory for x in captured)


def test_scoped_checkpoint_reuse_cannot_cross_agent_or_revision(memory):
    c,i=memory;assert add(c,i,scope='agent',target_agent_id=i['a']).status_code==200
    with c.app.state.session_factory() as db:
        for n in range(30):append_event(db,i['room'],ActorRef('user',i['owner']),'message',{'content':str(n)})
        events=[dict(event_id=e.event_id,room_id=e.room_id,seq=e.seq,actor_type=e.actor_type,actor_id=e.actor_id,actor_name_snapshot=e.actor_name_snapshot,kind=e.kind,payload=e.payload) for e in db.query(RoomEvent).filter_by(room_id=i['room']).order_by(RoomEvent.seq)]
        snaps=[snapshot(db,i['room'],agent) for agent in (i['a'],i['b'])]
    def proj(s,extra=[]):return project_turn_context(events=events,extractive_checkpoints=extra,watermark_seq=events[-1]['seq'],active_event_id=events[-1]['event_id'],agent_id=s['binding']['agent_id'],system='',memory=s['memory'],tools=[],memory_binding=s['binding'])
    p=proj(snaps[0]);plan=p.checkpoint_plan;assert plan and plan.memory_binding['agent_id']==i['a']
    stored=p.events[0] if p.events[0]['payload'].get('extractive_checkpoint') else next(e for e in p.events if e['payload'].get('extractive_checkpoint'))
    reused=proj(snaps[0],[stored])
    assert reused.checkpoint_plan is None and reused.messages==p.messages
    assert proj(snaps[1],[stored]).checkpoint_plan is not None
    other={**snaps[0],'binding':{**snaps[0]['binding'],'room_revision':2}}
    assert proj(other,[stored]).checkpoint_plan is not None


def test_memory_write_during_provider_request_is_rejected(memory):
    c,i=memory;out=[]
    async def provider(request):
        # Use the core directly from another session while scheduler owns a running trigger.
        from app.services.room_memory import mutate
        from app.schemas.room_memory import MemoryWrite
        from fastapi import HTTPException
        with c.app.state.session_factory() as db:
            try:mutate(db,i['owner'],i['room'],'add',MemoryWrite(**body()),'while-running')
            except HTTPException as e:out.append(e.detail['code'])
        return ProviderResult(text='done')
    c.app.state.scheduler.provider=provider
    r=c.post(f'/api/v1/conversations/{i["room"]}/messages',json={'content':'work'},headers=h(c,i));assert r.status_code==201
    assert out and set(out)=={'memory_room_busy'}


def test_connection_await_revision_drift_blocks_provider_request(memory,monkeypatch):
    c,i=memory;import app.services.context as context
    original=context.resolve_connection_async
    async def resolve(*a,**kw):
        result=await original(*a,**kw)
        with c.app.state.session_factory() as db:
            # Deliberate DB bypass tests defense after await, not an allowed Owner edit.
            state=db.get(RoomMemoryState,i['room'])
            if not state:state=RoomMemoryState(room_id=i['room'],revision=0,common_context_after_seq=0);db.add(state)
            state.revision+=1;db.commit()
        return result
    monkeypatch.setattr(context,'resolve_connection_async',resolve)
    calls=[]
    async def provider(req):calls.append(req);return ProviderResult(text='must not run')
    c.app.state.scheduler.provider=provider
    r=c.post(f'/api/v1/conversations/{i["room"]}/messages',json={'content':'work'},headers=h(c,i));assert r.status_code==502
    assert calls==[]
    with c.app.state.session_factory() as db:assert db.query(RoomEvent).filter_by(room_id=i['room'],kind='failure').first().payload['code']=='memory_context_changed'


def test_queue_source_before_floor_refused_and_room_isolation(memory):
    c,i=memory
    with c.app.state.session_factory() as db:e=append_event(db,i['room'],ActorRef('user',i['owner']),'message',{'content':'unadmitted request'});eid=e.event_id
    r=add(c,i);mid=r.json()['memory_id'];assert c.request('DELETE',url(i)+'/'+mid,json={'expected_room_revision':1,'expected_revision':1},headers=h(c,i,'delete')).status_code==200
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as error:asyncio.run(c.app.state.scheduler.schedule_user_event(i['room'],eid))
    assert error.value.status_code==409
    with c.app.state.session_factory() as db:
        assert db.get(RoomTrigger,eid) is None
        assert snapshot(db,i['second'],i['a'])['binding']['context_after_seq']==0


def test_additive_27_to28_original_rows_ddl_and_repeat_bytes(tmp_path,monkeypatch):
    import app.core.migrate as m
    from sqlalchemy import create_engine
    monkeypatch.setenv('GAMJA_APP_DATA_DIR',str(tmp_path/'owned-appdata'))
    path=tmp_path/'owned.sqlite';url='sqlite:///'+str(path);original=m.REVISIONS
    monkeypatch.setattr(m,'REVISIONS',original[:-1]);engine=create_engine(url);m.migrate_database(engine);engine.dispose()
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO users(id,email,username,hashed_password,is_active) VALUES(1,'x@test.invalid','Synthetic','x',1)");db.commit()
        before={name:(sql,db.execute('SELECT * FROM "'+name+'"').fetchall()) for name,sql in db.execute("SELECT name,sql FROM sqlite_master WHERE type='table' AND name!='schema_version'").fetchall()}
    monkeypatch.setattr(m,'REVISIONS',original);engine=create_engine(url);m.migrate_database(engine);engine.dispose()
    with sqlite3.connect(path) as db:
        for name,(ddl,rows) in before.items():assert db.execute("SELECT sql FROM sqlite_master WHERE name=?",(name,)).fetchone()[0]==ddl and db.execute('SELECT * FROM "'+name+'"').fetchall()==rows
        assert db.execute('SELECT MAX(version) FROM schema_version').fetchone()[0]==28
    raw=path.read_bytes();engine=create_engine(url);m.migrate_database(engine);engine.dispose();assert path.read_bytes()==raw

from test_request_retry import retry_client,preflight as retry_preview,execute as retry_execute
from test_request_finalize import finalize_client,preview as finalize_preview,execute as finalize_execute


def test_retry_old_preview_stale_and_pre_floor_source_blocked(retry_client):
    c,i=retry_client;before=retry_preview(c,i).json();assert before['eligible']
    r=add(c,i);assert r.status_code==200
    assert retry_execute(c,i,before['plan_hash']).status_code==409
    mid=r.json()['memory_id'];d={'expected_room_revision':1,'expected_revision':1}
    assert c.request('DELETE',url(i)+'/'+mid,json=d,headers=h(c,i,'delete')).status_code==200
    new=retry_preview(c,i).json();assert not new['eligible'] and new['blocked_reason']=='memory_context_changed'
    assert i['provider_calls']==[]


def test_finalizer_old_plan_stale_then_explicit_source_still_no_memory(finalize_client):
    c,i=finalize_client;before=finalize_preview(c,i).json();assert before['eligible']
    r=add(c,i);assert r.status_code==200
    assert finalize_execute(c,i,before['plan_hash']).status_code==409
    new=finalize_preview(c,i).json();assert new['eligible'] and new['plan_hash']!=before['plan_hash']
    result=finalize_execute(c,i,new['plan_hash']);assert result.status_code==201,result.text
    assert i['captured'] and i['captured'][0].memory is None and 'Remember me' not in json.dumps(i['captured'][0].events)


def test_generation_change_during_connection_await_prevents_transport(memory,monkeypatch):
    c,i=memory;import app.services.context as context
    original=context.resolve_connection_async
    async def resolve(*args,**kwargs):
        result=await original(*args,**kwargs)
        with c.app.state.session_factory() as db:
            for run in db.query(RoomAgentRun).filter_by(room_id=i['room']):run.generation+=1
            db.commit()
        return result
    monkeypatch.setattr(context,'resolve_connection_async',resolve)
    calls=[]
    async def provider(req):calls.append(req);return ProviderResult(text='forbidden stale request')
    c.app.state.scheduler.provider=provider
    c.post(f'/api/v1/conversations/{i["room"]}/messages',json={'content':'stop race'},headers=h(c,i))
    assert calls==[]


def test_preview_metadata_exact_recipient_entries_floor_and_serialized_size(memory):
    c,i=memory;first=add(c,i,scope='agent',target_agent_id=i['a']).json()
    p=c.post(f'/api/v1/conversations/{i["room"]}/context-preview',json={'content':'inspect'},headers=h(c,i)).json()
    a,b=p['agents'];meta=a['memory_projection'];assert meta['agent_id']==i['a']
    assert meta['entries']==[{'id':first['memory_id'],'revision':1,'scope':'agent','target_agent_id':i['a']}]
    assert b['memory_projection']['entries']==[] and 'Remember me' not in json.dumps(b['messages'])
    with c.app.state.session_factory() as db:assert meta['serialized_chars']==len(json.dumps(snapshot(db,i['room'],i['a'])['memory'],ensure_ascii=False))
