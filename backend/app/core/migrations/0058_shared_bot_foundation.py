"""Staged team and sharing records; no identity provider is installed here."""
VERSION = 58
REQUIRED_COLUMNS = {
    'team_realms': {'id','issuer','team_subject','owner_id','owner_subject','revision','revocation_epoch','created_at'},
    'team_people': {'team_id','issuer','subject','role','state','revision'},
    'team_invites': {'id','team_id','issuer','subject','role','state','expires_at','created_at'},
    'shared_bot_publications': {'id','team_id','owner_id','owner_issuer','owner_subject','agent_id','role_text','knowledge_text','audience','revision','revocation_epoch','state','updated_at'},
    'shared_bot_people': {'share_id','issuer','subject','role'},
    'shared_bot_rooms': {'share_id','issuer','subject','recipient_id','room_id','connection_id','model_id','share_revision','revocation_epoch','team_revocation_epoch','created_at'},
}

def upgrade(db):
    db.execute('''CREATE TABLE team_realms (id TEXT PRIMARY KEY, issuer TEXT NOT NULL, team_subject TEXT NOT NULL,
        owner_id INTEGER NOT NULL REFERENCES users(id), owner_subject TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>0),
        revocation_epoch INTEGER NOT NULL CHECK(revocation_epoch>=0), created_at TEXT NOT NULL, UNIQUE(issuer,team_subject))''')
    db.execute('''CREATE TABLE team_people (team_id TEXT NOT NULL REFERENCES team_realms(id), issuer TEXT NOT NULL,
        subject TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('owner','manager','member')),
        state TEXT NOT NULL CHECK(state IN ('active','revoked')), revision INTEGER NOT NULL CHECK(revision>0),
        PRIMARY KEY(team_id,issuer,subject))''')
    db.execute('''CREATE TABLE team_invites (id TEXT PRIMARY KEY, team_id TEXT NOT NULL REFERENCES team_realms(id),
        issuer TEXT NOT NULL, subject TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('manager','member')),
        state TEXT NOT NULL CHECK(state IN ('pending','accepted','revoked')), expires_at TEXT NOT NULL, created_at TEXT NOT NULL)''')
    db.execute('''CREATE TABLE shared_bot_publications (id TEXT PRIMARY KEY, team_id TEXT NOT NULL REFERENCES team_realms(id),
        owner_id INTEGER NOT NULL REFERENCES users(id), owner_issuer TEXT NOT NULL, owner_subject TEXT NOT NULL, agent_id INTEGER NOT NULL REFERENCES agents(id),
        role_text TEXT NOT NULL, knowledge_text TEXT NOT NULL, audience TEXT NOT NULL CHECK(audience IN ('team','selected')),
        revision INTEGER NOT NULL CHECK(revision>0), revocation_epoch INTEGER NOT NULL CHECK(revocation_epoch>=0),
        state TEXT NOT NULL CHECK(state IN ('prepared','revoked')), updated_at TEXT NOT NULL, UNIQUE(team_id,agent_id))''')
    db.execute('''CREATE TABLE shared_bot_people (share_id TEXT NOT NULL REFERENCES shared_bot_publications(id),
        issuer TEXT NOT NULL, subject TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('manager','collaborator')),
        PRIMARY KEY(share_id,issuer,subject))''')
    db.execute('''CREATE TABLE shared_bot_rooms (share_id TEXT NOT NULL REFERENCES shared_bot_publications(id),
        issuer TEXT NOT NULL, subject TEXT NOT NULL, recipient_id INTEGER NOT NULL REFERENCES users(id),
        room_id INTEGER NOT NULL UNIQUE REFERENCES conversations(id), connection_id INTEGER NOT NULL REFERENCES provider_connections(id),
        model_id TEXT NOT NULL, share_revision INTEGER NOT NULL, revocation_epoch INTEGER NOT NULL, team_revocation_epoch INTEGER NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY(share_id,issuer,subject))''')
