"""Owner-selected references and durable pin-operation outcomes."""
VERSION=68
REQUIRED_COLUMNS={
 'library_pins':{'owner_id','bot_id','source_key','source_json','revision','pinned'},
 'library_pin_operations':{'owner_id','bot_id','operation_id','request_digest','state','revision','pinned','code'},
}
def upgrade(db):
 db.execute('''CREATE TABLE library_pins (owner_id INTEGER NOT NULL REFERENCES users(id), bot_id INTEGER NOT NULL REFERENCES agents(id), source_key TEXT NOT NULL, source_json TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>=1), pinned INTEGER NOT NULL CHECK(pinned IN (0,1)), PRIMARY KEY(owner_id,bot_id,source_key))''')
 db.execute('''CREATE TABLE library_pin_operations (owner_id INTEGER NOT NULL REFERENCES users(id), bot_id INTEGER NOT NULL REFERENCES agents(id), operation_id TEXT NOT NULL, request_digest TEXT NOT NULL CHECK(length(request_digest)=64), state TEXT NOT NULL CHECK(state IN ('applied','rejected','fenced')), revision INTEGER, pinned INTEGER CHECK(pinned IS NULL OR pinned IN (0,1)), code TEXT, PRIMARY KEY(owner_id,bot_id,operation_id))''')
