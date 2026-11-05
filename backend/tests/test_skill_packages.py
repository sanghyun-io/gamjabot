from test_room_state import room_client,headers
from test_bot_creation import private_team
from app.models.owner_skills import OwnerSkillBinding


def test_builtin_install_readback_edit_and_binding(room_client):
    client,(owner,other,project,_,_)=room_client;auth=headers(client,owner)
    bot,_,_=private_team(client,owner,project)
    catalog=client.get('/api/v1/skill-packages',headers=auth,params={'q':'meeting'}).json()
    preview=catalog['items'][0];manifest=preview['manifest'];key={**auth,'Idempotency-Key':'package-case'}
    response=client.post('/api/v1/skill-packages/install',headers=key,json={'manifest':manifest,'digest':preview['digest']})
    assert response.status_code==200,response.text
    skill=response.json();sid=skill['skill_id'];assert not skill['active'] and skill['source'] is None
    assert skill['package_provenance']['origin']=='builtin' and not skill['package_modified']
    assert client.get('/api/v1/skills/commands/package-case',headers=auth).json()==skill
    assert client.post('/api/v1/skill-packages/install',headers=key,json={'manifest':manifest,'digest':preview['digest']}).json()==skill
    with client.app.state.session_factory() as db:assert db.query(OwnerSkillBinding).filter_by(skill_id=sid).count()==0
    assert client.get(f'/api/v1/skills/{sid}',headers=headers(client,other)).status_code==404
    changed=dict(manifest);changed['content']='Changed content'
    rejected=client.post('/api/v1/skill-packages/install',headers=key,json={'manifest':changed,'digest':preview['digest']})
    assert rejected.status_code==409
    update={k:manifest[k] for k in ('name','description','content')};update.update(expected_revision=1,active=True,content='Edited by Owner')
    edited=client.patch(f'/api/v1/skills/{sid}',headers=auth,json=update).json()
    assert edited['package_modified'] and edited['package_provenance']==skill['package_provenance']
    assert client.put(f'/api/v1/bots/{bot}/skills/{sid}',headers=auth,json={'expected_revision':0,'enabled':True}).status_code==200
    versions=client.get(f'/api/v1/skills/{sid}/versions',headers=auth).json()['versions']
    assert versions[0]['package_modified'] and not versions[1]['package_modified']


def test_manual_preview_strict_and_frozen_digest(room_client):
    client,(owner,_,_,_,_)=room_client;auth=headers(client,owner)
    manifest={'package_id':'example.notes','package_version':'1','author':'Owner','license':'unspecified','name':'Notes','description':'','content':'Use supplied notes.'}
    p=client.post('/api/v1/skill-packages/preview',headers=auth,json=manifest);assert p.status_code==200
    preview=p.json();bad=dict(manifest,hooks=['execute'])
    assert client.post('/api/v1/skill-packages/preview',headers=auth,json=bad).status_code==422
    key={**auth,'Idempotency-Key':'manual-package'}
    assert client.post('/api/v1/skill-packages/install',headers=key,json={'manifest':dict(manifest,content='different'),'digest':preview['digest']}).status_code==409
    installed=client.post('/api/v1/skill-packages/install',headers=key,json={'manifest':manifest,'digest':preview['digest']}).json()
    assert installed['package_provenance']['origin']=='manual_manifest' and not installed['active']
