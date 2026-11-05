"""Recipient-local bindings to canonical shared publications."""
VERSION = 59
REQUIRED_COLUMNS = {'shared_text_rooms': {'owner_id','issuer','subject','team_id','publication_id','room_id','agent_id','connection_id','model_id'}}

def upgrade(db):
    db.execute('''CREATE TABLE shared_text_rooms (
        owner_id INTEGER NOT NULL REFERENCES users(id), issuer TEXT NOT NULL, subject TEXT NOT NULL,
        team_id TEXT NOT NULL, publication_id TEXT NOT NULL,
        room_id INTEGER NOT NULL UNIQUE REFERENCES conversations(id),
        agent_id INTEGER NOT NULL UNIQUE REFERENCES agents(id),
        connection_id INTEGER NOT NULL REFERENCES provider_connections(id), model_id TEXT NOT NULL,
        PRIMARY KEY(owner_id,issuer,subject,team_id,publication_id))''')
