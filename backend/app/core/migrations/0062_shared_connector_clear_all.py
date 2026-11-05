"""Durable account-scoped barrier for global connector-preference clearing."""
VERSION=62
REQUIRED_COLUMNS={'shared_connector_clear_barriers':{'owner_id','issuer','subject','key_fingerprint','operation_id','state'}}
def upgrade(db):
 db.execute('''CREATE TABLE shared_connector_clear_barriers (
  owner_id INTEGER NOT NULL REFERENCES users(id),issuer TEXT NOT NULL,subject TEXT NOT NULL,key_fingerprint TEXT NOT NULL,
  operation_id TEXT NOT NULL,state TEXT NOT NULL CHECK(state IN ('pending','uncertain')),
  PRIMARY KEY(owner_id,issuer,subject,key_fingerprint))''')
