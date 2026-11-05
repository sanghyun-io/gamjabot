"""Synthetic canonical events/SQLite/API, with no installed DB, model or file input."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.exc import IntegrityError

from app.api.routes.auth import create_access_token
from app.main import create_app
from app.models.models import User, Agent, Project, Conversation
from app.models.collaboration import RoomEvent, ToolAction
from app.models.room_result import RoomResult, RoomResultVersion, RoomResultCommand, RoomResultReview
from app.schemas.room_result import ResultSave, ResultVersionSave, ResultReviewWrite
from app.services import room_results as service
from app.services.result_content import visible_answer, answer_links, safe_url


@pytest.fixture
def results(sqlite_url, monkeypatch):
    monkeypatch.setenv("GAMJA_LOCAL_SESSION_TOKEN", "synthetic-results-session")
    app = create_app(database_url=sqlite_url)
    with TestClient(app) as client:
        with app.state.session_factory() as db:
            owners = [User(email=f"result{i}@test.invalid", username=f"owner{i}", hashed_password="x") for i in range(2)]
            db.add_all(owners); db.flush()
            projects = [Project(user_id=o.id, name="Project") for o in owners]
            db.add_all(projects); db.flush()
            rooms = [Conversation(user_id=owners[0].id, project_id=projects[0].id, title="A"),
                     Conversation(user_id=owners[0].id, project_id=projects[0].id, title="B"),
                     Conversation(user_id=owners[1].id, project_id=projects[1].id, title="Other")]
            db.add_all(rooms); db.flush()
            agents = [Agent(user_id=owners[0].id, name=n, description="d", system_prompt="none") for n in ["CURRENT renamed author", "CURRENT renamed reviewer"]]
            db.add_all(agents); db.flush()
            ids = dict(owner=owners[0].id, other=owners[1].id, room=rooms[0].id, second=rooms[1].id,
                       other_room=rooms[2].id, author=agents[0].id, reviewer=agents[1].id)
            now = datetime.now(timezone.utc)
            def source(name, seq, actor, payload, kind="message", key=None, actor_type="agent"):
                eid = str(uuid4()); ids[name] = eid
                db.add(RoomEvent(event_id=eid, room_id=rooms[0].id, seq=seq, actor_type=actor_type,
                    actor_id=actor, actor_name_snapshot="HISTORICAL author" if actor == agents[0].id else "HISTORICAL reviewer",
                    kind=kind, payload=payload, created_at=now, idempotency_key=key))
            source("request", 1, owners[0].id, {"content": "Original request"}, actor_type="user")
            source("answer", 2, agents[0].id, {"content": "<think>HIDDEN https://hidden.invalid</think>\nUseful v1 [site](https://public.invalid/a)",
                "trigger_event_id": ids["request"], "round": 1, "generation": 7,
                "model_use": {"connection_id": 4, "model_id": "historical-model", "provider_kind": "compatible", "watermark_seq": 1}})
            source("next", 3, agents[0].id, {"content": "Useful v2", "trigger_event_id": ids["request"], "generation": 8})
            source("peer", 4, agents[1].id, {"content": "A declared opinion, not an approval", "reply_provenance": {
                "version": 1, "watermark_seq": 3, "provided_peer_event_ids": [ids["answer"], ids["next"]],
                "reply_to_event_ids": [ids["answer"]], "declaration_state": "declared"}})
            action_id = str(uuid4()); ids["action"] = action_id
            ref = f"rooms/{rooms[0].id}/screenshots/{action_id}.png"
            db.add(ToolAction(action_id=action_id, room_id=rooms[0].id, agent_id=agents[0].id,
                family="screenshot", arguments={}, state="completed", trigger_event_id=ids["request"],
                observation={"screenshot_ref": ref, "state": "completed"}))
            source("file", 5, None, {"action_id": action_id, "agent_id": agents[0].id,
                "state": "completed", "screenshot_ref": ref, "image_width": 16, "image_height": 16},
                kind="tool_result", key="tool-result:"+action_id, actor_type="tool")
            source("third", 6, agents[0].id, {"content": "Useful v3"})
            db.commit()
        yield client, ids


def headers(client, ids, key=None, owner=None):
    out = {"X-Gamja-Session": client.app.state.local_session_token,
           "Authorization": "Bearer " + create_access_token({"sub": owner or ids["owner"]})}
    if key is not None: out["Idempotency-Key"] = key
    return out


def base(ids, room=None):
    return f"/api/v1/conversations/{room or ids['room']}/results"


def save(client, ids, source="answer", kind="text", key="save", **extra):
    return client.post(base(ids), json={"source_event_id": ids[source], "kind": kind,
        "title": "Result", **extra}, headers=headers(client, ids, key))


def show(client, ids, created, version=1, room=None, owner=None):
    return client.get(f"{base(ids,room)}/{created['result_id']}/versions/{version}", headers=headers(client,ids,owner=owner))


def test_text_link_file_refs_and_original_provenance(results):
    c, ids = results
    text = save(c, ids); assert text.status_code == 201, text.text
    assert text.headers['cache-control'] == 'no-store'
    first = text.json(); doc = show(c, ids, first).json()
    assert "HIDDEN" not in doc["content"] and "Useful v1" in doc["content"]
    assert doc["author_name_snapshot"] == "HISTORICAL author"
    assert doc["execution"]["model_use"]["model_id"] == "historical-model"
    assert doc["execution"]["generation"] == 7 and doc["source"]["event_id"] == ids["answer"]
    assert doc["evidence_status"] == "none" and doc["state"] == "draft"
    link = save(c, ids, kind="link", key="link", url="https://public.invalid/a")
    assert link.status_code == 201, link.text
    assert show(c, ids, link.json()).json()["link_report"] == "unchecked"
    file = save(c, ids, source="file", kind="file", key="file")
    assert file.status_code == 201, file.text
    f = show(c, ids, file.json()).json()
    assert f["file_ref"]["action_id"] == ids["action"] and f["file_status"] == "unchecked"
    assert f["author_name_snapshot"] == "" and f["review_support"] == "unsupported"
    assert "screenshot_ref" not in str(f) and f["content"] == ""
    listing = c.get(base(ids), headers=headers(c,ids)).json()
    assert len(listing["items"]) == 3 and all("content" not in x for x in listing["items"])
    with c.app.state.session_factory() as db:
        action = db.get(ToolAction, ids["action"])
        action.observation = {**action.observation, "artifact_state": "deleted"}; db.commit()
    assert show(c, ids, file.json()).json()["file_status"] == "removed"


def test_replay_unique_source_conflicting_payload_and_no_extra_audit(results):
    c, ids = results
    first = save(c,ids).json(); replay = save(c,ids)
    assert replay.status_code == 200 and replay.json() == {**first,"replayed":True}
    assert save(c,ids,title="Different").status_code == 409
    assert save(c,ids,key="different-key").status_code == 409
    assert save(c,ids,source="next").status_code == 409
    with c.app.state.session_factory() as db:
        assert db.query(RoomResult).count() == db.query(RoomResultCommand).count() == 1
        assert db.query(RoomEvent).filter_by(kind="activity").count() == 1


def test_version_review_late_opinion_pinned_to_v1_and_state_never_inherited(results):
    c, ids = results; first = save(c,ids).json(); path=f"{base(ids)}/{first['result_id']}/versions"
    state=c.post(path+"/1/state",json={"state":"final","expected_state_revision":1},headers=headers(c,ids,"state"))
    assert state.status_code==200
    v2=c.post(path,json={"source_event_id":ids["next"],"title":"Revision","kind":"text","expected_current_version":1},headers=headers(c,ids,"version"))
    assert v2.status_code==201, v2.text
    opinion=c.post(path+"/1/reviews",json={"review_event_id":ids["peer"]},headers=headers(c,ids,"review"))
    assert opinion.status_code==201,opinion.text
    v1=show(c,ids,first).json(); new=show(c,ids,first,2).json()
    assert v1["linked_opinion_count"]==1 and v1["state"]=="final"
    assert new["linked_opinion_count"]==0 and new["state"]=="draft" and new["state_revision"]==1
    assert new["execution"]["model_use"] is None
    assert c.post(path+"/2/reviews",json={"review_event_id":ids["peer"]},headers=headers(c,ids,"wrong-version")).status_code==422
    assert c.post(path+"/1/reviews",json={"review_event_id":ids["peer"]},headers=headers(c,ids,"new-review-key")).status_code==409
    assert c.post(path+"/1/state",json={"state":"draft","expected_state_revision":1},headers=headers(c,ids,"stale-state")).status_code==409
    reviews=c.get(path+"/1/reviews",headers=headers(c,ids)).json()["items"]
    assert reviews[0]["version"]==1 and reviews[0]["coverage"]=="source_reply_only"


@pytest.mark.parametrize("mutation", ["null", "bool_version", "future", "undeclared", "unprovided", "self", "duplicate", "unknown"])
def test_unverified_peer_provenance_fails_closed(results,mutation):
    c,ids=results;first=save(c,ids).json()
    with c.app.state.session_factory() as db:
        r=db.get(RoomEvent,ids["peer"]);meta=dict(r.payload["reply_provenance"])
        if mutation=="null":meta=None
        elif mutation=="bool_version":meta["version"]=True
        elif mutation=="future":meta["watermark_seq"]=r.seq
        elif mutation=="undeclared":meta["declaration_state"]="absent"
        elif mutation=="unprovided":meta["provided_peer_event_ids"]=[ids["next"]]
        elif mutation=="self":r.actor_id=ids["author"]
        elif mutation=="duplicate":meta["provided_peer_event_ids"]=[ids["answer"],ids["answer"]]
        else:meta["unexpected"]="must reject"
        r.payload={**r.payload,"reply_provenance":meta};flag_modified(r,"payload");db.commit()
    response=c.post(f"{base(ids)}/{first['result_id']}/versions/1/reviews",json={"review_event_id":ids["peer"]},headers=headers(c,ids,"opinion"))
    assert response.status_code==422,response.text
    with c.app.state.session_factory() as db:assert db.query(RoomResultReview).count()==0


def test_room_owner_isolation_archived_room_and_replay_authority(results):
    c,ids=results;first=save(c,ids).json()
    assert show(c,ids,first,room=ids["second"]).status_code==404
    assert show(c,ids,first,owner=ids["other"]).status_code==404
    assert c.post(base(ids,ids["second"]),json={"kind":"text","title":"x","source_event_id":ids["answer"]},headers=headers(c,ids,"cross")).status_code==404
    with c.app.state.session_factory() as db:
        room=db.get(Conversation,ids["room"]);room.is_active=False;db.commit()
    assert show(c,ids,first).status_code==200
    assert save(c,ids).status_code==200  # Receipt replay, no new write in an archived room.
    assert save(c,ids,source="next",key="new").status_code==404
    with c.app.state.session_factory() as db:
        # Ownership transfer is also blocked structurally by the result's composite FK.
        room=db.get(Conversation,ids["room"]);room.user_id=ids["other"]
        with pytest.raises(IntegrityError):db.commit()
        db.rollback()
        user=db.get(User,ids["owner"]);user.is_active=False;db.commit()
    # Simulate an old auth dependency result; mutation still checks fresh authority before replay.
    with c.app.state.session_factory() as db:
        with pytest.raises(HTTPException) as denied:
            service.mutate(db,ids["owner"],ids["room"],"save",ResultSave(source_event_id=ids["answer"],kind="text",title="Result"),"save")
        assert denied.value.status_code==404


def test_link_reports_evidence_and_no_network_claim(results):
    c,ids=results
    first=save(c,ids,kind="link",url="https://public.invalid/a",evidence=[{"event_id":ids["request"]},{"url":"https://evidence.invalid"}]).json()
    path=f"{base(ids)}/{first['result_id']}/versions/1/link-report"
    report=c.post(path,json={"report":"reported_bad","expected_state_revision":1},headers=headers(c,ids,"report"))
    assert report.status_code==200,report.text
    doc=show(c,ids,first).json();assert doc["link_report"]=="reported_bad" and doc["evidence_status"]=="provided_unverified"
    assert doc["link_report_owner_id"]==ids["owner"] and doc["link_reported_at"]
    assert doc["state_owner_id"] is None and doc["state_changed_at"] is None
    assert c.post(path,json={"report":"unchecked","expected_state_revision":1},headers=headers(c,ids,"stale-report")).status_code==409
    assert save(c,ids,kind="link",key="hidden",url="https://hidden.invalid").status_code==422
    assert save(c,ids,key="dup-evidence",evidence=[{"event_id":ids["request"]}]*2).status_code==422


@pytest.mark.parametrize("extra",[{"author_agent_id":1},{"content":"forged"},{"kind":"text","url":"file:///tmp/private"},{"title":"  "},{"evidence":[{"event_id":str(uuid4()),"url":"https://x.invalid"}]}])
def test_strict_write_shape_and_no_caller_authorship(results,extra):
    c,ids=results;assert save(c,ids,**extra).status_code==422


def test_transaction_rolls_back_version_command_and_activity_together(results,monkeypatch):
    c,ids=results
    original=service.stage_event
    def failed(*a,**kw):
        original(*a,**kw)
        raise RuntimeError("synthetic audit persistence failure")
    monkeypatch.setattr(service,"stage_event",failed)
    with c.app.state.session_factory() as db:
        with pytest.raises(RuntimeError):
            service.mutate(db,ids["owner"],ids["room"],"save",ResultSave(source_event_id=ids["answer"],kind="text",title="x"),"rollback")
    with c.app.state.session_factory() as db:
        assert db.query(RoomResult).count()==db.query(RoomResultVersion).count()==db.query(RoomResultCommand).count()==0
        assert db.query(RoomEvent).filter_by(kind="activity").count()==0


@pytest.mark.parametrize("field,value",[("state","failed"),("agent_id",99999),("screenshot_ref","rooms/99999/screenshots/other.png")])
def test_file_reference_requires_exact_completed_room_action_event(results,field,value):
    c,ids=results
    with c.app.state.session_factory() as db:
        source=db.get(RoomEvent,ids["file"]);source.payload={**source.payload,field:value};db.commit()
    response=save(c,ids,source="file",kind="file")
    assert response.status_code==422
    with c.app.state.session_factory() as db:assert db.query(RoomResult).count()==0


def test_review_database_fk_pins_exact_source_version_and_room(results):
    c,ids=results;first=save(c,ids).json()
    c.post(f"{base(ids)}/{first['result_id']}/versions/1/reviews",json={"review_event_id":ids["peer"]},headers=headers(c,ids,"review"))
    with c.app.state.session_factory() as db:
        record=db.query(RoomResultReview).one();record.source_event_id=ids["next"]
        with pytest.raises(IntegrityError):db.commit()
        db.rollback()
        row=db.get(RoomResultVersion,(first["result_id"],1));row.room_id=ids["second"]
        with pytest.raises(IntegrityError):db.commit()
        db.rollback()


def test_source_text_is_bounded_and_never_silently_truncated(results):
    c,ids=results
    with c.app.state.session_factory() as db:
        source=db.get(RoomEvent,ids["answer"]);source.payload={"content":"한"*22000};db.commit()
    response=save(c,ids);assert response.status_code==422
    with c.app.state.session_factory() as db:assert db.query(RoomResult).count()==0


def test_stable_result_and_version_pagination_has_no_duplicate_rows(results):
    c,ids=results
    for i,s in enumerate(["answer","next","third"]):assert save(c,ids,source=s,key=str(i)).status_code==201
    seen=[];cursor=None
    for _ in range(3):
        response=c.get(base(ids),params={"limit":1,**({"after":cursor} if cursor else {})},headers=headers(c,ids));assert response.status_code==200
        page=response.json();seen.extend(x["result_id"] for x in page["items"]);cursor=page["next_cursor"]
    assert len(set(seen))==3 and cursor is None


def test_version_target_scope_is_checked_before_duplicate_source(results):
    c,ids=results;save(c,ids)
    response=c.post(f"{base(ids)}/{uuid4()}/versions",json={"source_event_id":ids["answer"],
        "kind":"text","title":"x","expected_current_version":1},headers=headers(c,ids,"unknown-target"))
    assert response.status_code==404


def test_concurrent_once_registration_and_version_cas(results):
    c,ids=results
    def command(source,key,operation="save",result_id=None):
        with c.app.state.session_factory() as db:
            value=dict(source_event_id=ids[source],kind="text",title="x")
            if operation=="version":value["expected_current_version"]=1
            try:return service.mutate(db,ids["owner"],ids["room"],operation,
                (ResultVersionSave if operation=="version" else ResultSave)(**value),key,result_id=result_id)
            except HTTPException as e:return e.status_code
    with ThreadPoolExecutor(2) as pool:out=list(pool.map(lambda _:command("answer","same"),range(2)))
    assert len({x["result_id"] for x in out})==1 and sorted(x["replayed"] for x in out)==[False,True]
    rid=out[0]["result_id"]
    with ThreadPoolExecutor(2) as pool:
        calls=[pool.submit(command,s,s,"version",rid) for s in ["next","third"]];out=[x.result() for x in calls]
    assert sum(isinstance(x,dict) for x in out)==1 and 409 in out


def test_cli_uses_same_api_exact_ids_and_explicit_retry_key(results,capsys):
    from app.owner_cli import command
    c,ids=results
    class Client:
        def call(self,method,path,body=None,**kw):
            response=c.request(method,"/api/v1"+path,json=body,headers=headers(c,ids,kw.get("idempotency_key")))
            assert response.status_code in {200,201},response.text
            return response.json()
    payload={"source_event_id":ids["answer"],"kind":"text","title":"CLI"}
    words=["room","results","save",str(ids["room"]),"--idempotency-key","cli-stable"]
    command(Client(),words,input_body=payload)
    import json
    first=json.loads(capsys.readouterr().out)
    command(Client(),words,input_body=payload);assert json.loads(capsys.readouterr().out)["replayed"]
    command(Client(),["room","results","show",str(ids["room"]),first["result_id"],"1"])
    assert json.loads(capsys.readouterr().out)["source"]["event_id"]==ids["answer"]
    with pytest.raises(RuntimeError):command(Client(),["room","results","save",str(ids["room"])],input_body=payload)


@pytest.mark.parametrize("raw,expected",[("<think>private</think>\npublic","\npublic"),("<think>unfinished",""),("```html\n<think>literal</think>\n```","```html\n<think>literal</think>\n```"),("`<think>literal</think>`","`<think>literal</think>`"),("Answer\n<gamja_reply_to>{bad}</gamja_reply_to>","Answer")])
def test_visible_answer_preserves_code_and_hides_reasoning_footer(raw,expected):
    assert visible_answer(raw)==expected


@pytest.mark.parametrize("url",["javascript:alert(1)","file:///tmp/a","https://user:pass@host.invalid","https://x.invalid/\nprivate","https://x.invalid:99999","https://x.invalid/\u202e"])
def test_unsafe_links_never_become_navigation_or_network_targets(url):
    assert not safe_url(url)


def test_link_selector_ignores_code_and_multiline_html_comments():
    s="```\nhttps://code.invalid\n```\n`https://inline.invalid`\n<!--\nhttps://hidden.invalid\n-->\n[visible](https://public.invalid/a)"
    assert answer_links(s)=={"https://public.invalid/a"}
